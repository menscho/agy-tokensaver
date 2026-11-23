#!/usr/bin/env perl
use strict;

# Zero-module Perl CLI output compressor (stdlib only, no CPAN/external modules).
# Invariants:
# - Exact child exit code propagation + process-group SIGINT/SIGTERM forwarding (no orphans).
# - Raw UTF-8 byte I/O (immune to LANG=C / LC_ALL=C crashes).
# - Never alters or minifies real code hunks (git_diff / cat_large).
# - Two-tier net-savings guard: never emits output larger than clean input.
# - User-scoped temp spillback (0700 on Unix) whenever lines are trimmed.
# - Fail-open on any internal error.

binmode(STDOUT, ':raw');
binmode(STDERR, ':raw');

my $t0 = time();
my $cat = 'generic';
my $test_stdin = 0;
my $test_rc = 0;
my $orig_cmd = '';

# Parse CLI args: --cat <cat> [--test-stdin --test-rc N | -- <cmd>]
my $i = 0;
while ($i < @ARGV) {
    my $a = $ARGV[$i++];
    if ($a eq '--cat' && $i < @ARGV) {
        $cat = $ARGV[$i++];
    } elsif ($a eq '--test-stdin') {
        $test_stdin = 1;
    } elsif ($a eq '--test-rc' && $i < @ARGV) {
        $test_rc = int($ARGV[$i++]);
    } elsif ($a eq '--') {
        $orig_cmd = join(' ', @ARGV[$i .. $#ARGV]);
        last;
    }
}

my $TMP_BASE = $ENV{TMPDIR} || $ENV{TEMP} || $ENV{TMP} || '/tmp';
$TMP_BASE =~ s{[\\/]+$}{};
my $USER_TAG = ($^O eq 'MSWin32') ? ($ENV{USERNAME} || 'user') : $<;
$USER_TAG =~ s/[^A-Za-z0-9_-]/_/g;
my $SPILL_DIR = "$TMP_BASE/agy-tokensaver-$USER_TAG";
my $MAX_RAM   = 8 * 1024 * 1024; # 8 MB cap
# Antigravity natively shows tool output <= ~10.1KB in full and replaces larger output with a
# 9,896-byte head+tail preview + ~60-token truncation notice. Zero-loss rule: generic head/tail
# line dropping only happens on output the model would NOT have seen in full anyway.
my $NATIVE_CAP     = 9900;
my $NATIVE_PREVIEW = 9896 + 240;  # preview bytes + truncation notice (~60 tok)
my $NOTICE_BYTES   = 94;          # "A pre-tool hook changed the arguments ... Changed: CommandLine."
# Set whenever a transform drops or shortens content (not just line count) => forces spillback.
my $LOSSY = 0;
# Lines removed by provably lossless transforms (exact-duplicate merge, empty box borders).
my $LOSSLESS_RM = 0;

my $LOCKFILE_DIFF_RE = qr{(?:^|/)(?:(?:vendor|third_party)/[^/]+\.(?:js|mjs|cjs|css|json)|package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|poetry\.lock|composer\.lock|Gemfile\.lock|go\.sum|uv\.lock|bun\.lockb?|Pipfile\.lock|flake\.lock|Podfile\.lock|packages\.lock\.json|paket\.lock|mix\.lock|pubspec\.lock|Package\.resolved|gradle\.lockfile|conda-lock\.yml|pdm\.lock|pixi\.lock|renv\.lock|deno\.lock|vcpkg-lock\.json|conan\.lock|Chart\.lock|MODULE\.bazel\.lock|shard\.lock|[^/]+\.(?:min\.(?:js|mjs|cjs|css)|(?:bundle|chunk)\.(?:js|mjs|cjs|css)|map|ipynb|svg|snap|snapshot|golden|tsbuildinfo|tfstate(?:\.backup)?)|[^/]+(?:_pb2(?:_grpc)?\.pyi?|\.pb\.(?:go|gw\.go|cc|h|ts|rs|dart)|_pb\.js|_(?:gen|generated)\.go|\.(?:generated|gen)\.(?:ts|tsx|js)|\.g\.dart|\.freezed\.dart|\.mocks\.dart))$}i;

sub ensure_spill_dir {
    if (!-d $SPILL_DIR) {
        mkdir($SPILL_DIR, 0700);
    }
    # Security check: ensure owned by current UID
    my @st = stat($SPILL_DIR);
    return (-d $SPILL_DIR && @st && $st[4] == $<);
}

sub cleanup_spill_dir {
    return unless int(rand(16)) == 0;
    return unless opendir(my $dh, $SPILL_DIR);
    my $cutoff = time() - 86400;
    my @files;
    while (my $f = readdir($dh)) {
        next if $f eq '.' || $f eq '..';
        my $p = "$SPILL_DIR/$f";
        my @st = stat($p);
        next unless @st && -f $p;
        if ($st[9] < $cutoff) {
            unlink($p);
        } else {
            push @files, [$st[9], $p];
        }
    }
    closedir($dh);
    # Enforce max 200 files cap
    if (@files > 200) {
        @files = sort { $a->[0] <=> $b->[0] } @files;
        unlink($files[$_][1]) for (0 .. $#files - 200);
    }
}

sub write_spillback {
    my ($text) = @_;
    return '' unless ensure_spill_dir();
    cleanup_spill_dir();
    my $hex = sprintf('%06x', (time() ^ ($$ << 8) ^ int(rand(0xffffff))) & 0xffffff);
    my $fp = "$SPILL_DIR/$hex.log";
    if (open my $fh, '>:raw', $fp) {
        print $fh $text;
        close $fh;
        return $fp;
    }
    return '';
}

sub json_escape {
    my ($s) = @_;
    $s =~ s/\\/\\\\/g;
    $s =~ s/"/\\"/g;
    $s =~ s/\n/\\n/g;
    $s =~ s/\r/\\r/g;
    $s =~ s/\t/\\t/g;
    return $s;
}

sub log_telemetry {
    my ($category, $raw_bytes, $kept_bytes, $rc) = @_;
    eval {
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
        # Honest net savings vs. what Antigravity would natively have shown (capped preview),
        # minus the overwrite notice this wrap cost. May be negative (passthrough = -23 tok).
        my $native = $raw_bytes > $NATIVE_CAP ? $NATIVE_PREVIEW : $raw_bytes;
        my $net = $native - $kept_bytes - $NOTICE_BYTES;
        my $action = $kept_bytes < $raw_bytes ? 'compressed' : 'passthrough';
        my $rec = sprintf(
            '{"ts":%d,"source":"cmd_wrap","category":"%s","action":"%s","rc":%d,"raw_bytes":%d,"kept_bytes":%d,"est_tokens_saved":%d}',
            time(), json_escape($category), $action, int($rc), int($raw_bytes), int($kept_bytes), int($net / 4)
        );
        if (open my $fh, '>>', "$data_dir/events.jsonl") {
            print $fh "$rec\n";
            close $fh;
        }
    };
}

# --- Universal lossless terminal sanitizer ---
# $preserve_ws: 0 = full cleanup; 1 = verbatim code (git_diff/cat_large: keep CRLF, blank runs,
# trailing ws, base64); 2 = remote/raw text (ssh: normalize CRLF/progress bars only).
sub sanitize_terminal {
    my ($raw, $preserve_ws) = @_;
    # Strip ANSI CSI, OSC (incl. OSC 8 hyperlinks), DCS/SOS/PM/APC, charset shifts (\x1b(B), 2-byte escapes, C1 CSI
    $raw =~ s/\x1b\[[0-?]*[ -\/]*[@-~]//g;
    $raw =~ s/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)//g;
    $raw =~ s/\x1b[PX^_][^\x1b]*(?:\x1b\\|\x07)//g;
    $raw =~ s/\x1b[()*+][0-9A-Za-z]//g;
    $raw =~ s/\x1b[@-Z\\-_]//g;
    $raw =~ s/\x9b[0-?]*[ -\/]*[@-~]//g;
    # Resolve backspace overstrikes (bounded: contiguous \b runs were O(n^2) -> 12s at 40K)
    if (index($raw, "\x08") >= 0) {
        my $it = 0;
        1 while $it++ < 64 && $raw =~ s/[^\x08\n]\x08//g;
        $raw =~ s/\x08//g;
    }
    # Strip stray control chars (keep \t, \n, \r)
    $raw =~ s/[\x00-\x08\x0b\x0c\x0e-\x1f]//g;
    # Verbatim code mode: keep CRLF so copied diff/file content stays byte-exact
    return $raw if $preserve_ws == 1;
    # Normalize CRLF
    $raw =~ s/\r\n/\n/g;
    # Virtual line buffer for standalone \r progress bars
    if (index($raw, "\r") >= 0) {
        my @lines = split(/\n/, $raw, -1);
        for my $l (@lines) {
            if (index($l, "\r") >= 0) {
                my @parts = grep { $_ =~ /\S/ } split(/\r/, $l);
                $l = @parts ? $parts[-1] : '';
            }
        }
        $raw = join("\n", @lines);
    }
    # Scrub inline base64 data URIs & giant raw base64 blobs (>= 300 chars) unless git_diff/cat_large
    if (!$preserve_ws) {
        # Any scrub is lossy => $LOSSY forces a full-log spillback (keys/tokens stay recoverable)
        $LOSSY = 1 if $raw =~ s{(data:[\w\/+.-]+;base64,)([A-Za-z0-9+\/=]{140,})}{$1 . "<BASE64:" . length($2) . "B>"}ge;
        $LOSSY = 1 if $raw =~ s{((?<![A-Za-z0-9+\/])(?<!ssh-rsa )(?<!ssh-dss )[A-Za-z0-9+\/]{320,}={0,2})}{"<BASE64:" . length($1) . "B>"}ge;
        # Drop pure Braille spinner lines
        my @lines = split(/\n/, $raw, -1);
        my @out;
        my $blank_run = 0;
        for my $l (@lines) {
            $l =~ s/[ \t]+$//;
            next if $l =~ /^\xe2\xa0[\x80-\xbf]\s/; # UTF-8 Braille spinner U+2800..U+283F
            if ($l eq '') {
                $blank_run++;
                next if $blank_run > 1;
            } else {
                $blank_run = 0;
            }
            push @out, $l;
        }
        pop @out while @out && $out[-1] eq '';
        shift @out while @out && $out[0] eq '';
        $raw = join("\n", @out) . (@out ? "\n" : '');
    }
    return $raw;
}

# --- Shared lossless helpers ---
sub dedupe_consecutive {
    my ($lines) = @_;
    return [] unless @$lines;
    my @out;
    my ($prev, $cnt) = ($lines->[0], 1);
    for (my $k = 1; $k < @$lines; $k++) {
        if ($lines->[$k] eq $prev) {
            $cnt++;
        } else {
            if ($cnt > 2) { push @out, "$prev [x$cnt]"; $LOSSLESS_RM += $cnt - 1; }
            elsif ($cnt == 2) { push @out, $prev, $prev; }
            else { push @out, $prev; }
            ($prev, $cnt) = ($lines->[$k], 1);
        }
    }
    if ($cnt > 2) { push @out, "$prev [x$cnt]"; $LOSSLESS_RM += $cnt - 1; }
    elsif ($cnt == 2) { push @out, $prev, $prev; }
    else { push @out, $prev; }
    return \@out;
}

# Whitespace-only table compaction. Only touches lines that are unambiguously tabular so code,
# YAML block scalars, string literals and shell operators (`a || b`) are never altered.
my $BOX_V  = "\xe2\x94\x82";                                  # U+2502 │
my $BOX_RE = qr/^(?:\xe2\x94[\x80-\xbf]|\xe2\x95[\x80-\xac]|[-+=| ])+$/; # U+2500..U+256C borders only
sub collapse_table_padding {
    my ($lines) = @_;
    my @out;
    for my $l (@$lines) {
        my $lead = ($l =~ /^([ \t]+)/) ? $1 : '';
        my $body = substr($l, length($lead));
        if (length($body) >= 12 && $body =~ $BOX_RE) {
            # Box-drawing border row (┌─┬─┐ ├─┼─┤ └─┴─┘; no vertical data bars) carries no data -> drop
            if ($body =~ /\xe2[\x94\x95]/ && index($body, $BOX_V) < 0 && index($body, "\xe2\x95\x91") < 0) { $LOSSLESS_RM++; next; }
            $body =~ s/(?:-|=|\xe2\x94[\x80\x81]|\xe2\x95\x90){4,}/---/g;
            $body =~ s/ {3,}/ /g;
            push @out, $lead . $body;
            next;
        }
        # Box-drawn data row: │ a │ b │ -> | a | b |
        if (index($body, $BOX_V) == 0) {
            $body =~ s/\xe2\x94\x82/|/g;
            $body =~ s/ {2,}/ /g;
            push @out, $lead . $body;
            next;
        }
        # ASCII table row (starts AND ends with |): pad collapse is safe
        if ($body =~ /^\|.*\|$/ && ($body =~ tr/|//) >= 3) {
            $body =~ s/ {2,}/ /g;
            push @out, $lead . $body;
            next;
        }
        # Column-aligned CLI output (ps/df/docker ps/kubectl get): >=2 runs of 3+ spaces and no
        # code/quote/assignment characters.
        my $runs = () = $body =~ / {3,}/g;
        if ($runs >= 2 && $body !~ /["'`=(){};]/) {
            $body =~ s/ {3,}/  /g;
        }
        push @out, $lead . $body;
    }
    return \@out;
}

sub fold_stack_frames {
    my ($lines) = @_;
    my @out;
    my $n = scalar(@$lines);
    my $k = 0;
    my $folded = 0;
    my $fstart = 0;
    my sub flush_folded {
        if ($folded >= 2) {
            $LOSSY = 1;
            push @out, "    ... [$folded library frames folded]";
        }
        $folded = 0;
    }
    while ($k < $n) {
        my $l = $lines->[$k];
        # Python 2-3 line library frame
        if ($l =~ /^\s+File\s+"[^"]*(?:\/site-packages\/|\/dist-packages\/|\/lib\/python3\.\d+\/|\/_pytest\/|\/pluggy\/|<frozen )/i) {
            $fstart = $k if $folded == 0;
            $folded++;
            $k++;
            $k++ if $k < $n && $lines->[$k] =~ /^\s{4,}\S/;
            $k++ if $k < $n && $lines->[$k] =~ /^\s+[~^]+$/;
            next;
        }
        # Node/JS/Java/Ruby/.NET single-line library frame
        if ($l =~ /^\s+at\s+.*(?:\/node_modules\/|\\node_modules\\|node:internal\/|internal\/(?:modules|process|timers|streams)\/|java\.|javax\.|sun\.|jdk\.|org\.(?:junit|apache|springframework|gradle)\.|io\.netty\.|kotlin\.coroutines\.|System\.|Microsoft\.|Xunit\.|NUnit\.)/ ||
            $l =~ /^\s+\d+:\s+(?:std::|core::|alloc::|tokio::|__rust_)/) {
            $fstart = $k if $folded == 0;
            $folded++;
            $k++;
            $k++ if $k < $n && $lines->[$k] =~ /^\s+at\s+\/(?:rustc|cargo\/registry)\//;
            next;
        }
        # Go 2-line stdlib/mod frame
        if ($l =~ /^(?:runtime|testing)\./ && $k + 1 < $n && $lines->[$k + 1] =~ m{^\s+(?:/usr/local/go/src/|/go/pkg/mod/)}) {
            $fstart = $k if $folded == 0;
            $folded++;
            $k += 2;
            next;
        }
        if ($folded == 1) {
            # Keep a lone library frame verbatim (all of its 1-3 lines)
            push @out, @{$lines}[$fstart .. $k - 1];
            $folded = 0;
        } else {
            flush_folded();
        }
        push @out, $l;
        $k++;
    }
    if ($folded == 1) { push @out, @{$lines}[$fstart .. $n - 1]; $folded = 0; }
    flush_folded();
    return \@out;
}

sub dedupe_log_templates {
    my ($lines) = @_;
    return $lines if @$lines <= 25;
    my sub tmpl_key {
        my ($s) = @_;
        $s =~ s/\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?/<TS>/g;
        $s =~ s/\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+\s+\d{2}:\d{2}:\d{2}\b/<TS>/g;
        $s =~ s/\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b/<TS>/g;
        $s =~ s/\[\s*\d+\.\d+\]/[<TS>]/g;
        $s =~ s/\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b/<UUID>/g;
        $s =~ s/\b[0-9a-fA-F]{12,}\b/<HEX>/g;
        $s =~ s/\b\d+(?:\.\d+)?(?:ms|us|ns|s)\b/<DUR>/g;
        $s =~ s/\b(pid|req|trace|span|id)([=:\-])\s*[A-Za-z0-9_-]+/$1$2<ID>/ig;
        return $s;
    }
    my @out;
    my ($prev_k, $first_l, $last_l, $cnt) = ('', '', '', 0);
    my sub flush_run {
        return unless $cnt > 0;
        if ($cnt >= 3) {
            $LOSSY = 1;
            push @out, $first_l;
            my $tail_snip = length($last_l) > 100 ? substr($last_l, 0, 97) . '...' : $last_l;
            push @out, "  ... [x$cnt similar lines; last: $tail_snip]";
        } elsif ($cnt == 2) {
            push @out, $first_l, $last_l;
        } else {
            push @out, $first_l;
        }
    }
    for my $l (@$lines) {
        my $k = tmpl_key($l);
        if ($cnt > 0 && $k eq $prev_k) {
            $last_l = $l;
            $cnt++;
        } else {
            flush_run();
            ($prev_k, $first_l, $last_l, $cnt) = ($k, $l, $l, 1);
        }
    }
    flush_run();
    return \@out;
}

sub tail_weighted_cap {
    my ($lines, $head, $tail) = @_;
    $head //= 35;
    $tail //= 110;
    return $lines if @$lines <= $head + $tail + 5;
    # Zero-loss rule: never drop lines the model would natively have seen in full
    my $bytes = 0;
    $bytes += length($_) + 1 for @$lines;
    return $lines if $bytes <= $NATIVE_CAP;
    $LOSSY = 1;
    my $omit = @$lines - $head - $tail;
    return [
        @{$lines}[0 .. $head - 1],
        "... ($omit middle lines omitted; see full log below) ...",
        @{$lines}[-$tail .. -1]
    ];
}

# Head/tail cap that also keeps (in original order) middle lines carrying signal: errors,
# warnings, and section headers (=== X ===, ## X, [X]) so multi-section ssh/diagnostic scripts
# never lose a section's identity. Gaps are marked with omitted-line counts.
my $SIGNAL_RE = qr/\b(?:ERROR|FATAL|PANIC|CRITICAL|EXCEPTION|TRACEBACK|WARN(?:ING)?|FAIL(?:ED|URE)?|DENIED|REFUSED|TIMEOUT|Traceback)\b|^\s*(?:={3,}|-{3,}|#{2,}\s|\*{3,})\s*\S|^\s*\[[A-Za-z][^\]]{0,60}\]\s*$|exit (?:code|status)/i;
sub cap_with_signal {
    my ($lines, $head, $tail, $max_sig) = @_;
    my $n = scalar(@$lines);
    return $lines if $n <= $head + $tail + 5;
    my $bytes = 0;
    $bytes += length($_) + 1 for @$lines;
    return $lines if $bytes <= $NATIVE_CAP;
    $LOSSY = 1;
    my @keep_idx = grep { $lines->[$_] =~ $SIGNAL_RE } ($head .. $n - $tail - 1);
    @keep_idx = @keep_idx[0 .. $max_sig - 1] if @keep_idx > $max_sig;
    my @out = @{$lines}[0 .. $head - 1];
    my $prev = $head - 1;
    for my $i (@keep_idx, $n - $tail) {
        my $gap = $i - $prev - 1;
        push @out, "... ($gap lines omitted) ..." if $gap > 0;
        push @out, $lines->[$i] if $i < $n - $tail;
        $prev = $i;
    }
    push @out, @{$lines}[$n - $tail .. $n - 1];
    return \@out;
}

sub cap_line_len {
    my ($l, $max, $keep) = @_;
    return $l if length($l) <= $max;
    $LOSSY = 1;
    my $s = substr($l, 0, $keep);
    # Never split a UTF-8 multibyte sequence
    $s =~ s/[\xc0-\xff][\x80-\xbf]*\z//;
    return $s . ' ... [+' . (length($l) - length($s)) . ' chars]';
}

# --- Category Compressors ---

sub compress_test {
    my ($lines, $rc) = @_;
    my $pass_re = qr{(?:^\s*(?:PASSED|✓|✔|ok\s+\d+|test\s+\S+\s+\.\.\.\s+ok\b|---\s*PASS:|===\s*RUN\s+|Compiling\s+\S+|Downloading\s+\S+|Blocking waiting for file lock|PASS\s+\S+|collected\s+\d+\s+items|\.{10,}|[sSxX.]{4,}\s*(?:\[\s*\d+%\])?$|\S+\.py\s+[.sSxX]+\s*(?:\[\s*\d+%\])?$)|\s+PASSED(?:\s+\[\s*\d+%\])?$)};
    if ($rc == 0) {
        my @kept;
        for my $l (@$lines) {
            next if $l =~ $pass_re;
            next if $l =~ /^(?:platform |rootdir:|configfile:|plugins:|cachedir:)/;
            if ($l =~ /(?:\b\d+\s+(?:passed|failed|skipped|xfailed|xpassed|warnings?|errors?)\b|Test Suites?:|Tests?:|Snapshots?:|Time:|Ran \d+ tests?|test result:|ok\s+\S+\s+\d|BUILD SUCCESS|Summary|TOTAL|All files|WARN(?:ING)?\b|DEPRECATION\b|\|\s*\d+(?:\.\d+)?%)/i) {
                push @kept, $l;
            }
        }
        my $d = dedupe_consecutive(collapse_table_padding(\@kept));
        return @$d ? (@$d > 30 ? [@{$d}[-30 .. -1]] : $d) : [@{$lines}[(@$lines > 12 ? -12 : -@$lines) .. -1]];
    } else {
        my @kept;
        for my $l (@$lines) {
            next if $l =~ $pass_re && $l !~ /FAIL|ERROR|PANIC|ASSERT/i;
            push @kept, $l;
        }
        my $f = fold_stack_frames(dedupe_consecutive(\@kept));
        return tail_weighted_cap($f, 50, 130);
    }
}

sub compress_pkg {
    my ($lines, $rc) = @_;
    my $noise = qr{^\s*(?:Downloading\b|Unpacking\b|Using cached\b|Collecting\b|Resolving\b|Fetching\b|Preparing metadata|Building wheel|Installing collected|Requirement already satisfied|Get:\d+|Hit:\d+|Ign:\d+|Reading package lists|Building dependency tree|Reading state information|Setting up\b|Selecting previously unselected|Processing triggers|Compiling\s+\S+|Downloaded\s+\d+|Updating crates\.io|npm notice|packages? (?:are|is) looking for funding|run `npm fund`|\[notice\] A new release|==> Pouring|[|\/\-\\]\s*$|[#=>\-]{8,})}i;
    if ($rc == 0) {
        my @kept = grep {
            $_ !~ $noise &&
            $_ =~ /(?:added \d+|removed \d+|changed \d+|audited \d+|up to date|Successfully installed|Installed \d+|Upgraded|Resolved \d+|Prepared \d+|vulnerabilit|WARN(?:ING)?\b|ERR!|ERROR|Done in \d|Finished\b|\d+ upgraded)/i
        } @$lines;
        my $d = dedupe_consecutive(\@kept);
        return @$d ? (@$d > 25 ? [@{$d}[-25 .. -1]] : $d) : [@{$lines}[(@$lines > 10 ? -10 : -@$lines) .. -1]];
    } else {
        my @kept = grep { $_ !~ $noise || $_ =~ /err|fail|conflict|fatal/i } @$lines;
        return tail_weighted_cap(fold_stack_frames(dedupe_consecutive(\@kept)), 30, 100);
    }
}

sub compress_build {
    my ($lines, $rc) = @_;
    my $noise = qr{^\s*(?:Compiling\s+\S+|Downloading\s+\S+|Fresh\s+\S+|#\d+\s+(?:CACHED|DONE|transferring|load metadata|load build definition|\[internal\]|sha256:)|--->\s+(?:Using cache|Running in|[0-9a-f]{12})|>\s*Task\s+:\S+\s+(?:UP-TO-DATE|NO-SOURCE|FROM-CACHE)|\[INFO\]\s*(?:Downloading|Downloaded|---|Building|Scanning|Skipping|Copying)|make\[\d+\]:\s*(?:Entering|Leaving) directory|(?:gcc|g\+\+|clang|clang\+\+|cc|c\+\+|rustc|ar|ranlib)\s+.*(?:-c\s|-o\s)|\[\s*\d+%\]\s+(?:Building|Scanning|Linking))};
    if ($rc == 0) {
        my @kept = grep {
            $_ !~ $noise &&
            $_ =~ /(?:warning\b|warn\b|error\b|Finished\b|BUILD SUCCESS|Successfully (?:built|tagged)|writing image|naming to|dist\/|Compiled successfully|Built in\b|Route \(|Size\s+First Load)/i
        } @$lines;
        my $d = dedupe_consecutive(\@kept);
        return @$d ? tail_weighted_cap($d, 25, 40) : [@{$lines}[(@$lines > 12 ? -12 : -@$lines) .. -1]];
    } else {
        my @kept = grep { $_ !~ $noise || $_ =~ /error|fail|fatal|undefined/i } @$lines;
        return tail_weighted_cap(fold_stack_frames(dedupe_consecutive(\@kept)), 40, 120);
    }
}

sub compress_lint {
    my ($lines, $rc) = @_;
    my @clean;
    for my $l (@$lines) {
        next if $l =~ /^\s*(?:https?:\/\/\S+|[-=~─━═]{10,}|\|\s*|Checking\s+\S+|Compiling\s+\S+|Downloading\s+\S+|Fresh\s+\S+)\s*$/;
        next if $l =~ /^\s*=\s*help:\s*for further information visit https?:\/\/\S+\s*$/i;
        push @clean, $l;
    }
    my $d = dedupe_consecutive(\@clean);
    return $d if @$d <= 90;

    my (%seen, %supp, @out);
    for my $l (@$d) {
        if ($l eq '' || $l =~ /(?:Found \d+|problems?|errors?,\s*\d+\s*warnings?|checked \d+ files|Summary)/i) {
            push @out, $l;
            next;
        }
        my $key = $l;
        $key =~ s/^\s+//;
        $key =~ s/:\d+(?::\d+)?/:L/g;
        $seen{$key}++;
        if ($seen{$key} <= 5) {
            push @out, $l;
        } else {
            $supp{$key}++;
        }
    }
    if (%supp) {
        $LOSSY = 1;
        push @out, '--- Repeated diagnostics collapsed ---';
        my @top = sort { $supp{$b} <=> $supp{$a} } keys %supp;
        for my $k (@top[0 .. ($#top > 4 ? 4 : $#top)]) {
            my $sk = length($k) > 110 ? substr($k, 0, 107) . '...' : $k;
            push @out, "  (+$supp{$k} more) $sk";
        }
    }
    return tail_weighted_cap(\@out, 50, 80);
}

sub compress_git_diff {
    my ($lines, $rc) = @_;
    my (@out, @file_stats, @cur_lines);
    my ($cur_hdr, $cur_file) = (undef, '');

    my sub flush_blk {
        return unless defined $cur_hdr;
        my ($adds, $dels) = (0, 0);
        for my $x (@cur_lines) {
            $adds++ if substr($x, 0, 1) eq '+' && substr($x, 0, 3) ne '+++';
            $dels++ if substr($x, 0, 1) eq '-' && substr($x, 0, 3) ne '---';
        }
        push @file_stats, "  $cur_file (+$adds -$dels)";
        if ($cur_file =~ $LOCKFILE_DIFF_RE) {
            (my $hdr_clean = $cur_hdr) =~ s/\r$//;
            push @out, "$hdr_clean [omitted lockfile/generated diff: +$adds -$dels lines]";
        } else {
            push @out, $cur_hdr;
            for my $x (@cur_lines) {
                next if $x =~ /^index [0-9a-f]+\.\.[0-9a-f]+(?: \d+)?\r?$/;
                push @out, $x;
            }
        }
        ($cur_hdr, $cur_file, @cur_lines) = (undef, '', ());
    }

    for my $l (@$lines) {
        (my $lc = $l) =~ s/\r$//;
        if ($lc =~ /^diff --git (?:"?a\/(.+?)"?) (?:"?b\/(.+?)"?)$/) {
            flush_blk();
            $cur_hdr = $l;
            $cur_file = $2;
        } elsif (defined $cur_hdr) {
            push @cur_lines, $l;
        } else {
            push @out, $l;
        }
    }
    flush_blk();
    # No diff blocks (e.g. `git show <rev>:path` file content, commit message only): return verbatim
    return $lines unless @file_stats;
    my $omitted_lock = grep { / \[omitted lockfile\/generated diff: / } @out;
    $LOSSY = 1 if $omitted_lock;

    my $out_bytes = 0;
    $out_bytes += length($_) + 1 for @out;
    if (@out > 420 && $out_bytes > $NATIVE_CAP) {
        my @hdr = ("[Diff summary: " . scalar(@file_stats) . " files]");
        push @hdr, @file_stats[0 .. ($#file_stats > 29 ? 29 : $#file_stats)];
        push @hdr, "  ... (+" . (@file_stats - 30) . " more files)" if @file_stats > 30;
        push @hdr, '---';
        return [@hdr, @{tail_weighted_cap(\@out, 160, 160)}];
    }
    return \@out;
}

sub compress_git_log {
    my ($lines, $rc) = @_;
    return $lines if @$lines <= 45;
    # P0 invariant: if diff hunks are present (e.g. git log -p), route to git_diff instead!
    for my $l (@$lines) {
        return compress_git_diff($lines, $rc) if $l =~ /^diff --git /;
    }
    my $is_std = 0;
    for my $l (@{$lines}[0 .. ($#$lines > 9 ? 9 : $#$lines)]) {
        $is_std = 1 if $l =~ /^commit [0-9a-f]{7,40}/;
    }
    return tail_weighted_cap($lines, 40, 40) unless $is_std;

    my @commits;
    my ($sha, $author, $date, $subj) = ('', '', '', '');
    for my $l (@$lines) {
        if ($l =~ /^commit ([0-9a-f]{7,40})/) {
            push @commits, "$sha $date $author | $subj" if $sha ne '';
            $sha = substr($1, 0, 8);
            ($author, $date, $subj) = ('', '', '');
        } elsif ($l =~ /^Author:\s*(.+)$/) {
            my $a = $1; $a =~ s/\s*<[^>]+>//;
            $author = substr($a, 0, 24);
        } elsif ($l =~ /^Date:\s*(.+)$/) {
            my @p = split(/\s+/, $1);
            $date = @p >= 4 ? join(' ', @p[1..3]) : substr($1, 0, 16);
        } elsif ($l =~ /^    (.+)$/ && $subj eq '') {
            $subj = substr($1, 0, 100);
        }
    }
    push @commits, "$sha $date $author | $subj" if $sha ne '';
    $LOSSY = 1 if @commits;
    if (@commits > 60) {
        return [@commits[0 .. 49], "... (+" . (@commits - 50) . " older commits in full log)"];
    }
    return @commits ? \@commits : tail_weighted_cap($lines, 40, 40);
}

sub compress_git_net {
    my ($lines, $rc) = @_;
    my @kept = grep { $_ !~ /^(?:remote:\s*)?(?:Enumerating|Counting|Compressing|Writing|Receiving|Resolving|Unpacking)\s+(?:objects|deltas):/ } @$lines;
    my $d = dedupe_consecutive(\@kept);
    return @$d ? $d : [@{$lines}[(@$lines > 5 ? -5 : -@$lines) .. -1]];
}

sub compress_search {
    my ($lines, $rc) = @_;
    my (@grouped, %file_counts);
    my $cur_file = '';
    for my $l (@$lines) {
        # Cap minified single-line matches (> 360 chars)
        $l = cap_line_len($l, 360, 320);
        if ($l =~ /^([^\s:][^:]*):(\d+)([:-])(.*)$/ && $1 !~ /^\d+$/) {
            my ($f, $ln, $sep, $txt) = ($1, $2, $3, $4);
            $file_counts{$f}++;
            if ($f ne $cur_file) {
                push @grouped, "$f:";
                $cur_file = $f;
            }
            push @grouped, "  $ln$sep$txt";
        } else {
            $cur_file = '';
            push @grouped, $l;
        }
    }
    my $g_bytes = 0;
    $g_bytes += length($_) + 1 for @grouped;
    return cap_with_signal(\@grouped, 70, 35, 40) if !%file_counts;
    if (@grouped > 160 && $g_bytes > $NATIVE_CAP) {
        $LOSSY = 1;
        my @top = sort { $file_counts{$b} <=> $file_counts{$a} } keys %file_counts;
        my @hdr = ("[Search summary: " . scalar(@$lines) . " lines across " . scalar(@top) . " files]");
        for my $f (@top[0 .. ($#top > 14 ? 14 : $#top)]) {
            push @hdr, "  $f: $file_counts{$f}";
        }
        push @hdr, '---';
        return [@hdr, @{tail_weighted_cap(\@grouped, 70, 35)}];
    }
    return \@grouped;
}

sub compress_find {
    my ($lines, $rc) = @_;
    return $lines if @$lines <= 45;
    # Detect tree box-drawing output vs path list
    my $is_tree = ($lines->[0] !~ m{/} && @$lines > 2 && $lines->[1] =~ /[├└│|`-]/);
    return tail_weighted_cap($lines, 60, 40) if $is_tree;

    # Lossless directory grouping for 46..300 paths
    if (@$lines <= 300) {
        my (%by_dir, @dir_order);
        for my $l (@$lines) {
            next if $l eq '';
            my ($d, $f) = ($l =~ m{^(.+)/([^/]+)$}) ? ($1, $2) : ('.', $l);
            push @dir_order, $d unless exists $by_dir{$d};
            push @{$by_dir{$d}}, $f;
        }
        my @out;
        for my $d (@dir_order) {
            my $line = "$d/:";
            my $first = 1;
            for my $f (@{$by_dir{$d}}) {
                if (!$first && length($line) + length($f) > 236) {
                    push @out, $line;
                    $line = "  ";
                    $first = 1;
                }
                $line .= ($first ? ' ' : ', ') . $f;
                $first = 0;
            }
            push @out, $line;
        }
        return \@out if @out < @$lines * 0.75;
    }
    my $f_bytes = 0;
    $f_bytes += length($_) + 1 for @$lines;
    return $lines if $f_bytes <= $NATIVE_CAP;
    $LOSSY = 1;

    my %dir_cnt;
    for my $l (@$lines) {
        next unless $l =~ /\S/;
        my $k = $l;
        $k =~ s{^\./}{};
        my @p = split(m{/}, $k);
        my $grp = @p >= 2 ? "$p[0]/$p[1]" : $p[0];
        $dir_cnt{$grp}++;
    }
    my @top = sort { $dir_cnt{$b} <=> $dir_cnt{$a} } keys %dir_cnt;
    my @hdr = ("[Directory summary (" . scalar(@$lines) . " entries)]");
    for my $d (@top[0 .. ($#top > 14 ? 14 : $#top)]) {
        push @hdr, "  $d: $dir_cnt{$d}";
    }
    return [@hdr, '--- sample ---', @{tail_weighted_cap($lines, 35, 35)}];
}

sub compress_cat_large {
    my ($lines, $rc) = @_;
    my @capped = map { cap_line_len($_, 400, 360) } @$lines;
    return tail_weighted_cap(\@capped, 160, 40);
}

sub compress_http {
    my ($lines, $rc) = @_;
    my @kept;
    for my $l (@$lines) {
        # Strip curl/wget progress meters
        next if $l =~ /^\s*%\s+Total\s+%\s+Received/;
        next if $l =~ /^\s*Dload\s+Upload\s+Total/;
        next if $l =~ /^\s*\d+\s+\S+\s+\d+\s+\S+\s+\d+\s+\S+\s+\S+\s+\S+\s+(?:--:--:--|\d+:\d+:\d+)/;
        next if $l =~ /^\s*\d+K\s+[.\s]+\d+%/;
        next if $l =~ /^\s*\d+%\s+\[=*>*\s*\]/;
        push @kept, $l;
    }
    my $joined = join("\n", @kept);
    return tail_weighted_cap(\@kept, 60, 40) if $joined =~ /\A\s*[\[{]/;
    # Strip HTML script/style/svg/comments if HTML response > 4KB
    if (length($joined) > 4096 && $joined =~ /<(?:!doctype\s+html|html\b)/i) {
        $joined =~ s/<(script|style|svg)\b[^>]*>.*?<\/\1>//gis;
        $joined =~ s/<!--.*?-->//gs;
        $LOSSY = 1;
        my @hl = grep { /\S/ } map { s/[ \t]+/ /g; s/^\s+|\s+$//g; $_ } split(/\n/, $joined);
        return tail_weighted_cap(\@hl, 60, 40);
    }
    my @capped = map { cap_line_len($_, 500, 460) } @kept;
    return tail_weighted_cap(dedupe_consecutive(\@capped), 60, 40);
}

sub compress_archive {
    my ($lines, $rc) = @_;
    if ($rc == 0) {
        return $lines if @$lines <= 40;
        my $a_bytes = 0;
        $a_bytes += length($_) + 1 for @$lines;
        return $lines if $a_bytes <= $NATIVE_CAP;
        $LOSSY = 1;
        my %dirs;
        for my $l (@$lines) {
            my $p = $l; $p =~ s/^(?:inflating|extracting|creating|adding|x)\s*:?\s*//i;
            my ($d) = ($p =~ m{^([^/]+/)});
            $dirs{$d // '(root)'}++;
        }
        my @top = sort { $dirs{$b} <=> $dirs{$a} } keys %dirs;
        my @hdr = ("[Archive/sync summary (" . scalar(@$lines) . " lines)]");
        push @hdr, "  $_: $dirs{$_}" for @top[0 .. ($#top > 11 ? 11 : $#top)];
        return [@hdr, '--- head/tail ---', @{$lines}[0 .. 9], '...', @{$lines}[-10 .. -1]];
    } else {
        my @kept = grep { $_ !~ /^\s*(?:inflating|extracting|adding|creating):/i || $_ =~ /err|fail|warn|denied/i } @$lines;
        return tail_weighted_cap(dedupe_consecutive(\@kept), 20, 50);
    }
}

sub compress_iac {
    my ($lines, $rc) = @_;
    my @kept = grep {
        $_ !~ /^(?:.*:\s*(?:Refreshing state\.\.\.|Reading\.\.\.|Read complete after|Still creating|Still destroying|Still modifying)|skipping:\s*\[)/
    } @$lines;
    return tail_weighted_cap(dedupe_consecutive(\@kept), 50, 90);
}

sub compress_k8s_yaml {
    my ($lines, $rc) = @_;
    return $lines if @$lines <= 150;
    my @index;
    my ($kind, $name) = ('', '');
    for my $l (@$lines) {
        if ($l =~ /^kind:\s*(\S+)/) { $kind = $1; $name = ''; }
        elsif ($l =~ /^\s+name:\s*(\S+)/ && $kind ne '' && $name eq '') {
            $name = $1;
            push @index, "  $kind/$name";
            $kind = '';
        }
    }
    my @hdr = @index ? ("[Helm/K8s manifests (" . scalar(@index) . " resources)]", @index[0 .. ($#index > 29 ? 29 : $#index)], '---') : ();
    return [@hdr, @{tail_weighted_cap($lines, 50, 50)}];
}

sub total_bytes { my $b = 0; $b += length($_) + 1 for @{$_[0]}; return $b; }

sub compress_logs {
    my ($lines, $rc) = @_;
    # <= native cap: model would see every line -> lossless exact-duplicate merge only
    return dedupe_consecutive($lines) if total_bytes($lines) <= $NATIVE_CAP;
    my $dedup = dedupe_log_templates(fold_stack_frames($lines));
    return cap_with_signal($dedup, 25, 80, 50);
}

sub compress_ssh {
    my ($lines, $rc) = @_;
    # Remote output the model asked for: <= native cap stays complete (lossless transforms only)
    return collapse_table_padding(dedupe_consecutive($lines)) if total_bytes($lines) <= $NATIVE_CAP;
    my $t = collapse_table_padding(dedupe_log_templates(fold_stack_frames($lines)));
    return $rc != 0 ? cap_with_signal($t, 40, 110, 60) : cap_with_signal($t, 35, 85, 60);
}

sub compress_ps {
    my ($lines, $rc) = @_;
    my (@kept, $kthreads);
    $kthreads = 0;
    for my $l (@$lines) {
        if ($l =~ /\s+\[(?:kworker|ksoftirqd|rcu_|cpuhp|migration|kswapd|kcompactd|khugepaged|irq\/|watchdog|scsi_|ext4|jbd2|loop\d|kdevtmpfs|netns|kauditd|khungtaskd|oom_reaper|writeback|ksmd|kintegrityd|kblockd|blkcg|tpm_|ata_sff|md|edac|devfreq|kthrotld|acpi_|nvme)/) {
            $kthreads++;
            next;
        }
        $l = cap_line_len($l, 220, 217);
        push @kept, $l;
    }
    my $c = collapse_table_padding(\@kept);
    push @$c, "[+$kthreads kernel threads omitted]" if $kthreads > 0;
    return tail_weighted_cap($c, 45, 35);
}

sub compress_table {
    my ($lines, $rc) = @_;
    my @kept = grep { $_ !~ m{^/dev/loop\d+\s} } @$lines;
    my $c = collapse_table_padding(\@kept);
    return tail_weighted_cap($c, 50, 40);
}

sub compress_cloud {
    my ($lines, $rc) = @_;
    return tail_weighted_cap($lines, 50, 60) if @$lines && $lines->[0] =~ /^\s*[\[{]/;
    my $t = collapse_table_padding(dedupe_log_templates(fold_stack_frames($lines)));
    my @capped = map { cap_line_len($_, 400, 360) } @$t;
    return tail_weighted_cap(\@capped, 50, 60);
}

sub compress_chain {
    my ($lines, $rc) = @_;
    my $noise = qr{^\s*(?:Compiling\s+\S+|Downloading\s+\S+|Unpacking\s+\S+|Using cached\b|Fresh\s+\S+|#\d+\s+CACHED|>\s*Task\s+:\S+\s+UP-TO-DATE|PASSED\b|\S+\.py\s+[.sSxX]+\s*(?:\[\s*\d+%\])?$|---\s*PASS:|===\s*RUN\s+)};
    my @kept = grep { $_ !~ $noise || $_ =~ /FAIL|ERROR|WARN|PANIC/i } @$lines;
    my $f = fold_stack_frames(dedupe_consecutive(\@kept));
    return tail_weighted_cap($f, 50, 110);
}

my %DISPATCH = (
    test      => \&compress_test,
    pkg       => \&compress_pkg,
    build     => \&compress_build,
    lint      => \&compress_lint,
    git_diff  => \&compress_git_diff,
    git_log   => \&compress_git_log,
    git_net   => \&compress_git_net,
    search    => \&compress_search,
    find      => \&compress_find,
    cat_large => \&compress_cat_large,
    http      => \&compress_http,
    archive   => \&compress_archive,
    iac       => \&compress_iac,
    k8s_yaml  => \&compress_k8s_yaml,
    logs      => \&compress_logs,
    ssh       => \&compress_ssh,
    ps        => \&compress_ps,
    table     => \&compress_table,
    cloud     => \&compress_cloud,
    chain     => \&compress_chain,
);

sub process_output {
    my ($raw, $rc, $total_raw_bytes, $existing_spill) = @_;
    my $preserve_ws = ($cat eq 'git_diff' || $cat eq 'cat_large') ? 1 : ($cat eq 'ssh' ? 2 : 0);
    my $clean = sanitize_terminal($raw, $preserve_ws);
    # If sanitizing itself was lossy (base64 scrub), every fallback must return unscrubbed text
    my $safe = $LOSSY ? sanitize_terminal($raw, 2) : $clean;
    my $clean_bytes = length($clean);
    my @clean_lines = split(/\n/, $clean);
    my $n_clean = scalar(@clean_lines);

    # Fast passthrough for tiny outputs unless table/search/git_net can compress losslessly
    if ($clean_bytes <= 900 && $n_clean <= 20 && $cat !~ /^(?:search|table|ps|git_net)$/) {
        log_telemetry($cat, $total_raw_bytes, length($safe), $rc);
        return $safe;
    }

    my $fn = $DISPATCH{$cat};
    my $comp_lines = $fn ? $fn->(\@clean_lines, $rc) : tail_weighted_cap(dedupe_consecutive(\@clean_lines), 35, 110);
    my $comp = join("\n", @$comp_lines);
    $comp .= "\n" if $clean =~ /\n\z/ && $comp ne '' && $comp !~ /\n\z/;

    # Lossy = any content dropped/shortened beyond provably lossless merges => must spill full log
    my $dropped_lines = ($LOSSY || @$comp_lines < $n_clean - $LOSSLESS_RM || defined $existing_spill) ? 1 : 0;

    # Two-tier net-savings threshold:
    # (a) Pure lossless transform (no dropped content, no spillback): emit if saves >= 32 bytes
    if (!$dropped_lines) {
        if ($clean_bytes - length($comp) >= 32) {
            log_telemetry($cat, $total_raw_bytes, length($comp), $rc);
            return $comp;
        }
        log_telemetry($cat, $total_raw_bytes, length($safe), $rc);
        return $safe;
    }

    # (b) Omitting transform (with spillback): verify net savings >= 160 bytes & >= 15% AFTER spill note
    my $est_note_len = 60;
    if (($clean_bytes - (length($comp) + $est_note_len)) < 160 || (length($comp) + $est_note_len) >= $clean_bytes * 0.85) {
        if (!defined $existing_spill) {
            log_telemetry($cat, $total_raw_bytes, length($safe), $rc);
            return $safe;
        }
    }

    # Never drop content without a recoverable full log
    my $spill_path = defined $existing_spill ? $existing_spill : write_spillback($safe);
    if ($spill_path eq '') {
        log_telemetry($cat, $total_raw_bytes, length($safe), $rc);
        return $safe;
    }
    my $l_info = defined $existing_spill
        ? int($total_raw_bytes / 1024) . "KB->" . scalar(@$comp_lines) . "L"
        : "${n_clean}L->" . scalar(@$comp_lines) . "L";
    my $note = "[$l_info; full: $spill_path]\n";

    my $final = $comp . ($comp ne '' && $comp !~ /\n\z/ ? "\n" : '') . $note;
    if (!defined $existing_spill && length($final) >= length($safe)) {
        unlink($spill_path);
        log_telemetry($cat, $total_raw_bytes, length($safe), $rc);
        return $safe;
    }

    log_telemetry($cat, $total_raw_bytes, length($final), $rc);
    return $final;
}

# --- Test stdin mode ---
if ($test_stdin) {
    binmode(STDIN, ':raw');
    local $/;
    my $in = <STDIN> // '';
    my $out = eval { process_output($in, $test_rc, length($in), undef) };
    print($@ ? $in : $out);
    exit $test_rc;
}

exit 0 if $orig_cmd eq '';

# --- Execute child in its own process group with SIGINT/SIGTERM forwarding ---
pipe(my $rd, my $wr) or exit 1;
# If stdin is a terminal, the child must stay in the foreground process group: a new pgrp would
# get SIGTTIN (hang forever) on any prompt/read (apt w/o -y, terraform apply, ssh/sudo password).
my $own_pgrp = -t STDIN ? 0 : 1;
my $pid = fork();
if (!defined $pid) {
    exit 1;
}
if ($pid == 0) {
    # Child: new process group, redirect stdout+stderr to pipe, exec bash -c
    setpgrp(0, 0) if $own_pgrp;
    close($rd);
    open(STDOUT, '>&', $wr) or _exit(127);
    open(STDERR, '>&', $wr) or _exit(127);
    close($wr);
    exec('bash', '-c', $orig_cmd);
    exit 127;
}
close($wr);

my $caught_sig = 0;
my $forward_sig = sub {
    my ($signame) = @_;
    $caught_sig = ($signame eq 'INT' ? 2 : $signame eq 'HUP' ? 1 : 15);
    kill($own_pgrp ? "-$signame" : $signame, $pid);
};
$SIG{INT}  = $forward_sig;
$SIG{TERM} = $forward_sig;
$SIG{HUP}  = $forward_sig;

my $buf = '';
my $head_buf = '';
my $tail_buf = '';
my $total_read = 0;
my ($ov_fh, $ov_path);

while (1) {
    my $chunk = '';
    my $n = sysread($rd, $chunk, 65536);
    if (!defined $n) {
        next if $!{EINTR};
        last;
    }
    last if $n == 0;
    $total_read += $n;
    if (!defined $ov_fh && $total_read <= $MAX_RAM) {
        $buf .= $chunk;
    } else {
        if (!defined $ov_fh) {
            if (ensure_spill_dir()) {
                my $hex = sprintf('%06x', (time() ^ ($$ << 8) ^ int(rand(0xffffff))) & 0xffffff);
                $ov_path = "$SPILL_DIR/ov-$hex.log";
                if (open($ov_fh, '>:raw', $ov_path)) {
                    print $ov_fh $buf;
                }
            }
            # Snap head buffer to newline boundary (~160KB)
            my $cut = rindex($buf, "\n", 163840);
            $head_buf = $cut > 0 ? substr($buf, 0, $cut + 1) : substr($buf, 0, 163840);
            $tail_buf = '';
            $buf = '';
        }
        print $ov_fh $chunk if defined $ov_fh;
        $tail_buf .= $chunk;
        if (length($tail_buf) > 200000) {
            my $drop = length($tail_buf) - 163840;
            my $nl = index($tail_buf, "\n", $drop);
            $tail_buf = ($nl >= 0) ? substr($tail_buf, $nl + 1) : substr($tail_buf, $drop);
        }
    }
}
close($rd);
close($ov_fh) if defined $ov_fh;

waitpid($pid, 0);
my $status = $?;
my $rc = ($status == -1) ? 1 : (($status & 127) ? (128 + ($status & 127)) : ($status >> 8));
$rc = 128 + $caught_sig if $caught_sig && $rc == 0;

my $assembled = defined $ov_path
    ? ($head_buf . "\n... [8MB RAM cap reached; full stream saved to $ov_path] ...\n" . $tail_buf)
    : $buf;

my $out = eval { process_output($assembled, $rc, $total_read, $ov_path) };
print($@ ? $assembled : $out);
STDOUT->flush() if STDOUT->can('flush');
exit $rc;
