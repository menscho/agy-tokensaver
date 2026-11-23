#!/usr/bin/env perl
use strict;

# Zero-module PreToolUse guard for write_file / write_to_file.
# Passes through immediately for new files, Append:true, and files <= 150 lines.
# Blocks ONLY two high-confidence waste/damage patterns (zero false positives):
#   1. Accidental truncation/wipeout: existing file > 150L, new content shrinks >= 40%
#      AND contains lazy omission placeholders ("// ... existing", "# ... unchanged", etc.).
#   2. Extreme surgical-overwrite waste: existing file > 350L AND >= 85% of lines identical.
#      LOG ONLY (v2.1): at PreToolUse time the output tokens are already spent, so a deny would
#      throw them away AND force a second (edit_file) generation. Prevention lives in AGENTS.md.

local $/;
my $raw = <STDIN>;
if (!defined $raw || $raw eq '') { print '{"decision":"allow"}'; exit 0; }

# Fast-path: Append=true or Overwrite not true -> allow immediately
if ($raw =~ /"Append"\s*:\s*true\b/) { print '{"decision":"allow"}'; exit 0; }
if ($raw !~ /"Overwrite"\s*:\s*true\b/) { print '{"decision":"allow"}'; exit 0; }

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

my $path = json_get_str($raw, 'Path') // json_get_str($raw, 'TargetFile') // '';
if ($path eq '') { print '{"decision":"allow"}'; exit 0; }
if ($path =~ m{^~([/\\].*)?$}) {
    my $home = $ENV{HOME} // $ENV{USERPROFILE} // '';
    $path = $home . ($1 // '') if $home ne '';
} elsif ($path !~ m{^(?:/|[A-Za-z]:[/\\]|\\\\)}) {
    my $cwd = json_get_str($raw, 'cwd') // '';
    $path = "$cwd/$path" if $cwd ne '';
}

# Brand-new file or missing file -> allow immediately
if (!-f $path) { print '{"decision":"allow"}'; exit 0; }

# Small existing file (< 4KB) -> allow immediately
my $old_sz = (stat($path))[7] // 0;
if ($old_sz < 4096) { print '{"decision":"allow"}'; exit 0; }

# Never block artifacts inside brain/ or generated lock/json/snapshot files
if ($path =~ m{/\.gemini/antigravity/brain/} || $path =~ /\.(?:json|md|txt|yaml|yml|toml|lock|snap|csv)$/i) {
    print '{"decision":"allow"}';
    exit 0;
}

open(my $fh, '<:raw', $path) or do { print '{"decision":"allow"}'; exit 0; };
my $old_content = '';
sysread($fh, $old_content, 512 * 1024);
close $fh;

my @old_lines = split(/\n/, $old_content);
my $old_cnt = scalar(@old_lines);
if ($old_cnt <= 150) { print '{"decision":"allow"}'; exit 0; }

my $new_content = json_get_str($raw, 'Content') // json_get_str($raw, 'CodeContent');
if (!defined $new_content) { print '{"decision":"allow"}'; exit 0; }

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
            time(), $detail, $action, length($new_content), 0, int($est_saved), json_escape($base)
        );
        if (open my $lfh, '>>', "$data_dir/events.jsonl") { print $lfh "$rec\n"; close $lfh; }
    };
}

# Case 1: Lazy truncation / accidental wipeout guard (>150L shrunk >=40% + placeholder markers)
if ($new_cnt <= $old_cnt * 0.60 &&
    $new_content =~ m{(?://|#|/\*|<!--)\s*\.\.\.\s*(?:existing|unchanged|rest|remaining|omitted|same as before|keep existing)}i) {
    log_write_event('blocked', 'lazy_truncation', int($old_sz / 4));
    print '{"decision":"deny","reason":"' . json_escape(
        "[token-saver] Blocked potential lazy truncation on '$base' (${old_cnt}L -> ${new_cnt}L with placeholder comment). Use edit_file for surgical changes without losing existing code."
    ) . '"}';
    exit 0;
}

# Case 2: Extreme full-file overwrite (>350L existing AND >=85% lines unchanged)
if ($old_cnt > 350 && $new_cnt > 250) {
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
    if ($overlap >= 0.85) {
        log_write_event('observed', 'huge_overwrite', 0);
    }
}

print '{"decision":"allow"}';
exit 0;
