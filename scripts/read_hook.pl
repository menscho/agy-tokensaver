#!/usr/bin/env perl
use strict;
use warnings;
BEGIN {
    my $d = $0; $d =~ s{[^/\\]*$}{}; $d = '.' if $d eq ''; $d =~ s{[/\\]+$}{};
    unshift @INC, "$d/lib";
}
use TsJson;

# PreToolUse anti-bloat guard for read_file / view_file.
# - Decodes payload once via JSON::PP (TsJson).
# - Resolves relative paths via cwd, expands ~, checks symlink targets.
# - Supports ByteOffset (2.0) and ContentOffset (IDE view_file).
# - Tier 1: blocks unbounded lockfiles, minified/single-line bundles, sourcemaps, binaries,
#   .ipynb (>12KB), .har/.heapsnapshot/.cpuprofile/.sarif, generated protobuf/ORM/Dart/OpenAPI,
#   .snap, .tsbuildinfo, compile_commands.json, lcov.info, .tfstate*, sqlite DBs — with
#   ts_inspect.py hints.
# - Tier 2: transparent auto-cap via overwrite (0 retry turns!) for unbounded & partial-bound
#   large reads, spillback output.txt logs, multi-line .svg >20KB, and 16-48KB hybrid caps.
# - Honest telemetry: caps raw_bytes at 46,080 (read_file per-call limit).
# - Dedupe (files >4KB): unchanged (mtime+size+MD5) slices already in context are denied with a
#   short reason; a covered prefix/suffix is trimmed via overwrite to the missing lines only.

my $raw = TsJson::read_stdin_capped();
our $doc = TsJson::decode_hook_input($raw);
if (!defined $doc) { print '{"decision":"allow"}'; exit 0; }
if (TsJson::hooks_disabled()) { print '{"decision":"allow"}'; exit 0; }

our $ti = TsJson::tool_input($doc);
sub json_escape { return TsJson::json_escape(@_); }

# Explicit ByteOffset / ContentOffset continuation -> pass through immediately
my $boff = TsJson::get_int($ti, 'ByteOffset', 'ContentOffset');
if (defined $boff && $boff > 0) {
    print '{"decision":"allow"}';
    exit 0;
}

our $path = TsJson::get_str($ti, 'Path', 'AbsolutePath');
if (!defined $path || $path eq '') { print '{"decision":"allow"}'; exit 0; }

