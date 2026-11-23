#!/usr/bin/env perl
use strict;

# Zero-module PreToolUse anti-bloat guard for read_file / view_file.
# - Resolves relative paths via cwd, expands ~, checks symlink targets.
# - Supports ByteOffset (2.0) and ContentOffset (IDE view_file).
# - Tier 1: blocks unbounded lockfiles, minified/single-line bundles, sourcemaps, binaries,
#   .ipynb (>12KB), .har/.heapsnapshot/.cpuprofile/.sarif, generated protobuf/ORM/Dart/OpenAPI,
#   .snap, .tsbuildinfo, compile_commands.json, lcov.info, .tfstate*, sqlite DBs — with
#   ts_inspect.py hints.
# - Tier 2: transparent auto-cap via overwrite (0 retry turns!) for unbounded & partial-bound
#   (StartLine-only / EndLine-only / span >400) large reads, spillback output.txt logs,
#   multi-line .svg >20KB, and 16-48KB size+line-count hybrid (strictly verifying est_lines
#   exceeds cap + 80 to beat the ~26-token overwrite notice).
# - Honest telemetry: caps raw_bytes at 46,080 (read_file per-call limit).

my $raw = do { local $/; <STDIN> };
if (!defined $raw || $raw eq '') { print '{"decision":"allow"}'; exit 0; }

# Explicit ByteOffset / ContentOffset continuation -> pass through immediately
if ($raw =~ /"(?:ByteOffset|ContentOffset)"\s*:\s*[1-9]\d*/) {
    print '{"decision":"allow"}';
    exit 0;
}

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

sub json_get_int {
    my ($src, $key) = @_;
    return int($1) if $src =~ /"\Q$key\E"\s*:\s*"?(-?\d+)"?/;
    return undef;
}

my $path = json_get_str($raw, 'Path');
$path = json_get_str($raw, 'AbsolutePath') unless defined $path && $path ne '';
if (!defined $path || $path eq '') { print '{"decision":"allow"}'; exit 0; }

