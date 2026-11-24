#!/usr/bin/env perl
use strict;
use warnings;
use Fcntl qw(O_WRONLY O_CREAT O_TRUNC);

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

our $t0 = time();
our $cat = 'generic';
my $test_stdin = 0;
my $test_rc = 0;
my $orig_cmd = '';
my $pwsh_src = '';   # '' | 'stdin' | 'b64'  (PowerShell mode, Windows)
my $pwsh_b64 = '';

# Parse CLI args: --cat <cat> [--test-stdin --test-rc N | -- <cmd> | --pwsh-stdin | --pwsh-b64 B64]
my $i = 0;
while ($i < @ARGV) {
    my $a = $ARGV[$i++];
    if ($a eq '--cat' && $i < @ARGV) {
        $cat = $ARGV[$i++];
    } elsif ($a eq '--test-stdin') {
        $test_stdin = 1;
    } elsif ($a eq '--test-rc' && $i < @ARGV) {
        $test_rc = int($ARGV[$i++]);
    } elsif ($a eq '--pwsh-stdin') {
        $pwsh_src = 'stdin';
    } elsif ($a eq '--pwsh-b64' && $i < @ARGV) {
        $pwsh_src = 'b64';
        $pwsh_b64 = $ARGV[$i++];
    } elsif ($a eq '--pwsh-test') {
        $pwsh_src = 'test';   # --test-stdin with PowerShell-mode output rules
    } elsif ($a eq '--') {
        $orig_cmd = join(' ', @ARGV[$i .. $#ARGV]);
        last;
    }
}

my $TMP_BASE = $ENV{TMPDIR} || $ENV{TEMP} || $ENV{TMP} || '/tmp';
$TMP_BASE =~ s{[\\/]+$}{};
my $USER_TAG = ($^O eq 'MSWin32') ? ($ENV{USERNAME} || 'user') : $<;
$USER_TAG =~ s/[^A-Za-z0-9_-]/_/g;
our $SPILL_DIR = $ENV{AGY_TS_SPILL_DIR} || "$TMP_BASE/agy-tokensaver-$USER_TAG";
# PowerShell mode (Windows host): the model reads spill files with Windows tools, so pointers
# must be native Windows paths (Git/Cygwin perl sees /tmp/... or /c/...).
my $PWSH_MODE = ($pwsh_src ne '' || $cat =~ /^(?:pwsh|script)$/) ? 1 : 0;
sub shown_path {
    my ($p) = @_;
    return $p unless $PWSH_MODE;
    if ($^O ne 'MSWin32' && $p =~ m{^/}) {
        my $w = eval { Cygwin::posix_to_win_path($p) };
        return $w if defined $w && $w ne '';
    }
    $p =~ s{/}{\\}g if $p =~ /^(?:[A-Za-z]:|\\\\)/;
    return $p;
}
my $MAX_RAM   = 8 * 1024 * 1024; # 8 MB cap
# Antigravity natively shows tool output <= ~10.1KB in full and replaces larger output with a
# 9,896-byte head+tail preview + ~60-token truncation notice. Zero-loss rule: generic head/tail
# line dropping only happens on output the model would NOT have seen in full anyway.
our $NATIVE_CAP     = 9900;
our $NATIVE_PREVIEW = 9896 + 240;  # preview bytes + truncation notice (~60 tok)
our $NOTICE_BYTES   = 94;          # "A pre-tool hook changed the arguments ... Changed: CommandLine."
# Set whenever a transform drops or shortens content (not just line count) => forces spillback.
our $LOSSY = 0;
# Lines removed by provably lossless transforms (exact-duplicate merge, empty box borders).
our $LOSSLESS_RM = 0;

our $LOCKFILE_DIFF_RE = qr{(?:^|/)(?:(?:vendor|third_party)/[^/]+\.(?:js|mjs|cjs|css|json)|package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|poetry\.lock|composer\.lock|Gemfile\.lock|go\.sum|uv\.lock|bun\.lockb?|Pipfile\.lock|flake\.lock|Podfile\.lock|packages\.lock\.json|paket\.lock|mix\.lock|pubspec\.lock|Package\.resolved|gradle\.lockfile|conda-lock\.yml|pdm\.lock|pixi\.lock|renv\.lock|deno\.lock|vcpkg-lock\.json|conan\.lock|Chart\.lock|MODULE\.bazel\.lock|shard\.lock|[^/]+\.(?:min\.(?:js|mjs|cjs|css)|(?:bundle|chunk)\.(?:js|mjs|cjs|css)|map|ipynb|svg|snap|snapshot|golden|tsbuildinfo|tfstate(?:\.backup)?)|[^/]+(?:_pb2(?:_grpc)?\.pyi?|\.pb\.(?:go|gw\.go|cc|h|ts|rs|dart)|_pb\.js|_(?:gen|generated)\.go|\.(?:generated|gen)\.(?:ts|tsx|js)|\.g\.dart|\.freezed\.dart|\.mocks\.dart))$}i;


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

my $SCRIPT_DIR = do {
    my $d = $0;
    $d =~ s{[^/\\]*$}{};
    $d = '.' if $d eq '';
    $d =~ s{[/\\]+$}{};
    $d = "./$d" if $d !~ m{^(?:/|\\|[A-Za-z]:|\.{1,2}[/\\])};
    $d;
};
do "$SCRIPT_DIR/lib/TsSpill.pl" or die "failed to load TsSpill.pl: " . ($@ || $!);

my %_LOADED_FAM;
sub load_compressor_family {
    my ($fam) = @_;
    return 1 if $_LOADED_FAM{$fam};
    $_LOADED_FAM{$fam} = 1;
    my $fp = "$SCRIPT_DIR/compressors/$fam.pl";
    my $ok = do $fp;
    die "failed to load $fp: " . ($@ || $! || 'unknown') unless $ok;
    return 1;
}

my %CAT_FAMILY = (
    test      => ['test',  'compress_test'],
    pkg       => ['test',  'compress_pkg'],
    build     => ['build', 'compress_build'],
    lint      => ['build', 'compress_lint'],
    git_diff  => ['git',   'compress_git_diff'],
    git_log   => ['git',   'compress_git_log'],
    git_net   => ['git',   'compress_git_net'],
    search    => ['log',   'compress_search'],
    find      => ['log',   'compress_find'],
    cat_large => ['log',   'compress_cat_large'],
    http      => ['log',   'compress_http'],
    archive   => ['log',   'compress_archive'],
    iac       => ['log',   'compress_iac'],
    k8s_yaml  => ['log',   'compress_k8s_yaml'],
    logs      => ['log',   'compress_logs'],
    ssh       => ['ssh',   'compress_ssh'],
    ps        => ['log',   'compress_ps'],
    table     => ['log',   'compress_table'],
    cloud     => ['log',   'compress_cloud'],
    chain     => ['log',   'compress_chain'],
    pwsh      => ['pwsh',  'compress_pwsh'],
    script    => ['pwsh',  'compress_pwsh'],
);

sub dispatch_compressor {
    my ($c, $lines, $rc) = @_;
    return $lines if $c eq 'raw';
    load_compressor_family('common');
    if (my $spec = $CAT_FAMILY{$c}) {
        load_compressor_family($spec->[0]);
        no strict 'refs';
        return &{$spec->[1]}($lines, $rc);
    }
    return tail_weighted_cap(dedupe_consecutive($lines), 35, 110);
}

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
    if ($clean_bytes <= 900 && $n_clean <= 20 && $cat !~ /^(?:search|table|ps|git_net|pwsh)$/) {
        log_telemetry($cat, $total_raw_bytes, length($safe), $rc);
        return $safe;
    }

    my $comp_lines = dispatch_compressor($cat, \@clean_lines, $rc);
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

    # Never drop content without a byte-exact recoverable raw log
    my $spill_path = defined $existing_spill ? $existing_spill : write_spillback($raw);
    if ($spill_path eq '') {
        log_telemetry($cat, $total_raw_bytes, length($safe), $rc);
        return $safe;
    }
    my $l_info = defined $existing_spill
        ? int($total_raw_bytes / 1024) . "KB->" . scalar(@$comp_lines) . "L"
        : "${n_clean}L->" . scalar(@$comp_lines) . "L";
    # squeez-inspired ids_preserved: surface unique UUIDs / ERR_* codes / URLs omitted from $comp
    # so the model never hallucinates dropped IDs or spends an extra read_file turn.
    my $ids_str = '';
    if (!defined $existing_spill && length($safe) <= 524288 && $cat ne 'git_diff') {
        my (%in_comp, %seen_id, @omitted_ids);
        $in_comp{$_} = 1 for ($comp =~ /\b(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|ERR_[A-Z0-9_]{3,32}|https?:\/\/[^\s"'<>)\]]{8,90})\b/g);
        for my $id ($safe =~ /\b(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|ERR_[A-Z0-9_]{3,32}|https?:\/\/[^\s"'<>)\]]{8,90})\b/g) {
            next if $in_comp{$id} || $seen_id{$id}++;
            push @omitted_ids, $id;
            last if @omitted_ids >= 5;
        }
        if (@omitted_ids) {
            my $joined_ids = join(', ', @omitted_ids);
            $ids_str = "ids: $joined_ids; " if length($joined_ids) <= 140;
        }
    }
    my $note = "[$l_info; ${ids_str}full: " . shown_path($spill_path) . "]\n";

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

# Base64 (of the UTF-8 script) as written by cmd_hook_core.pl for non-ASCII commands
sub b64_decode {
    my ($s) = @_;
    $s =~ tr{A-Za-z0-9+/}{}cd;
    $s =~ tr{A-Za-z0-9+/}{ -_};
    my $uu = '';
    while ($s =~ /(.{1,60})/gs) { $uu .= chr(32 + int(length($1) * 3 / 4)) . $1 . "\n"; }
    return unpack('u', $uu);
}

my ($rd, $wr, $pid, $own_pgrp);
my $pipe_open = 0;
if ($pwsh_src eq 'stdin' || $pwsh_src eq 'b64') {
    # --- PowerShell mode: run the model's exact script text in a child Windows PowerShell ---
    my $script;
    if ($pwsh_src eq 'stdin') {
        binmode(STDIN, ':raw');
        local $/;
        $script = <STDIN> // '';
        $script =~ s/\A\xef\xbb\xbf//; # strip UTF-8 BOM PowerShell 5.1 may prepend when piping strings
        $script =~ s/\r?\n\z//;        # the newline PowerShell appends when piping a string
    } else {
        $script = b64_decode($pwsh_b64);
        $script =~ s/\A\xef\xbb\xbf//;
    }
    exit 0 if $script !~ /\S/;
    # The script travels in an env var (no command-line quoting/path-conversion can touch it) and
    # is run by Invoke-Expression at top level (-EncodedCommand would turn stderr into CLIXML).
    # Bootstrap = the encoding prelude Antigravity prepends to every command (UTF-8 console and
    # native pipes) + progress bars off (never captured output). Trailer: exit status of the LAST
    # statement as `powershell -Command` reports it, but keeping a native's own exit code.
    # (`| Out-Default` = the one formatter a -Command script uses; it is flushed before `exit`,
    # which would otherwise discard table output the formatter is still buffering.)
    $ENV{AGY_TS_PS1} = $script
        . "\n\$global:__agy_rc = if (-not \$?) { if (\$LASTEXITCODE -is [int] -and \$LASTEXITCODE -ne 0) { \$LASTEXITCODE } else { 1 } } else { 0 }\n";
    my $boot = "try { \$OutputEncoding = [System.Text.UTF8Encoding]::new(\$false); [Console]::OutputEncoding = \$OutputEncoding } catch {}; "
             . "\$ProgressPreference = 'SilentlyContinue'; \$__agy_ts = \$env:AGY_TS_PS1; Remove-Item Env:AGY_TS_PS1; "
             . "Invoke-Expression \$__agy_ts | Out-Default; if (\$null -eq \$global:__agy_rc) { exit 1 }; exit \$global:__agy_rc";
    # Child stdout is a pipe, not the console: keep Python's UTF-8 output and line ordering
    $ENV{PYTHONIOENCODING} = 'utf-8' unless defined $ENV{PYTHONIOENCODING};
    $ENV{PYTHONUNBUFFERED} = '1' unless defined $ENV{PYTHONUNBUFFERED};
    my $sysroot = $ENV{SystemRoot} || $ENV{SYSTEMROOT} || $ENV{windir} || $ENV{WINDIR} || 'C:\\Windows';
    my $ps = "$sysroot\\System32\\WindowsPowerShell\\v1.0\\powershell.exe";
    $ps = 'powershell.exe' unless -f $ps;
    my @ps_args = ('-NoLogo', '-NoProfile', '-NonInteractive', '-Command', $boot);
    $own_pgrp = 0;
    if ($^O eq 'MSWin32') {
        $pid = open($rd, '-|', qq{"$ps" -NoLogo -NoProfile -NonInteractive -Command "$boot" 2>&1 <NUL}) or exit 1;
        binmode($rd);
        $pipe_open = 1;
    } else {
        pipe($rd, $wr) or exit 1;
        $pid = fork();
        exit 1 unless defined $pid;
        if ($pid == 0) {
            close($rd);
            open(STDIN, '<', '/dev/null');
            open(STDOUT, '>&', $wr) or _exit(127);
            open(STDERR, '>&', $wr) or _exit(127);
            close($wr);
            exec { $ps } $ps, @ps_args;
            exit 127;
        }
        close($wr);
    }
} else {
exit 0 if $orig_cmd eq '';

# --- Execute child in its own process group with SIGINT/SIGTERM forwarding ---
pipe($rd, $wr) or exit 1;
# If stdin is a terminal, the child must stay in the foreground process group: a new pgrp would
# get SIGTTIN (hang forever) on any prompt/read (apt w/o -y, terraform apply, ssh/sudo password).
$own_pgrp = -t STDIN ? 0 : 1;
$pid = fork();
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
    # RTK ANSI-free env hygiene: prevent color escapes & interactive pagers at the source
    $ENV{NO_COLOR}  = '1'    unless defined $ENV{NO_COLOR};
    $ENV{CLICOLOR}  = '0'    unless defined $ENV{CLICOLOR};
    $ENV{GIT_PAGER} = 'cat'  unless defined $ENV{GIT_PAGER};
    exec('bash', '-c', $orig_cmd);
    exit 127;
}
close($wr);
}

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
                if (!-l $ov_path && sysopen($ov_fh, $ov_path, O_WRONLY | O_CREAT | O_TRUNC, 0600)) {
                    binmode($ov_fh, ':raw');
                    chmod(0600, $ov_path) unless $^O eq 'MSWin32';
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
my $status;
if ($pipe_open) { close($rd); $status = $?; }   # MSWin32 piped open: close() reaps the child
else { close($rd); waitpid($pid, 0); $status = $?; }
close($ov_fh) if defined $ov_fh;
my $rc = ($status == -1) ? 1 : (($status & 127) ? (128 + ($status & 127)) : ($status >> 8));
$rc = 128 + $caught_sig if $caught_sig && $rc == 0;

my $assembled = defined $ov_path
    ? ($head_buf . "\n... [8MB RAM cap reached; full stream saved to " . shown_path($ov_path) . "] ...\n" . $tail_buf)
    : $buf;

my $out = eval { process_output($assembled, $rc, $total_read, $ov_path) };
print($@ ? $assembled : $out);
STDOUT->flush() if STDOUT->can('flush');
exit $rc;