# Expand ~ and resolve relative paths against cwd (handles Unix /, Windows C:\, and UNC \\)
if ($path =~ m{^~([/\\].*)?$}) {
    my $home = $ENV{HOME} // $ENV{USERPROFILE} // '';
    $path = $home . ($1 // '') if $home ne '';
} elsif ($path !~ m{^(?:/|[A-Za-z]:[/\\]|\\\\)}) {
    my $cwd = TsJson::get_str($doc, 'cwd') // TsJson::get_str($ti, 'cwd') // '';
    $path = "$cwd/$path" if $cwd ne '';
}

if (!-f $path) { print '{"decision":"allow"}'; exit 0; }

# Resolve symlink target name for trap matching
our $check_path = $path;
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

# Pure-Perl 0.1ms PNG/JPEG pixel dimension reader (zero external modules)

my $RLIB_DIR = do {
    my $d = $0;
    $d =~ s{[^/\\]*$}{};
    $d = '.' if $d eq '';
    $d =~ s{[/\\]+$}{};
    $d = "./$d" if $d !~ m{^(?:/|\\|[A-Za-z]:|\.{1,2}[/\\])};
    "$d/lib";
};
if ($check_path =~ /\.(?:png|jpe?g|gif|webp|ico|bmp|tiff?|pdf|mp4|mov|webm|avi|mkv|mp3|wav|ogg|m4a|flac|aac)$/i) {
    do "$RLIB_DIR/read_media.pl" or do { print '{"decision":"allow"}'; exit 0; };
}
my @st = stat($path);
if (!@st) { print '{"decision":"allow"}'; exit 0; }
our $size  = $st[7];
our $mtime = $st[9];

our $base = $check_path;
$base =~ s{^.*[/\\]}{};

our $start = TsJson::get_int($ti, 'StartLine');
our $end   = TsJson::get_int($ti, 'EndLine');
our $has_both = (defined $start && defined $end && $end >= $start);
our $span     = $has_both ? ($end - $start + 1) : 0;

our $plugin_root = $ENV{PLUGIN_ROOT};
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

our $READ_MAX_BYTES   = 46080;
our $READ_MAX_LINES   = 800;
# Conservative ceiling below 46,080B (accounting for line-number formatting) before
# assuming a line was delivered verbatim without read_file byte-truncation.
my $SAFE_DELIV_BYTES = 40000;
our $MAX_SCAN_BYTES   = 2 * 1024 * 1024;
our $OVERWRITE_NOTICE = 94;

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
        my $cid = TsJson::get_str($doc, 'conversationId') // '';
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


our $kb = sprintf('%.1f', $size / 1024);
my $eff_tok = int(($size > 46080 ? 46080 : $size) / 4);
my $sh_path = esc_sh($path);

# One 4KB head read shared by Tier 0 and the small-file fast path
our ($head_n, $head_nul) = (0, 0);
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


our ($est_lines, $avg_line, $is_spillback, $lic_skip_lines, $is_prose);
do "$RLIB_DIR/read_dedupe.pl" or die "read_dedupe.pl: " . ($@ || $!);

# --- Single 16KB peek (O(1) syscall) for binary, minified, base64, generated/license headers & density ---
my ($peek_n, $peek_nl, $max_line, $has_nul, $has_b64, $ws_on_max, $has_gen_hdr) = (0, 0, 0, 0, 0, 0, 0);
if (open(my $fh, '<:raw', $path)) {
    my $buf = '';
    $peek_n = sysread($fh, $buf, 16384) // 0;
    close $fh;
    if ($peek_n > 0) {
        $has_nul = (index($buf, "\0") >= 0) ? 1 : 0;
        $peek_nl = ($buf =~ tr/\n//);
        $has_b64 = ($buf =~ /data:[^;]{1,40};base64,[A-Za-z0-9+\/=]{500,}/) ? 1 : 0;
        my @plines = split(/\r?\n|\r/, $buf, -1);
        for my $l (@plines) {
            my $len = length($l);
            if ($len > $max_line) {
                $max_line = $len;
                $ws_on_max = ($l =~ tr/ \t//);
            }
        }
        # First-16-line generated-file header check (0% loss: prevents dumping auto-generated files)
        my $hmax = $#plines < 15 ? $#plines : 15;
        my $head16 = join("\n", @plines[0 .. $hmax]);
        $has_gen_hdr = 1 if $head16 =~ /(?:Code generated by|\@generated\b|DO NOT EDIT|AUTO-?GENERATED|automatically generated)/i;
        # Leading pure SPDX/Copyright/Apache/MIT comment banner (12..55 lines) -> safe StartLine advance
        my ($in_blk, $saw_lic, $c_end) = (0, 0, 0);
        my $lmax = $#plines < 55 ? $#plines : 55;
        for my $li (0 .. $lmax) {
            my $x = $plines[$li];
            if ($li == 0 && $x =~ /^#!/) { $c_end = 1; next; }
            if ($in_blk) {
                $saw_lic = 1 if $x =~ /(?:SPDX-License|Copyright\b|Apache License|MIT License|BSD|All rights reserved)/i;
                $c_end = $li + 1;
                $in_blk = 0 if $x =~ /\*\//;
                next;
            }
            if ($x =~ /^\s*\/\*/) {
                $saw_lic = 1 if $x =~ /(?:SPDX-License|Copyright\b|Apache License|MIT License|BSD|All rights reserved)/i;
                $c_end = $li + 1;
                $in_blk = 1 unless $x =~ /\*\//;
                next;
            }
            if ($x =~ /^\s*(?:\/\/|#|--)\s?/ || $x =~ /^\s*$/) {
                $saw_lic = 1 if $x =~ /(?:SPDX-License|Copyright\b|Apache License|MIT License|BSD|All rights reserved)/i;
                $c_end = $li + 1;
                next;
            }
            last;
        }
        $lic_skip_lines = $c_end if $saw_lic && $c_end >= 12 && $c_end <= 55;
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
$is_prose = ($base =~ /\.(?:md|mdx|rst|adoc|tex|txt)$/i);
$is_spillback = ($check_path =~ m{/\.system_generated/steps/[^/]+/output\.txt$} || $check_path =~ m{(?:^|[\\/])agy-tokensaver-[^\\/]+[\\/]});
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
    if ($has_gen_hdr && !$is_prose && !$is_spillback) {
        deny('generated_header', "[token-saver] Blocked unbounded read of auto-generated file '$base' (${kb}KB; \@generated/DO NOT EDIT header). Run: python3 $inspect_py outline $sh_path or grep.");
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
$est_lines = defined $exact_lines
    ? $exact_lines
    : ($peek_nl > 0 ? int($size / ($peek_n / $peek_nl)) : int($size / 80));

# --- Tier 2: Auto-Cap via overwrite ---
# Handles: (a) unbounded, (b) StartLine without EndLine, (c) EndLine without StartLine,
# (d) explicit huge span > 400 on large files, (e) Antigravity spillback output.txt logs.

my $is_data = ($base =~ /\.(?:json|jsonl|ndjson|ya?ml|csv|tsv|sql|xml|html|htm|lcov|diff|patch)$/i);
my $is_log  = ($base =~ /\.(?:log|out|err)$/i);

# Opt-in Compact Reads mode (default OFF).
# Enabled by AGY_TS_COMPACT_READS=1 or Modes handoff_only / small_loss in the HUD.
my $COMPACT_READS = 0;
if (defined $ENV{AGY_TS_COMPACT_READS}) {
    $COMPACT_READS = ($ENV{AGY_TS_COMPACT_READS} =~ /^(?:1|true|on|yes)$/i) ? 1 : 0;
} else {
    my $cfg_p = data_dir() . "/hud_config.json";
    if (-f $cfg_p && open(my $cfh, '<:raw', $cfg_p)) {
        my $cb = '';
        sysread($cfh, $cb, 4096);
        close $cfh;
        $COMPACT_READS = 1 if $cb =~ /"compact_reads"\s*:\s*true\b/i;
    }
}

# Determine cap target. Native read_file already fits <=46,080B / <=800L in one turn;
# logs under 46KB are left whole so mid-file errors aren't hidden by blind tail-cropping.
my ($cap, $cat_label);
if ($is_spillback && $size > 12288) {
    my $avg = ($peek_nl > 0) ? $peek_n / $peek_nl : 80;
    my $c = int(12288 / ($avg || 80));
    ($cap, $cat_label) = ($c > 120 ? 120 : ($c < 40 ? 40 : $c), 'spillback_log');
} elsif ($is_log && $size > 46080) {
    ($cap, $cat_label) = ($COMPACT_READS ? 100 : 150, 'large_log');
} elsif ($is_data && $size > ($COMPACT_READS ? 6144 : 8192)) {
    ($cap, $cat_label) = ($COMPACT_READS ? 80 : ($size > 65536 ? 100 : 120), 'large_data');
} elsif (!$is_log && $size > ($COMPACT_READS ? 8192 : 16384)) {
    my $is_doc = ($base =~ /\.(?:md|mdx|markdown|txt|rst|adoc)$/i);
    if ($is_doc) {
        if ($COMPACT_READS && $base =~ /^(?:CHANGELOG|HISTORY|RELEASES|CHANGES)\./i) {
            ($cap, $cat_label) = (80, 'compact_changelog');
        } elsif ($COMPACT_READS && $size > 8192) {
            ($cap, $cat_label) = (120, 'compact_doc');
        } elsif ($size > 46080) {
            ($cap, $cat_label) = ($size > 102400 ? 200 : 250, 'large_file');
        }
    } elsif ($COMPACT_READS && $size > 10240) {
        if (!(defined $exact_lines && $exact_lines <= 160 && $size <= 12288)) {
            ($cap, $cat_label) = (120, 'compact_code');
        }
    } elsif (!$COMPACT_READS && !(defined $exact_lines && $exact_lines <= 320 && $size <= 28672)) {
        ($cap, $cat_label) = ($size > 102400 ? 200 : 250, 'large_file');
    }
}

# Only cap when it trims >= 1KB beyond what read_file would natively return in one call
# (800 lines / 46,080 bytes); otherwise the ~25-token overwrite notice is pure overhead.
$avg_line = $size / ($est_lines || 1);
sub worth_cap {
    my ($from, $want_lines, $cap_lines) = @_;
    my $native_lines = $want_lines > 800 ? 800 : $want_lines;
    my $native_bytes = $native_lines * $avg_line;
    $native_bytes = 46080 if $native_bytes > 46080;
    return ($native_bytes - $cap_lines * $avg_line) >= 1024;
}

# Dedupe/trim against earlier reads, then auto-cap (if any), then record and emit.
decide_and_emit($cap, $cat_label);
