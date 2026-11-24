#!/usr/bin/env perl
use strict;
use warnings;
use Fcntl qw(:flock);
BEGIN {
    my $d = $0; $d =~ s{[^/\\]*$}{}; $d = '.' if $d eq ''; $d =~ s{[/\\]+$}{};
    unshift @INC, "$d/lib";
}
use TsJson;

# Stop hook (incremental byte-offset scan).
# - Acquires flock on sessions.lock to eliminate concurrent subagent race conditions.
# - Preserves history safely on any parse error (never wipes sessions.json).
# - Tracks real cache_hit_rate, warm_cache_hit_rate, cache_drops, tail_2049_hits,
#   and output_per_turn in PLUGIN_DATA/sessions.json.
# - Always emits '{}' to stdout (0 tokens injected, never blocks stop).

my $raw = TsJson::read_stdin_capped();
my $doc = TsJson::decode_hook_input($raw);
if (!defined $doc) { print '{}'; exit 0; }

my $tx   = TsJson::get_str($doc, 'transcriptPath') // '';
my $cid  = TsJson::get_str($doc, 'conversationId') // '';
my $term = TsJson::get_str($doc, 'terminationReason') // '';

if ($tx eq '' || $cid eq '' || !-f $tx) { print '{}'; exit 0; }

eval {
    my @st = stat($tx);
    return unless @st;
    my ($inode, $fsize) = ($st[1], $st[7]);
    return if $fsize == 0;

    my $plugin_root = $ENV{PLUGIN_ROOT};
    if (!$plugin_root) {
        my $s = $0; $s = "$ENV{PWD}/$s" if $s !~ m{^/} && $ENV{PWD};
        $s =~ s{/[^/]+/[^/]+$}{}; $plugin_root = $s;
    }
    my $agy_pd = ($ENV{HOME} // '') . "/.gemini/antigravity/plugin_data";
    my $data_dir = $ENV{PLUGIN_DATA} || (-d $agy_pd ? "$agy_pd/token-saver" : "$plugin_root/.data");
    if (!-d $data_dir) {
        require File::Path;
        File::Path::make_path($data_dir);
    }

    # Acquire exclusive lock across read-modify-write
    open(my $lk, '>', "$data_dir/sessions.lock") or return;
    flock($lk, LOCK_EX) or do { close $lk; return; };

    require JSON::PP;
    my $sfile = "$data_dir/sessions.json";
    my $sessions = {};
    if (-f $sfile && -s $sfile > 0) {
        open(my $rfh, '<:raw', $sfile) or do { close $lk; return; };
        local $/;
        my $txt = <$rfh>;
        close $rfh;
        my $parsed = eval { JSON::PP::decode_json($txt) };
        if ($@ || ref($parsed) ne 'HASH') {
            # Never wipe existing history on corrupt/partial read
            close $lk;
            return;
        }
        $sessions = $parsed;
    }

    my $rec = ref($sessions->{$cid}) eq 'HASH' ? $sessions->{$cid} : undef;
    my $offset = 0;
    my ($turns, $tot_in, $tot_cr, $tot_out, $warm_in, $warm_cr, $drops, $tail_2049, $prev_cr) = (0) x 9;
    my %models;

    if ($rec && ($rec->{_inode} // -1) == $inode && ($rec->{_offset} // 0) <= $fsize) {
        $offset    = int($rec->{_offset} // 0);
        if ($offset == $fsize) {
            close $lk;
            return;
        }
        $turns     = int($rec->{turns} // 0);
        $tot_in    = int($rec->{uncached_input_tokens} // 0);
        $tot_cr    = int($rec->{cache_read_tokens} // 0);
        $tot_out   = int($rec->{output_tokens} // 0);
        $warm_in   = int($rec->{_warm_in} // 0);
        $warm_cr   = int($rec->{_warm_cr} // 0);
        $drops     = int($rec->{cache_drops} // 0);
        $tail_2049 = int($rec->{tail_2049_hits} // 0);
        $prev_cr   = int($rec->{_prev_cr} // 0);
        if (ref($rec->{models}) eq 'ARRAY') {
            $models{$_} = 1 for @{$rec->{models}};
        }
    }

    open(my $tfh, '<:raw', $tx) or do { close $lk; return; };
    seek($tfh, $offset, 0) if $offset > 0;
    my $new_offset = $offset;
    while (my $line = <$tfh>) {
        last unless $line =~ /\n\z/; # Only consume complete newline-terminated lines
        $new_offset += length($line);
        next unless index($line, '"PLANNER_RESPONSE"') >= 0;
        my $in_t  = ($line =~ /"input_tokens"\s*:\s*(\d+)/)      ? int($1) : 0;
        my $cr_t  = ($line =~ /"cache_read_tokens"\s*:\s*(\d+)/) ? int($1) : 0;
        my $out_t = ($line =~ /"output_tokens"\s*:\s*(\d+)/)     ? int($1) : 0;
        next if ($in_t == 0 && $cr_t == 0 && $out_t == 0);
        $turns++;
        $tot_in  += $in_t;
        $tot_cr  += $cr_t;
        $tot_out += $out_t;
        $models{$1} = 1 if $line =~ /"model"\s*:\s*"([^"]+)"/;
        if ($turns > 1) {
            $warm_in += $in_t;
            $warm_cr += $cr_t;
            $drops++ if ($prev_cr >= 4000 && $cr_t < $prev_cr * 0.8);
            # Gemini 4 2,049-2,050 token incremental tail penalty detector
            $tail_2049++ if ($in_t >= 2048 && $in_t <= 2060 && $cr_t >= 4000);
        }
        $prev_cr = $cr_t;
    }
    close $tfh;

    if ($turns > 0) {
        my $denom = $tot_in + $tot_cr;
        my $wden  = $warm_in + $warm_cr;
        $sessions->{$cid} = {
            updated_ts            => time(),
            turns                 => $turns,
            models                => [sort keys %models],
            uncached_input_tokens => $tot_in,
            cache_read_tokens     => $tot_cr,
            output_tokens         => $tot_out,
            output_per_turn       => int($tot_out / $turns),
            cache_hit_rate        => $denom ? (sprintf('%.4f', $tot_cr / $denom) + 0) : 0,
            warm_cache_hit_rate   => $wden  ? (sprintf('%.4f', $warm_cr / $wden) + 0) : 0,
            cache_drops           => $drops,
            tail_2049_hits        => $tail_2049,
            termination_reason    => $term,
            _inode                => $inode,
            _offset               => $new_offset,
            _warm_in              => $warm_in,
            _warm_cr              => $warm_cr,
            _prev_cr              => $prev_cr,
        };

        # Keep most recent 200 sessions
        my @keys = keys %$sessions;
        if (@keys > 200) {
            my @keep = (sort { ($sessions->{$b}{updated_ts} // 0) <=> ($sessions->{$a}{updated_ts} // 0) } @keys)[0 .. 199];
            my %trimmed = map { $_ => $sessions->{$_} } @keep;
            $sessions = \%trimmed;
        }

        my $tmp = "$data_dir/sessions.$$.tmp";
        if (open my $wfh, '>:raw', $tmp) {
            print $wfh JSON::PP::encode_json($sessions);
            close $wfh;
            rename($tmp, $sfile) or unlink($tmp);
        }
    }
    close $lk;
};

print '{}';
exit 0;
