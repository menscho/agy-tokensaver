#!/usr/bin/env perl
use strict;

# Zero-module PostInvocation hook.
# Emits ZERO-LLM-TOKEN inline UI alerts (systemMessage: "", metadata.user_markdown)
# when context crosses 100k / 180k / 300k tokens OR a severe >25% cache drop occurs.
# Never injects tokens into the model prompt and never mutates the cached prefix.

my $raw = do { local $/; <STDIN> };
if (!defined $raw || $raw eq '') { print '{}'; exit 0; }

# Fast-path: skip early turns (<4) where context is small and cache is warming up
my $inv_num = ($raw =~ /"invocationNum"\s*:\s*(\d+)/) ? int($1) : 0;
if ($inv_num < 4) { print '{}'; exit 0; }

sub json_unescape {
    my ($s) = @_;
    # \uXXXX (incl. surrogate pairs) -> UTF-8 bytes, so rewritten args stay valid UTF-8
    $s =~ s{\\(?:(["\\/bfnrt])|u([dD][89abAB][0-9a-fA-F]{2})\\u([dD][c-fC-F][0-9a-fA-F]{2})|u([0-9a-fA-F]{4}))}{
        defined $1 ? (
            $1 eq 'n' ? "\n" : $1 eq 'r' ? "\r" : $1 eq 't' ? "\t" :
            $1 eq 'b' ? "\b" : $1 eq 'f' ? "\f" : $1
        ) : do {
            my $c = defined $2 ? chr(0x10000 + ((hex($2) - 0xD800) << 10) + (hex($3) - 0xDC00)) : chr(hex($4));
            utf8::encode($c); $c
        }
    }ge;
    return $s;
}

sub json_escape {
    my ($s) = @_;
    $s =~ s/\\/\\\\/g;
    $s =~ s/"/\\"/g;
    $s =~ s/\n/\\n/g;
    $s =~ s/\r/\\r/g;
    $s =~ s/\t/\\t/g;
    $s =~ s/([\x00-\x1f])/sprintf('\\u%04x', ord($1))/ge;
    return $s;
}

sub json_get_str {
    my ($src, $key) = @_;
    return undef unless $src =~ /"\Q$key\E"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/;
    return json_unescape($1);
}

my $tx   = json_get_str($raw, 'transcriptPath') // '';
my $cid  = json_get_str($raw, 'conversationId') // '';
if ($tx eq '' || $cid eq '' || !-f $tx) { print '{}'; exit 0; }

# Tail-seek last 48KB of transcript (O(1) even on 50MB transcripts!)
my $sz = (stat($tx))[7] // 0;
if ($sz == 0) { print '{}'; exit 0; }

open(my $fh, '<:raw', $tx) or do { print '{}'; exit 0; };
my $seek_pos = $sz > 49152 ? ($sz - 49152) : 0;
seek($fh, $seek_pos, 0) if $seek_pos > 0;
my $tail = '';
sysread($fh, $tail, 49152);
close $fh;

# Parse last 2 PLANNER_RESPONSE token lines & detect CHECKPOINT between them
my @turns;
my $saw_checkpoint = 0;
for my $line (split(/\n/, $tail)) {
    if (index($line, '"CHECKPOINT"') >= 0) {
        $saw_checkpoint = 1;
        next;
    }
    next unless index($line, '"PLANNER_RESPONSE"') >= 0;
    my $in_t  = ($line =~ /"input_tokens"\s*:\s*(\d+)/)      ? int($1) : 0;
    my $cr_t  = ($line =~ /"cache_read_tokens"\s*:\s*(\d+)/) ? int($1) : 0;
    my $out_t = ($line =~ /"output_tokens"\s*:\s*(\d+)/)     ? int($1) : 0;
    next if ($in_t == 0 && $cr_t == 0 && $out_t == 0);
    my $mdl = ($line =~ /"model"\s*:\s*"([^"]+)"/)      ? $1 : '';
    my $ts  = ($line =~ /"created_at"\s*:\s*"([^"]+)"/) ? $1 : '';
    push @turns, { in => $in_t, cr => $cr_t, tot => $in_t + $cr_t, model => $mdl, ts => $ts, cp => $saw_checkpoint };
    $saw_checkpoint = 0; # a checkpoint only marks the turn immediately after it
}

if (!@turns) { print '{}'; exit 0; }
my $cur  = $turns[-1];
my $prev = @turns >= 2 ? $turns[-2] : undef;

# Load lightweight per-conversation state file in plugin_data
my $plugin_root = $ENV{PLUGIN_ROOT};
if (!$plugin_root) {
    my $s = $0; $s = "$ENV{PWD}/$s" if $s !~ m{^/} && $ENV{PWD};
    $s =~ s{/[^/]+/[^/]+$}{}; $plugin_root = $s;
}
my $agy_pd = ($ENV{HOME} // '') . "/.gemini/antigravity/plugin_data";
my $data_dir = $ENV{PLUGIN_DATA} || (-d $agy_pd ? "$agy_pd/token-saver" : "$plugin_root/.data");
my $safe_cid = $cid; $safe_cid =~ s/[^A-Za-z0-9_-]/_/g;
my $state_dir = "$data_dir/alerts";
my $state_file = "$state_dir/$safe_cid.state";

my ($last_tier, $last_drop_tot, $last_drop_ts) = (0, 0, '');
if (-f $state_file && open(my $sfh, '<', $state_file)) {
    my $st_line = <$sfh>;
    close $sfh;
    if (defined $st_line && $st_line =~ /^(\d+)\t(\d+)\t(.*)$/) {
        ($last_tier, $last_drop_tot, $last_drop_ts) = (int($1), int($2), $3);
    }
}

my @notes;

# 0. Track compaction epoch so read_hook.pl's immediate-duplicate guard resets on CHECKPOINT
if ($cur->{cp}) {
    eval {
        if (!-d $state_dir) { require File::Path; File::Path::make_path($state_dir); }
        my $ep_file = "$state_dir/$safe_cid.epoch";
        my $ep = 0;
        if (-f $ep_file && open(my $efh, '<', $ep_file)) { $ep = int(<$efh> // 0); close $efh; }
        $ep++;
        if (open(my $wfh, '>', $ep_file)) { print $wfh "$ep\n"; close $wfh; }
    };
}

# 1. Earlier, high-leverage context threshold crossings (75k, 110k, 150k, 220k; reset < 60k)
# Never emit a /compact nudge on the exact turn right after a CHECKPOINT compaction.
my $tot = $cur->{tot};
my $new_tier = $last_tier;
if ($tot < 60000) {
    $new_tier = 0;
} elsif (!$cur->{cp}) {
    my $cur_tier = $tot >= 220000 ? 220 : ($tot >= 150000 ? 150 : ($tot >= 110000 ? 110 : ($tot >= 75000 ? 75 : 0)));
    if ($cur_tier > $last_tier) {
        $new_tier = $cur_tier;
        my $k_tot = int($tot / 1000);
        my $k_cr_cost = sprintf('%.1f', ($tot * 0.05) / 1000);
        my $ttl_hint = ($cur->{model} =~ /hydrogen/i)
            ? " (`hydrogen` cache drops sharply after ~90s idle!)"
            : "";
        my $extra = $cur_tier >= 150
            ? " Resetting now via `/compact` or `/handoff` cuts remaining long-session tokens by ~60-70% and avoids the 200k+ cliff."
            : " Run `/compact` or `/handoff` at the next phase switch (especially before a >2m break).";
        push @notes, "**Token Saver:** Context reached **~${k_tot}k tokens** (~${k_cr_cost}k uncached-equiv/turn in cache reads)${ttl_hint}.${extra}";
    }
}

# 2. Severe cache drop >25% (suppress expected compaction checkpoints)
my $new_drop_ts = $last_drop_ts;
if (defined $prev && $prev->{cr} >= 8000 && $cur->{cr} < $prev->{cr} * 0.75 && $cur->{ts} ne $last_drop_ts) {
    my $is_compaction = ($cur->{cp} || $cur->{tot} < $prev->{tot} * 0.80);
    if (!$is_compaction) {
        $new_drop_ts = $cur->{ts};
        my $cause = 'prefix change or backend cache eviction';
        if ($prev->{model} ne '' && $cur->{model} ne '' && $prev->{model} ne $cur->{model}) {
            $cause = "model switch (`$prev->{model}` -> `$cur->{model}`)";
        } else {
            # Parse ISO timestamps if present to detect >=90s idle TTL cliff
            my sub iso_sec {
                my ($t) = @_;
                return undef unless $t =~ /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/;
                return (($3 * 86400) + ($4 * 3600) + ($5 * 60) + $6);
            }
            my ($t1, $t2) = (iso_sec($prev->{ts}), iso_sec($cur->{ts}));
            if (defined $t1 && defined $t2) {
                my $dt = $t2 - $t1;
                $cause = "idle TTL eviction (~${dt}s pause exceeded hot cache window)" if $dt >= 90;
            }
        }
        my $pk = int($prev->{cr} / 1000);
        my $ck = int($cur->{cr} / 1000);
        my $rk = int($cur->{in} / 1000);
        push @notes, "**Token Saver:** Prompt cache dropped (**${pk}k -> ${ck}k**, rebuilt **${rk}k** uncached) — likely ${cause}.";
    }
}

# Persist state only if changed
if ($new_tier != $last_tier || $new_drop_ts ne $last_drop_ts) {
    eval {
        if (!-d $state_dir) {
            require File::Path;
            File::Path::make_path($state_dir);
        }
        if (open my $sfh, '>', $state_file) {
            print $sfh "$new_tier\t$tot\t$new_drop_ts\n";
            close $sfh;
        }
    };
}

if (!@notes) {
    print '{}';
    exit 0;
}

my $md = join("\n\n", @notes);
print '{"injectSteps":[{"systemMessage":{"systemMessage":"","metadata":{"user_markdown":"' . json_escape($md) . '"}}}]}';
exit 0;