# Expand ~ and resolve relative paths against cwd (handles Unix /, Windows C:\, and UNC \\)
if ($path =~ m{^~([/\\].*)?$}) {
    my $home = $ENV{HOME} // $ENV{USERPROFILE} // '';
    $path = $home . ($1 // '') if $home ne '';
} elsif ($path !~ m{^(?:/|[A-Za-z]:[/\\]|\\\\)}) {
    my $cwd = json_get_str($raw, 'cwd') // '';
    $path = "$cwd/$path" if $cwd ne '';
}

if (!-f $path) { print '{"decision":"allow"}'; exit 0; }

# Resolve symlink target name for trap matching
my $check_path = $path;
if (-l $path) {
    my $target = readlink($path);
    if (defined $target && $target ne '') {
        if ($target !~ m{^(?:/|[A-Za-z]:[/\\]|\\\\)}) {
            my $dir = $path;
            $dir =~ s{[/\\][^/\\]+$}{};
            $target = "$dir/$target";
        }
        $check_path = $target;
    }
}

# Multimedia / document formats natively handled by read_file (NO svg — read_file returns raw XML!)
if ($check_path =~ /\.(?:png|jpe?g|gif|webp|ico|bmp|tiff?|pdf|mp4|mov|webm|avi|mkv|mp3|wav|ogg|m4a|flac|aac)$/i) {
    # Image-burst guard for long sessions (>3 images AND transcript >150KB):
    # Keeps 1-3 direct images & short-lived vision-scout subagents 100% untouched.
    if ($check_path =~ /\.(?:png|jpe?g|gif|webp)$/i) {
        my $cid = json_get_str($raw, 'conversationId') // '';
        my $tx  = json_get_str($raw, 'transcriptPath') // '';
        if ($cid ne '' && $tx ne '' && -f $tx && ((stat($tx))[7] // 0) > 153600) {
            my $pr = $ENV{PLUGIN_ROOT};
            if (!$pr) { my $s = $0; $s = "$ENV{PWD}/$s" if $s !~ m{^/} && $ENV{PWD}; $s =~ s{/[^/]+/[^/]+$}{}; $pr = $s; }
            my $agy_pd = ($ENV{HOME} // '') . "/.gemini/antigravity/plugin_data";
            my $dd = $ENV{PLUGIN_DATA} || (-d $agy_pd ? "$agy_pd/token-saver" : "$pr/.data");
            my $scid = $cid; $scid =~ s/[^A-Za-z0-9_-]/_/g;
            my $idir = "$dd/reads";
            my $ifile = "$idir/$scid.imgcnt";
            my $cnt = 0;
            if (-f $ifile && open(my $ifh, '<', $ifile)) { $cnt = int(<$ifh> // 0); close $ifh; }
            $cnt++;
            if ($cnt > 3) {
                my $cs_py = "$pr/scripts/contact_sheet.py";
                my $msg = "[token-saver] Image #${cnt} in a long session (>150KB transcript; each image replays ~1.5K tok/turn). Delegate multi-image inspection to `vision-scout` (Flash) or pack frames with: python3 $cs_py <dir_or_files>";
                print '{"decision":"deny","reason":"' . json_escape($msg) . '"}';
                exit 0;
            }
            eval {
                if (!-d $idir) { require File::Path; File::Path::make_path($idir); }
                if (open(my $ofh, '>', $ifile)) { print $ofh "$cnt\n"; close $ofh; }
            };
        }
    }
    print '{"decision":"allow"}';
    exit 0;
}

my @st = stat($path);
if (!@st) { print '{"decision":"allow"}'; exit 0; }
my $size  = $st[7];
my $mtime = $st[9];

my $base = $check_path;
$base =~ s{^.*[/\\]}{};

my $start = json_get_int($raw, 'StartLine');
my $end   = json_get_int($raw, 'EndLine');
my $has_both = (defined $start && defined $end && $end >= $start);
my $span     = $has_both ? ($end - $start + 1) : 0;

my $plugin_root = $ENV{PLUGIN_ROOT};
if (!$plugin_root) {
    my $script = $0;
    $script = "$ENV{PWD}/$script" if $script !~ m{^/} && $ENV{PWD};
    $script =~ s{/[^/]+/[^/]+$}{};
    $plugin_root = $script;
}
1 while $plugin_root =~ s{/\./}{/};
# (renamed from inspect.py: that name shadowed Python's stdlib `inspect` and crashed stats.py)
my $inspect_py = "$plugin_root/scripts/ts_inspect.py";

sub esc_sh {
    my ($s) = @_;
    $s =~ s/'/'\\''/g;
    return "'$s'";
}

my $READ_MAX_BYTES   = 46080;
my $READ_MAX_LINES   = 800;
# Conservative ceiling below 46,080B (accounting for line-number formatting) before
# assuming a line was delivered verbatim without read_file byte-truncation.
my $SAFE_DELIV_BYTES = 40000;
my $MAX_SCAN_BYTES   = 2 * 1024 * 1024;
my $OVERWRITE_NOTICE = 94;

# Measures actual bytes of a requested line range (capped at read_file's 800L / 46,080B limits)
# and computes the last line guaranteed not to have been byte-truncated.
# Returns ($verified, $slice_bytes, $safe_end, $full_file).
sub measure_line_range {
    my ($req_s, $req_e) = @_;
    my $s = (defined $req_s && $req_s >= 1) ? int($req_s) : 1;
    my $has_e = defined $req_e;
    my $e = $has_e ? int($req_e) : ($s + $READ_MAX_LINES - 1);
    $e = $s + $READ_MAX_LINES - 1 if ($e - $s + 1) > $READ_MAX_LINES;
    return (0, 0, undef, 0) if $e < $s;
    if ($size > $MAX_SCAN_BYTES && $s > 1) {
        return (0, $READ_MAX_BYTES, undef, 0);
    }
    open(my $mfh, '<:raw', $path) or return (0, 0, undef, 0);
    my ($ln, $slice_bytes, $hit_trunc, $scanned) = (0, 0, 0, 0);
    my $safe_end = undef;
    while (my $line = <$mfh>) {
        $ln++;
        my $len = length($line);
        $scanned += $len;
        if ($ln < $s) {
            if ($scanned > $MAX_SCAN_BYTES) {
                close $mfh;
                return (0, $READ_MAX_BYTES, undef, 0);
            }
            next;
        }
        last if $ln > $e;
        $slice_bytes += $len;
        my $eff_fmt = $slice_bytes + ($ln - $s + 1) * 12;
        if (!$hit_trunc && $eff_fmt <= $SAFE_DELIV_BYTES) {
            $safe_end = $ln;
        } else {
            $hit_trunc = 1;
        }
        if ($slice_bytes >= $READ_MAX_BYTES) {
            $slice_bytes = $READ_MAX_BYTES;
            $hit_trunc = 1;
            last;
        }
    }
    my $reached_eof = eof($mfh) ? 1 : 0;
    close $mfh;
    my $full_file = ($s == 1 && !$has_e && $reached_eof && !$hit_trunc && $ln <= $READ_MAX_LINES) ? 1 : 0;
    return (1, $slice_bytes, $safe_end, $full_file);
}

sub log_event {
    my ($action, $kept_bytes, $detail, $raw_override) = @_;
    eval {
        my $agy_pd = ($ENV{HOME} // $ENV{USERPROFILE} // '') . "/.gemini/antigravity/plugin_data";
        my $data_dir = $ENV{PLUGIN_DATA} || (-d $agy_pd ? "$agy_pd/token-saver" : "$plugin_root/.data");
        if (!-d $data_dir) {
            require File::Path;
            File::Path::make_path($data_dir);
        }
        my $eff_raw = defined $raw_override ? int($raw_override) : ($size > $READ_MAX_BYTES ? $READ_MAX_BYTES : $size);
        $eff_raw = $READ_MAX_BYTES if $eff_raw > $READ_MAX_BYTES;
        $eff_raw = 0 if $eff_raw < 0;
        my $kept = int($kept_bytes // 0);
        my $saved = $eff_raw - $kept;
        my $cid = json_get_str($raw, 'conversationId') // '';
        my $rec = sprintf(
            '{"ts":%d,"session_id":"%s","source":"read_guard","category":"%s","action":"%s","raw_bytes":%d,"kept_bytes":%d,"est_tokens_saved":%d,"file_bytes":%d,"target":"%s"}',
            time(), json_escape($cid), json_escape($detail), json_escape($action),
            $eff_raw, $kept, int($saved / 4), $size, json_escape($base)
        );
        if (open my $fh, '>>', "$data_dir/events.jsonl") {
            print $fh "$rec\n";
            close $fh;
        }
    };
}

sub deny {
    my ($cat, $msg, $raw_override) = @_;
    if (!defined $raw_override && (defined $start || defined $end)) {
        my ($ok, $sb) = measure_line_range($start, $end);
        $raw_override = $sb if $ok;
    }
    log_event('blocked', length($msg), $cat, $raw_override);
    print '{"decision":"deny","reason":"' . json_escape($msg) . '"}';
    exit 0;
}

sub cap_overwrite {
    my ($cat, $new_start, $new_end, $est_kept) = @_;
    my $eff_s = defined $new_start ? int($new_start) : (defined $start ? int($start) : 1);
    my $eff_e = int($new_end);
    check_and_record_read($eff_s, $eff_e, 0);
    my $raw_ov;
    if (defined $start || defined $end) {
        my ($ok, $sb) = measure_line_range($start, $end);
        $raw_ov = $sb if $ok;
    }
    log_event('capped', int($est_kept) + $OVERWRITE_NOTICE, $cat, $raw_ov);
    my $ov = '';
    if (defined $new_start) {
        $ov = sprintf('"StartLine":%d,"EndLine":%d', int($new_start), int($new_end));
    } else {
        $ov = sprintf('"EndLine":%d', int($new_end));
    }
    print '{"decision":"allow","overwrite":{' . $ov . '}}';
    exit 0;
}

my $kb = sprintf('%.1f', $size / 1024);
my $eff_tok = int(($size > 46080 ? 46080 : $size) / 4);
my $sh_path = esc_sh($path);

# One 4KB head read shared by Tier 0 and the small-file fast path
my ($head_n, $head_nul) = (0, 0);
if ($size > 0 && open(my $hfh, '<:raw', $path)) {
    my $hb = '';
    $head_n = sysread($hfh, $hb, 4096) // 0;
    close $hfh;
    $head_nul = ($head_n > 0 && index($hb, "\0") >= 0) ? 1 : 0;
}

# --- Tier 0: Non-media binary extensions (block at ANY size, even bounded) ---
# Ambiguous extensions (.db/.dat/.bin/.a are often plain text) are only blocked with a NUL byte.
if ($base =~ /\.(?:sqlite3?|db)$/i && $head_nul) {
    deny('sqlite', "[token-saver] Blocked read of SQLite DB '$base' (${kb}KB). Run: python3 $inspect_py sqlite $sh_path");
}
if ($base =~ /\.(?:pyc|pyo|wasm|so|dylib|dll|exe|o|class|jar|zip|tar|gz|tgz|bz2|xz|zst|7z|rar|parquet|arrow|feather|pkl|pickle|npy|npz|pt|pth|onnx|safetensors|h5|woff2?|ttf|otf|eot|xlsx?|docx|pptx)$/i ||
    ($base =~ /\.(?:a|bin|dat)$/i && $head_nul)) {
    deny('binary', "[token-saver] Blocked read of binary file '$base' (${kb}KB). Use targeted CLI tools (file/strings/python3) instead of read_file.");
}

# Fast-path allow for very small normal files (<= 4KB) unless binary/lockfile/minified
if ($size <= 4096) {
    if ($head_nul) {
        deny('binary', "[token-saver] Blocked read of binary file '$base' (${kb}KB). Use file/strings/python3 instead.");
    }
    print '{"decision":"allow"}';
    exit 0;
}

# --- Compaction-safe duplicate & superset-slice guard (files > 4KB, within <= 16 steps, same epoch) ---
# Only records line ranges guaranteed to fit below read_file's 46,080B / 800L truncation ceiling,
# so lines truncated from an earlier read are never falsely blocked.
my $dup_step = json_get_int($raw, 'stepIdx');
my $dup_cid  = json_get_str($raw, 'conversationId') // '';
sub check_and_record_read {
    my ($eff_s, $eff_e, $record_only) = @_;
    return unless $size > 4096 && defined $dup_step && $dup_cid ne '';
    eval {
        my $agy_pd = ($ENV{HOME} // $ENV{USERPROFILE} // '') . "/.gemini/antigravity/plugin_data";
        my $dd = $ENV{PLUGIN_DATA} || (-d $agy_pd ? "$agy_pd/token-saver" : "$plugin_root/.data");
        my $scid = $dup_cid; $scid =~ s/[^A-Za-z0-9_-]/_/g;
        my $ep_file = "$dd/alerts/$scid.epoch";
        my $epoch = 0;
        if (-f $ep_file && open(my $efh, '<', $ep_file)) { $epoch = int(<$efh> // 0); close $efh; }

        my $rdir = "$dd/reads";
        my $rfile = "$rdir/$scid.tsv";
        my $sk = defined $eff_s ? int($eff_s) : '';
        my $ek = defined $eff_e ? int($eff_e) : '';
        my @hist;
        my ($dup_delta, $dup_rng);
        if (-f $rfile && open(my $rfh, '<', $rfile)) {
            while (my $rl = <$rfh>) {
                chomp $rl;
                my ($rp, $rs, $re, $rm, $rsz, $rst, $rep) = split(/\t/, $rl);
                next unless defined $rep;
                if (!$record_only && $rp eq $path && $rm == $mtime && $rsz == $size && $rep == $epoch) {
                    my $delta = $dup_step - int($rst);
                    if ($delta > 0 && $delta <= 16) {
                        my $covered = 0;
                        # Prior verified full-file read (recorded as "".."" only when entire file fit safely)
                        if ($rs eq '' && $re eq '' && $size <= $SAFE_DELIV_BYTES) {
                            $covered = 1;
                        }
                        # Exact match on a verified bounded range
                        elsif ($rs ne '' && $re ne '' && $rs eq $sk && $re eq $ek) {
                            $covered = 1;
                        }
                        # Prior verified bounded range [rs..re] strictly covers requested bounded range [sk..ek]
                        elsif ($rs ne '' && $re ne '' && $sk ne '' && $ek ne '' && int($rs) <= int($sk) && int($re) >= int($ek)) {
                            $covered = 1;
                        }
                        if ($covered) {
                            $dup_delta = $delta;
                            $dup_rng = ($rs ne '' || $re ne '') ? ":$rs-$re" : '';
                        }
                    }
                }
                push @hist, $rl;
            }
            close $rfh;
        }
        if (defined $dup_delta) {
            my $req_rng = ($sk ne '' || $ek ne '') ? ":$sk-$ek" : '';
            my ($mok, $slice_b) = measure_line_range($eff_s, $eff_e);
            my $raw_ov = $mok ? $slice_b : ($size > $READ_MAX_BYTES ? $READ_MAX_BYTES : $size);
            my $req_kb = sprintf('%.1f', $raw_ov / 1024);
            deny('duplicate_read', "[token-saver] Unchanged slice '$base$req_rng' (${req_kb}KB) is already in context from read '$base$dup_rng' ${dup_delta} steps ago. Reuse it directly without re-reading.", $raw_ov);
        }
        # Record ONLY the portion guaranteed not to be truncated by read_file's 46,080B / 800L limits.
        my ($mok, $slice_b, $safe_end, $full_file) = measure_line_range($eff_s, $eff_e);
        return unless $mok;
        my ($rec_s, $rec_e);
        if ($full_file) {
            ($rec_s, $rec_e) = ('', '');
        } elsif (defined $safe_end) {
            my $rs_eff = defined $eff_s && $eff_s >= 1 ? int($eff_s) : 1;
            return if $safe_end < $rs_eff;
            ($rec_s, $rec_e) = ($rs_eff, $safe_end);
        } else {
            return;
        }
        push @hist, join("\t", $path, $rec_s, $rec_e, $mtime, $size, $dup_step, $epoch);
        @hist = @hist[-20 .. -1] if @hist > 20;
        if (!-d $rdir) { require File::Path; File::Path::make_path($rdir); }
        if (open(my $wfh, '>', $rfile)) { print $wfh join("\n", @hist) . "\n"; close $wfh; }
    };
}

# Fast-path allow for small normal files (4KB..12KB) unless data file >8KB (capped below) or binary
if ($size <= 12288 && !($size > 8192 && $base =~ /\.(?:json|jsonl|ndjson|ya?ml|csv|tsv|sql|xml|log|html|htm|lcov|diff|patch|out|err)$/i)) {
    if ($head_nul) {
        deny('binary', "[token-saver] Blocked read of binary file '$base' (${kb}KB). Use file/strings/python3 instead.");
    }
    check_and_record_read($start, $end, 0);
    print '{"decision":"allow"}';
    exit 0;
}

# --- Single 16KB peek (O(1) syscall) for binary, minified, base64, and line density ---
my ($peek_n, $peek_nl, $max_line, $has_nul, $has_b64, $ws_on_max) = (0, 0, 0, 0, 0, 0);
if (open(my $fh, '<:raw', $path)) {
    my $buf = '';
    $peek_n = sysread($fh, $buf, 16384) // 0;
    close $fh;
    if ($peek_n > 0) {
        $has_nul = (index($buf, "\0") >= 0) ? 1 : 0;
        $peek_nl = ($buf =~ tr/\n//);
        $has_b64 = ($buf =~ /data:[^;]{1,40};base64,[A-Za-z0-9+\/=]{500,}/) ? 1 : 0;
        for my $l (split(/\r?\n|\r/, $buf, -1)) {
            my $len = length($l);
            if ($len > $max_line) {
                $max_line = $len;
                $ws_on_max = ($l =~ tr/ \t//);
            }
        }
    }
}

# Binary NUL check (block even when bounded!)
if ($has_nul) {
    deny('binary', "[token-saver] Blocked read of binary file '$base' (${kb}KB). Use file/strings/python3 instead.");
}

# --- Tier 1a: Rich structured formats BEFORE generic single-line check (so exact inspect.py hint is emitted!) ---
if ($base =~ /\.ipynb$/i && (!$has_both || $span > 120)) {
    deny('ipynb', "[token-saver] Blocked raw read of notebook '$base' (${kb}KB; JSON/base64 bloat). Run: python3 $inspect_py ipynb $sh_path");
}
if ($base =~ /\.svg$/i && $size > 20480 && (!$has_both || $span > 80)) {
    deny('svg', "[token-saver] Blocked raw read of large SVG '$base' (${kb}KB). Run: python3 $inspect_py svg $sh_path (or pass StartLine/EndLine <=80).");
}
if ($base =~ /\.har$/i && (!$has_both || $span > 100)) {
    deny('har', "[token-saver] Blocked raw read of HAR '$base' (${kb}KB). Run: python3 $inspect_py har $sh_path");
}

# Single-line minified / data-URI bomb check (prose-safe). Exempt: narrow bounded reads (the 16KB
# peek says nothing about the requested lines) and spillback logs (already capped below).
my $is_prose = ($base =~ /\.(?:md|mdx|rst|adoc|tex|txt)$/i);
my $is_spillback = ($check_path =~ m{/\.system_generated/steps/[^/]+/output\.txt$} || $check_path =~ m{(?:^|[\\/])agy-tokensaver-[^\\/]+[\\/]});
if ($size > 15360 && !$is_spillback && !($has_both && $span <= 150)) {
    if ($has_b64) {
        deny('base64_blob', "[token-saver] Blocked read of '$base' (${kb}KB) containing inline base64 data URI. Use grep or python3 to inspect non-blob parts.");
    }
    if ($is_prose) {
        if ($max_line > 6000 || ($max_line > 2500 && ($ws_on_max / $max_line) < 0.05)) {
            deny('single_line_minified', "[token-saver] Blocked read of single-line/dense file '$base' (${kb}KB). Use grep/python3 to extract needed parts.");
        }
    } elsif ($max_line > 2200) {
        deny('single_line_minified', "[token-saver] Blocked read of single-line/minified file '$base' (${kb}KB, max line ${max_line}B). Extract needed keys/snippets with python3/grep or inspect.py.");
    }
}

# --- Tier 1b: Other domain-specific traps ---
if ($base =~ /\.(?:heapsnapshot|cpuprofile|sarif|drawio|excalidraw|geojson|topojson|kml|gpx|graphml)$/i && $size > 16384 && (!$has_both || $span > 100)) {
    deny('artifact_dump', "[token-saver] Blocked unbounded read of large artifact '$base' (${kb}KB). Use python3 $inspect_py json $sh_path or grep.");
}

# 1d. Lockfiles (25+ ecosystems)
if ($base =~ /^(?:package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|poetry\.lock|composer\.lock|Gemfile\.lock|go\.sum|uv\.lock|bun\.lockb?|Pipfile\.lock|flake\.lock|Podfile\.lock|packages\.lock\.json|paket\.lock|mix\.lock|pubspec\.lock|Package\.resolved|gradle\.lockfile|conda-lock\.yml|pdm\.lock|pixi\.lock|renv\.lock|deno\.lock|vcpkg-lock\.json|conan\.lock|Chart\.lock|MODULE\.bazel\.lock|shard\.lock)$/i) {
    if (!$has_both || $span > 150) {
        deny('lockfile', "[token-saver] Blocked unbounded read of lockfile '$base' (${kb}KB, ~${eff_tok} tok). Inspect the manifest, grep for a package, or pass StartLine/EndLine <=150.");
    }
}

# 1e. Minified bundles, sourcemaps, build/coverage/tfstate artifacts
if ($base =~ /\.(?:min\.(?:js|mjs|cjs|css|html|json)|(?:bundle|chunk)\.(?:js|mjs|cjs|css)|(?:js|mjs|cjs|css)?\.map|tsbuildinfo|tfstate(?:\.backup)?)$/i ||
    $base =~ /^(?:compile_commands\.json|lcov\.info|coverage\.xml|clover\.xml|junit\.xml|tags|GTAGS|cscope\.out|\.terraform\.lock\.hcl)$/i) {
    if (!$has_both || $span > 120) {
        deny('generated_artifact', "[token-saver] Blocked unbounded read of generated artifact '$base' (${kb}KB). Read original source files or use grep.");
    }
}

# 1f. Generated code (protobuf, Dart, Prisma, .snap, huge .d.ts, huge OpenAPI/GraphQL)
if ($size > 24576 && (!$has_both || $span > 150)) {
    if ($base =~ /(?:_pb2(?:_grpc)?\.pyi?|\.pb\.(?:go|gw\.go|cc|h|ts|rs|dart)|_pb\.js|_(?:gen|generated)\.go|\.(?:generated|gen)\.(?:ts|tsx|js)|\.g\.dart|\.freezed\.dart|\.mocks\.dart|\.gr\.dart|\.Designer\.cs|\.g\.cs|\.snap|\.snapshot|\.golden)$/i ||
        $check_path =~ m{(?:^|/)(?:\.prisma|prisma|\@prisma)/client/}) {
        deny('generated_code', "[token-saver] Blocked unbounded read of generated file '$base' (${kb}KB). Read the schema/source, grep for the symbol, or pass StartLine/EndLine <=150.");
    }
    if ($base =~ /\.d\.ts$/i && $size > 51200) {
        deny('large_dts', "[token-saver] Blocked unbounded read of huge type def '$base' (${kb}KB). Grep for the symbol and read a <=150-line slice.");
    }
    if ($base =~ /^(?:openapi|swagger)\.(?:json|ya?ml)$|\.graphqls?$/i && $size > 65536) {
        deny('large_schema', "[token-saver] Blocked unbounded read of huge schema '$base' (${kb}KB). Run: python3 $inspect_py schema $sh_path");
    }
}

# 1g. Unbounded reads inside node_modules / dist / build / .next / vendor (>24KB)
if ($size > 24576 && (!$has_both || $span > 150) && !$is_prose &&
    $check_path =~ m{/(?:node_modules|dist|\.next|\.nuxt|vendor)/}) {
    deny('vendor_dir', "[token-saver] Blocked unbounded read inside vendor/build path '$base' (${kb}KB). Grep for the symbol or pass StartLine/EndLine <=150.");
}

# --- Estimate total line count cleanly (for break-even cap check!) ---
my $exact_lines;
if ($size <= 49152 && open(my $fh2, '<:raw', $path)) {
    my $all = '';
    my $n2 = sysread($fh2, $all, 49152) // 0;
    close $fh2;
    $exact_lines = ($all =~ tr/\n//) + 1 if $n2 > 0;
}
my $est_lines = defined $exact_lines
    ? $exact_lines
    : ($peek_nl > 0 ? int($size / ($peek_n / $peek_nl)) : int($size / 80));

# --- Tier 2: Auto-Cap via overwrite ---
# Handles: (a) unbounded, (b) StartLine without EndLine, (c) EndLine without StartLine,
# (d) explicit huge span > 400 on large files, (e) Antigravity spillback output.txt logs.

my $is_data      = ($base =~ /\.(?:json|jsonl|ndjson|ya?ml|csv|tsv|sql|xml|log|html|htm|lcov|diff|patch|out|err)$/i);

# Determine cap target
my ($cap, $cat_label);
if ($is_spillback && $size > 12288) {
    my $avg = ($peek_nl > 0) ? $peek_n / $peek_nl : 80;
    my $c = int(12288 / ($avg || 80));
    ($cap, $cat_label) = ($c > 120 ? 120 : ($c < 40 ? 40 : $c), 'spillback_log');
} elsif ($is_data && $size > 8192) {
    ($cap, $cat_label) = ($size > 65536 ? 100 : 120, 'large_data');
} elsif ($size > 16384) {
    # Code/text hybrid: allow full file if exact_lines <= 320 and size <= 28KB
    if (!(defined $exact_lines && $exact_lines <= 320 && $size <= 28672)) {
        ($cap, $cat_label) = ($size > 102400 ? 200 : 250, 'large_file');
    }
}

# Only cap when it trims >= 1KB beyond what read_file would natively return in one call
# (800 lines / 46,080 bytes); otherwise the ~25-token overwrite notice is pure overhead.
my $avg_line = $size / ($est_lines || 1);
sub worth_cap {
    my ($from, $want_lines, $cap_lines) = @_;
    my $native_lines = $want_lines > 800 ? 800 : $want_lines;
    my $native_bytes = $native_lines * $avg_line;
    $native_bytes = 46080 if $native_bytes > 46080;
    return ($native_bytes - $cap_lines * $avg_line) >= 1024;
}

if (defined $cap) {
    # Case A: completely unbounded
    if (!defined $start && !defined $end) {
        # Break-even guard: only fire overwrite if at least 60-80 lines are actually trimmed!
        if ($est_lines > $cap + 60 && worth_cap(1, $est_lines, $cap)) {
            my $est_kept = int(($size * $cap) / ($est_lines || 1));
            cap_overwrite($cat_label, 1, $cap, $est_kept);
        }
    }
    # Case B: StartLine without EndLine (open-ended!)
    elsif (defined $start && !defined $end) {
        my $s = $start < 1 ? 1 : $start;
        my $rem = $est_lines - $s + 1;
        if ($rem > $cap + 60 && worth_cap($s, $rem, $cap)) {
            my $est_kept = int(($size * $cap) / ($est_lines || 1));
            cap_overwrite($cat_label . '_open_end', ($start < 1 ? 1 : undef), $s + $cap - 1, $est_kept);
        }
    }
    # Case C: EndLine without StartLine (spillback logs only)
    elsif (!defined $start && defined $end && $is_spillback) {
        if ($end > $cap + 60 && $est_lines > $cap + 60 && worth_cap(1, ($end < $est_lines ? $end : $est_lines), $cap)) {
            my $est_kept = int(($size * $cap) / ($est_lines || 1));
            cap_overwrite($cat_label . '_open_start', 1, $cap, $est_kept);
        }
    }
    # Case D: Explicit oversized span on spillback logs only (> 220 lines)
    elsif ($has_both && $is_spillback) {
        my $max_span = 220;
        my $eff = $est_lines - $start + 1;
        $eff = $span if $span < $eff;
        if ($span > $max_span && $eff > $max_span + 60 && worth_cap($start, $eff, $max_span)) {
            my $est_kept = int(($size * $max_span) / ($est_lines || 1));
            cap_overwrite($cat_label . '_wide_span', undef, $start + $max_span - 1, $est_kept);
        }
    }
}

# Check & record normal allowed read (>12KB) for duplicate/superset guard
check_and_record_read($start, $end, 0);

print '{"decision":"allow"}';
exit 0;
