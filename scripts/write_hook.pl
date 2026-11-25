#!/usr/bin/env perl
use strict;
use warnings;

# PreToolUse guard for write_file / write_to_file.
# Passes through immediately for new files, Append:true, and files <= 120 lines.
# Blocks ONLY accidental truncation/wipeout (>150L shrunk >=40% with lazy omission
# placeholders). Large full-file overwrites (>120L, >=75% unchanged) are logged for
# a 0-LLM-token UI nudge without wasting already-generated output tokens.

my $ALLOW = '{"decision":"allow"}';
binmode(STDIN);
my $raw = '';
my $MAX = 2 * 1024 * 1024;
while (length($raw) < $MAX + 1) {
    my $chunk = '';
    my $n = read(STDIN, $chunk, $MAX + 1 - length($raw));
    last if !defined $n || $n <= 0;
    $raw .= $chunk;
}
if ($raw eq '' || length($raw) > $MAX) { print $ALLOW; exit 0; }

# Conservative fast-path before loading JSON::PP
if (index($raw, 'Overwrite') < 0 && index($raw, '\\u') < 0) {
    print $ALLOW;
    exit 0;
}

{
    my $d = $0; $d =~ s{[^/\\]*$}{}; $d = '.' if $d eq ''; $d =~ s{[/\\]+$}{};
    unshift @INC, "$d/lib";
}
require TsJson;

my $doc = TsJson::decode_hook_input($raw);
if (!defined $doc) { print $ALLOW; exit 0; }
if (TsJson::hooks_disabled()) { print $ALLOW; exit 0; }

my $ti = TsJson::tool_input($doc);
if (TsJson::get_bool($ti, 'Append')) { print $ALLOW; exit 0; }
if (!TsJson::get_bool($ti, 'Overwrite')) { print $ALLOW; exit 0; }

my $path = TsJson::get_str($ti, 'Path', 'TargetFile') // '';
if ($path eq '') { print $ALLOW; exit 0; }
if ($path =~ m{^~([/\\].*)?$}) {
    my $home = $ENV{HOME} // $ENV{USERPROFILE} // '';
    $path = $home . ($1 // '') if $home ne '';
} elsif ($path !~ m{^(?:/|[A-Za-z]:[/\\]|\\\\)}) {
    my $cwd = TsJson::get_str($doc, 'cwd') // TsJson::get_str($ti, 'cwd') // '';
    $path = "$cwd/$path" if $cwd ne '';
}

# Brand-new file or missing file -> allow immediately
if (!-f $path) { print $ALLOW; exit 0; }

# Small existing file (< 4KB) -> allow immediately
my $old_sz = (stat($path))[7] // 0;
if ($old_sz < 4096) { print $ALLOW; exit 0; }

# Never block artifacts inside brain/ or generated lock/json/snapshot files
if ($path =~ m{/\.gemini/antigravity/brain/} || $path =~ /\.(?:json|md|txt|yaml|yml|toml|lock|snap|csv)$/i) {
    print $ALLOW;
    exit 0;
}

open(my $fh, '<:raw', $path) or do { print $ALLOW; exit 0; };
my $old_content = '';
sysread($fh, $old_content, 512 * 1024);
close $fh;

my @old_lines = split(/\n/, $old_content);
my $old_cnt = scalar(@old_lines);
if ($old_cnt <= 120) { print $ALLOW; exit 0; }

my $new_content = TsJson::get_str($ti, 'Content', 'CodeContent');
if (!defined $new_content) { print $ALLOW; exit 0; }

my @new_lines = split(/\n/, $new_content);
my $new_cnt = scalar(@new_lines);
my $base = $path; $base =~ s{^.*[/\\]}{};

sub log_write_event {
    my ($action, $detail, $est_saved) = @_;
    eval {
        my $plugin_root = $ENV{PLUGIN_ROOT};
        if (!$plugin_root) {
            my $s = $0; $s = "$ENV{PWD}/$s" if $s !~ m{^/} && $ENV{PWD};
            $s =~ s{/[^/]+/[^/]+$}{}; $plugin_root = $s;
        }
        my $agy_pd = ($ENV{HOME} // '') . "/.gemini/antigravity/plugin_data";
        my $data_dir = $ENV{PLUGIN_DATA} || (-d $agy_pd ? "$agy_pd/token-saver" : "$plugin_root/.data");
        if (!-d $data_dir) { require File::Path; File::Path::make_path($data_dir); }
        my $rec = sprintf(
            '{"ts":%d,"source":"write_guard","category":"%s","action":"%s","raw_bytes":%d,"kept_bytes":%d,"est_tokens_saved":%d,"target":"%s"}',
            time(), $detail, $action, length($new_content), 0, int($est_saved), TsJson::json_escape($base)
        );
        if (open my $lfh, '>>', "$data_dir/events.jsonl") { print $lfh "$rec\n"; close $lfh; }
    };
}

# Case 1: Lazy truncation / accidental wipeout guard (>150L shrunk >=40% + placeholder markers)
if ($new_cnt <= $old_cnt * 0.60 &&
    $new_content =~ m{(?://|#|/\*|<!--)\s*\.\.\.\s*(?:existing|unchanged|rest|remaining|omitted|same as before|keep existing)}i) {
    log_write_event('blocked', 'lazy_truncation', int($old_sz / 4));
    print '{"decision":"deny","reason":"' . TsJson::json_escape(
        "[token-saver] Blocked potential lazy truncation on '$base' (${old_cnt}L -> ${new_cnt}L with placeholder comment). Use edit_file for surgical changes without losing existing code."
    ) . '"}';
    exit 0;
}

# Case 2: Large full-file overwrite (>120L existing AND >=75% lines unchanged) ->
# Allow (tokens already generated) but log + queue a 0-LLM-token UI nudge for post_invocation_alerts.pl.
if ($old_cnt > 120 && $new_cnt > 100) {
    my %old_freq;
    for my $l (@old_lines) { $old_freq{$l}++; }
    my $matched = 0;
    for my $l (@new_lines) {
        if ($old_freq{$l}) {
            $old_freq{$l}--;
            $matched++;
        }
    }
    my $denom = $old_cnt > $new_cnt ? $old_cnt : $new_cnt;
    my $overlap = $matched / ($denom || 1);
    if ($overlap >= 0.75) {
        log_write_event('observed', 'huge_overwrite', 0);
        eval {
            my $cid = TsJson::get_str($doc, 'conversationId') // '';
            if ($cid ne '') {
                my $pr = $ENV{PLUGIN_ROOT} || do { my $s = $0; $s =~ s{/[^/]+/[^/]+$}{}; $s };
                my $agy_pd = ($ENV{HOME} // '') . "/.gemini/antigravity/plugin_data";
                my $dd = $ENV{PLUGIN_DATA} || (-d $agy_pd ? "$agy_pd/token-saver" : "$pr/.data");
                my $adir = "$dd/alerts";
                if (!-d $adir) { require File::Path; File::Path::make_path($adir); }
                my $scid = $cid; $scid =~ s/[^A-Za-z0-9_-]/_/g;
                if (open my $afh, '>', "$adir/$scid.huge_write") {
                    print $afh "$base\t$matched\t$old_cnt\n";
                    close $afh;
                }
            }
        };
    }
}

print $ALLOW;
exit 0;
