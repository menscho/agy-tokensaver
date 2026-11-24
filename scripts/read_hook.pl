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
my $doc = TsJson::decode_hook_input($raw);
if (!defined $doc) { print '{"decision":"allow"}'; exit 0; }

my $ti = TsJson::tool_input($doc);
sub json_escape { return TsJson::json_escape(@_); }

# Explicit ByteOffset / ContentOffset continuation -> pass through immediately
my $boff = TsJson::get_int($ti, 'ByteOffset', 'ContentOffset');
if (defined $boff && $boff > 0) {
    print '{"decision":"allow"}';
    exit 0;
}

my $path = TsJson::get_str($ti, 'Path', 'AbsolutePath');
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

# Pure-Perl 0.1ms PNG/JPEG pixel dimension reader (zero external modules)
sub img_dims {
    my ($p) = @_;
    open(my $fh, '<:raw', $p) or return (0, 0);
    my $buf = '';
    my $n = sysread($fh, $buf, 32768) // 0;
    close $fh;
    return (0, 0) if $n < 24;
    # PNG: 8-byte signature + IHDR length/type + width (4B BE) + height (4B BE)
    if (substr($buf, 0, 8) eq "\x89PNG\r\n\x1a\n" && substr($buf, 12, 4) eq 'IHDR') {
        my ($w, $h) = unpack('NN', substr($buf, 16, 8));
        return ($w, $h);
    }
    # JPEG: scan markers for SOF0/SOF1/SOF2 (0xC0/0xC1/0xC2)
    if (substr($buf, 0, 2) eq "\xff\xd8") {
        my $pos = 2;
        while ($pos + 9 < $n) {
            last unless ord(substr($buf, $pos, 1)) == 0xff;
            my $m = ord(substr($buf, $pos + 1, 1));
            $pos += 2;
            next if $m == 0xff || $m == 0x00 || $m == 0x01 || ($m >= 0xd0 && $m <= 0xd9);
            my $len = unpack('n', substr($buf, $pos, 2));
            last if $len < 2 || $pos + $len > $n;
            if ($m == 0xc0 || $m == 0xc1 || $m == 0xc2) {
                my ($h, $w) = unpack('nn', substr($buf, $pos + 3, 4));
                return ($w, $h);
            }
            $pos += $len;
        }
    }
    return (0, 0);
}

# Multimedia / document formats natively handled by read_file (NO svg — read_file returns raw XML!)
if ($check_path =~ /\.(?:png|jpe?g|gif|webp|ico|bmp|tiff?|pdf|mp4|mov|webm|avi|mkv|mp3|wav|ogg|m4a|flac|aac)$/i) {
    if ($check_path =~ /\.(?:png|jpe?g|gif|webp)$/i) {
        my $cid  = TsJson::get_str($doc, 'conversationId') // '';
        my $tx   = TsJson::get_str($doc, 'transcriptPath') // '';
        my $step = TsJson::get_int($doc, 'stepIdx');
        my $pr   = $ENV{PLUGIN_ROOT};
        if (!$pr) { my $s = $0; $s = "$ENV{PWD}/$s" if $s !~ m{^/} && $ENV{PWD}; $s =~ s{/[^/]+/[^/]+$}{}; $pr = $s; }
        my $agy_pd = ($ENV{HOME} // '') . "/.gemini/antigravity/plugin_data";
        my $dd = $ENV{PLUGIN_DATA} || (-d $agy_pd ? "$agy_pd/token-saver" : "$pr/.data");
        my $scid = $cid; $scid =~ s/[^A-Za-z0-9_-]/_/g;
        my @ist = stat($path);
        my ($isz, $imt) = @ist ? ($ist[7], $ist[9]) : (0, 0);

        # 1. Zero-loss Duplicate Image Read Guard: skip unchanged re-reads within 16 steps
        if ($cid ne '' && defined $step && $isz > 0) {
            my $idir = "$dd/reads";
            my $idup = "$idir/$scid.imgdup";
            my $ikey = "$check_path\t$imt\t$isz";
            my @kept_i;
            my $dup_step;
            if (-f $idup && open(my $dfh, '<', $idup)) {
                while (my $ln = <$dfh>) {
                    chomp $ln;
                    my ($st_n, $k) = split(/\t/, $ln, 2);
                    next unless defined $k;
                    if ($k eq $ikey && $step >= int($st_n) && ($step - int($st_n)) <= 16) {
                        $dup_step = int($st_n);
                    }
                    push @kept_i, $ln if ($step - int($st_n)) <= 40;
                }
                close $dfh;
            }
            if (defined $dup_step) {
                my $ib = $check_path; $ib =~ s{^.*[/\\]}{};
                my $msg = "[token-saver] Image '$ib' is unchanged and still in context from step $dup_step (~1.5k vision tok/turn saved).";
                print '{"decision":"deny","reason":"' . json_escape($msg) . '"}';
                exit 0;
            }
            eval {
                if (!-d $idir) { require File::Path; File::Path::make_path($idir); }
                push @kept_i, "$step\t$ikey";
                @kept_i = @kept_i[-40 .. -1] if @kept_i > 40;
                if (open(my $wfh, '>', $idup)) { print $wfh join("\n", @kept_i) . "\n"; close $wfh; }
            };
        }

        # 2. Image-burst guard for long sessions (>3 images AND transcript >150KB)
        if ($cid ne '' && $tx ne '' && -f $tx && ((stat($tx))[7] // 0) > 153600) {
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

        # 3. Oversized Screenshot Auto-Downscaler (opt-in Mode 2 / compact_reads: >1568px -> <=1280px)
        my $img_compact = 0;
        if (defined $ENV{AGY_TS_COMPACT_READS}) {
            $img_compact = ($ENV{AGY_TS_COMPACT_READS} =~ /^(?:1|true|on|yes)$/i) ? 1 : 0;
        } elsif (-f "$dd/hud_config.json" && open(my $cfh, '<:raw', "$dd/hud_config.json")) {
            my $cb = ''; sysread($cfh, $cb, 4096); close $cfh;
            $img_compact = 1 if $cb =~ /"compact_reads"\s*:\s*true\b/i;
        }
        if ($img_compact && $check_path =~ /\.(?:png|jpe?g)$/i && $isz > 65536) {
            my ($iw, $ih) = img_dims($path);
            if ($iw > 1568 || $ih > 1568) {
                my $tmpb = $ENV{TMPDIR} || $ENV{TEMP} || $ENV{TMP} || '/tmp';
                $tmpb =~ s{[\\/]+$}{};
                my $ut = ($^O eq 'MSWin32') ? ($ENV{USERNAME} || 'user') : $<;
                $ut =~ s/[^A-Za-z0-9_-]/_/g;
                my $sdir = "$tmpb/agy-tokensaver-$ut";
                mkdir($sdir, 0700) unless -d $sdir;
                # Deterministic content-addressed cache filename
                my $h = 2166136261;
                for my $ch (split(//, "$path|$imt|$isz")) { $h = (($h ^ ord($ch)) * 16777619) & 0xffffffff; }
                my $dst = sprintf('%s/img-%08x.jpg', $sdir, $h);
                if (!-f $dst) {
                    my $ok = 0;
                    # Prefer zero-dep native tools (ffmpeg / magick / sips) with strict fail-open
                    if (!$ok && system('ffmpeg', '-v', 'error', '-y', '-i', $path, '-vf', "scale='min(1280,iw)':'min(1280,ih)':force_original_aspect_ratio=decrease", '-q:v', '4', $dst) == 0 && -s $dst) {
                        $ok = 1;
                    }
                    if (!$ok && $^O eq 'darwin' && system('sips', '-Z', '1280', '-s', 'format', 'jpeg', $path, '--out', $dst) == 0 && -s $dst) {
                        $ok = 1;
                    }
                    if (!$ok && system('magick', $path, '-resize', '1280x1280>', '-quality', '85', $dst) == 0 && -s $dst) {
                        $ok = 1;
                    }
                }
                if (-s $dst) {
                    my $key = (defined TsJson::get_str($ti, 'AbsolutePath')) ? 'AbsolutePath' : 'Path';
                    print '{"decision":"allow","overwrite":{"' . $key . '":"' . json_escape($dst) . '"}}';
                    exit 0;
                }
            }
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

my $start = TsJson::get_int($ti, 'StartLine');
my $end   = TsJson::get_int($ti, 'EndLine');
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

# --- Compaction-safe duplicate / overlap guard (files > 4KB, same compaction epoch) ---
# A prior read counts as "still in context" only if the file is byte-identical (mtime + size +
# MD5) AND no context compaction happened since. Compaction is ruled out either by the legacy
# rule (<= 16 steps and same epoch file written by post_invocation_alerts.pl) or, for any distance,
# by scanning the conversation transcript: no CHECKPOINT record and no >40% context drop between
# the two reads, and the transcript is provably current (its last PLANNER_RESPONSE issued this very
# read_file/view_file call). Only line ranges guaranteed to fit below read_file's 46,080B / 800L
# truncation ceiling are recorded, so lines truncated from an earlier read are never treated as seen.
# Outcomes: fully covered -> short deny (nothing new to show); covered prefix/suffix -> transparent
# overwrite to the missing lines only (0 retry turns); otherwise -> untouched.
my $dup_step = TsJson::get_int($doc, 'stepIdx');
my $dup_cid  = TsJson::get_str($doc, 'conversationId') // '';
my $NEAR_STEPS     = 16;     # legacy window when the transcript cannot prove "no compaction since"
my $TRIM_MIN_BYTES = 1536;   # only trim an overlap that removes far more than the 94B notice
my $HIST_MAX       = 300;
my $dedupe_on = ($size > 4096 && defined $dup_step && $dup_cid ne '' && $path !~ /[\t\r\n]/) ? 1 : 0;
my $path_key = $path;
if ($^O =~ /^(?:cygwin|MSWin32|msys)$/ || $path_key =~ m{^(?:[A-Za-z]:|\\\\)}) {
    $path_key = lc $path_key;
    $path_key =~ tr{\\}{/};
}
my ($est_lines, $avg_line, $is_spillback);

sub data_dir {
    my $agy_pd = ($ENV{HOME} // $ENV{USERPROFILE} // '') . "/.gemini/antigravity/plugin_data";
    return $ENV{PLUGIN_DATA} || (-d $agy_pd ? "$agy_pd/token-saver" : "$plugin_root/.data");
}
my $scid = $dup_cid; $scid =~ s/[^A-Za-z0-9_-]/_/g;

# One pass over the file (<= 2MB): exact line count, per-line byte lengths, content fingerprint.
my ($scanned_file, $file_lines, $file_md5, @line_len);
sub scan_file {
    return if $scanned_file;
    $scanned_file = 1;
    return if $size > $MAX_SCAN_BYTES;
    open(my $fh, '<:raw', $path) or return;
    my $data = do { local $/; <$fh> };
    close $fh;
    return unless defined $data && length($data) == $size;
    @line_len = map { length } ($data =~ /[^\n]*\n|[^\n]+\z/g);
    $file_lines = scalar @line_len;
    $file_md5 = eval { require Digest::MD5; Digest::MD5::md5_hex($data) } // sprintf('s%08x', unpack('%32C*', $data));
}

# Bytes read_file would emit for lines [s..e] (incl. "N: " prefix), capped at 800 lines / 46,080B.
sub native_bytes {
    my ($s, $e) = @_;
    my ($tot, $n) = (0, 0);
    for my $i ($s .. $e) {
        last if $i > @line_len || ++$n > $READ_MAX_LINES;
        $tot += $line_len[$i - 1] + length($i) + 2;
        last if $tot >= $READ_MAX_BYTES;
    }
    return $tot > $READ_MAX_BYTES ? $READ_MAX_BYTES : $tot;
}

my ($hist_loaded, @hist_lines);
sub load_hist {
    return if $hist_loaded;
    $hist_loaded = 1;
    my $rdir = data_dir() . "/reads";
    my $rfile = "$rdir/$scid.tsv";
    # @plexer/cli-inspired compaction reset via initialNumSteps drop
    my $init_steps = TsJson::get_int($doc, 'initialNumSteps');
    if (defined $init_steps && $init_steps >= 0 && $scid ne '') {
        my $ifile = "$rdir/$scid.initsteps";
        my $prev_init;
        if (-f $ifile && open(my $ifh, '<', $ifile)) { $prev_init = int(<$ifh> // -1); close $ifh; }
        if (defined $prev_init && $prev_init >= 0 && $init_steps < $prev_init) {
            unlink($rfile, "$rdir/$scid.searches", "$rdir/$scid.lastblk");
        }
        eval {
            if (!-d $rdir) { require File::Path; File::Path::make_path($rdir); }
            if (open(my $wfh, '>', $ifile)) { print $wfh "$init_steps\n"; close $wfh; }
        };
    }
    if (-f $rfile && open(my $rfh, '<', $rfile)) {
        while (my $rl = <$rfh>) { chomp $rl; push @hist_lines, $rl if $rl ne ''; }
        close $rfh;
    }
}

my $epoch_val;
sub cur_epoch {
    return $epoch_val if defined $epoch_val;
    $epoch_val = 0;
    my $ep_file = data_dir() . "/alerts/$scid.epoch";
    if (-f $ep_file && open(my $efh, '<', $ep_file)) { $epoch_val = int(<$efh> // 0); close $efh; }
    return $epoch_val;
}

# (verified, last_compaction_step). verified=1 only when the transcript is readable and current.
my @tx_state;
sub transcript_state {
    return @tx_state if @tx_state;
    @tx_state = (0, -1);
    eval {
        my $tx = TsJson::get_str($doc, 'transcriptPath') // '';
        my $f = ($tx ne '' && -f $tx) ? $tx : undef;
        if (!defined $f && $dup_cid =~ /^[A-Za-z0-9_-]+$/) {
            my $home = $ENV{HOME} // $ENV{USERPROFILE} // '';
            for my $app ('antigravity', 'antigravity-cli') {
                my $c = "$home/.gemini/$app/brain/$dup_cid/.system_generated/logs/transcript.jsonl";
                if (-f $c) { $f = $c; last; }
            }
        }
        return unless defined $f;
        my $fsz = (stat($f))[7] // 0;
        return if $fsz <= 0 || $fsz > 32 * 1024 * 1024;
        open(my $fh, '<:raw', $f) or return;
        my ($barrier, $prev_ctx, $pl_step, @pl_names) = (-1, undef, undef);
        while (my $line = <$fh>) {
            next unless $line =~ /^\{\s*"step_index"\s*:\s*"?(\d+)/;
            my $si = int($1);
            last if $si >= $dup_step;
            my $type = ($line =~ /(?<!\\)"type"\s*:\s*"([A-Z_]+)"/) ? $1 : '';
            if ($type eq 'CHECKPOINT') {
                ($barrier, $prev_ctx, $pl_step) = ($si, undef, undef);
            } elsif ($type eq 'PLANNER_RESPONSE') {
                $pl_step = $si;
                @pl_names = ($line =~ /(?:\[|\},)\s*\{"name"\s*:\s*"([^"\\]+)"/g);
                if ($line =~ /(?<!\\)"input_tokens"\s*:\s*"?(\d+)/) {
                    my $ctx = $1 + (($line =~ /(?<!\\)"cache_read_tokens"\s*:\s*"?(\d+)/) ? $1 : 0);
                    $barrier = $si if defined $prev_ctx && $prev_ctx >= 20000 && $ctx < $prev_ctx * 0.6;
                    $prev_ctx = $ctx if $ctx > 0;
                }
            }
        }
        close $fh;
        return unless defined $pl_step;
        my $k = $dup_step - $pl_step;
        return unless $k >= 1 && $k <= @pl_names && $pl_names[$k - 1] =~ /^(?:read_file|view_file)$/;
        @tx_state = (1, $barrier);
    };
    return @tx_state;
}

# Plan for native request window [S..E]: ('dup', source_text) | ('trim', a, b, bytes) | ('none')
sub dedupe_plan {
    my ($S, $E) = @_;
    return ('none') unless $dedupe_on && $E >= $S;
    load_hist();
    my $ep = cur_epoch();
    my @c;
    for my $rl (@hist_lines) {
        my ($rp, $rs, $re, $rm, $rsz, $rst, $rep, $rmd) = split(/\t/, $rl);
        next unless defined $rep && $rp eq $path_key && $rm == $mtime && $rsz == $size && $rep == $ep;
        my $d = $dup_step - int($rst);
        next unless $d > 0;
        push @c, [($rs eq '' ? 1 : int($rs)), ($re eq '' ? 1e12 : int($re)), int($rst), $d, $rmd // ''];
    }
    return ('none') unless @c;
    scan_file();
    return ('none') unless defined $file_md5;
    @c = grep { $_->[4] eq $file_md5 } @c;
    return ('none') unless @c;
    my ($tx_ok, $barrier) = transcript_state();
    @c = grep { $tx_ok ? ($_->[2] > $barrier) : ($_->[3] <= $NEAR_STEPS) } @c;
    return ('none') unless @c;
    my @gaps;
    my $cur = $S;
    for my $iv (sort { $a->[0] <=> $b->[0] } @c) {
        my ($lo, $hi) = @$iv;
        next if $hi < $cur;
        last if $lo > $E;
        push @gaps, [$cur, $lo - 1] if $lo > $cur;
        $cur = $hi + 1 if $hi + 1 > $cur;
        last if $cur > $E;
    }
    push @gaps, [$cur, $E] if $cur <= $E;
    if (!@gaps) {
        my ($best) = sort { $a->[3] <=> $b->[3] } grep { $_->[0] <= $S && $_->[1] >= $E } @c;
        return ('dup', "your read of '$base" . ($best->[1] >= 1e12 ? '' : ":$best->[0]-$best->[1]") . "' at step $best->[2]") if $best;
        my ($last) = sort { $a->[3] <=> $b->[3] } @c;
        return ('dup', "your earlier reads of '$base' (last at step $last->[2])");
    }
    return ('none') if @gaps > 1;
    my ($ga, $gb) = @{$gaps[0]};
    return ('none') if $ga == $S && $gb == $E;
    $gb = $ga + $READ_MAX_LINES - 1 if $gb - $ga + 1 > $READ_MAX_LINES;
    my $saved = native_bytes($S, $E) - native_bytes($ga, $gb);
    return ('none') if $saved < $TRIM_MIN_BYTES;
    return ('trim', $ga, $gb, $saved);
}

sub record_read {
    my ($eff_s, $eff_e) = @_;
    return unless $dedupe_on;
    eval {
        scan_file();
        return unless defined $file_md5;
        my ($mok, $slice_b, $safe_end, $full_file) = measure_line_range($eff_s, $eff_e);
        return unless $mok;
        my ($rec_s, $rec_e);
        if ($full_file) {
            ($rec_s, $rec_e) = ('', '');
        } elsif (defined $safe_end) {
            my $rs_eff = (defined $eff_s && $eff_s >= 1) ? int($eff_s) : 1;
            return if $safe_end < $rs_eff;
            ($rec_s, $rec_e) = ($rs_eff, $safe_end);
        } else {
            return;
        }
        load_hist();
        push @hist_lines, join("\t", $path_key, $rec_s, $rec_e, $mtime, $size, $dup_step, cur_epoch(), $file_md5);
        @hist_lines = @hist_lines[-$HIST_MAX .. -1] if @hist_lines > $HIST_MAX;
        my $rdir = data_dir() . "/reads";
        if (!-d $rdir) { require File::Path; File::Path::make_path($rdir); }
        if (open(my $wfh, '>', "$rdir/$scid.tsv")) { print $wfh join("\n", @hist_lines) . "\n"; close $wfh; }
    };
}

# Lightweight per-line hash snapshot for post-edit dirty-hunk narrowing (squeez/Cline pattern).
sub snap_path_for {
    my ($k) = @_;
    my $h = 2166136261;
    for my $ch (split(//, $k)) { $h = (($h ^ ord($ch)) * 16777619) & 0xffffffff; }
    return sprintf('%s/reads/snaps/%08x.lines', data_dir(), $h);
}

sub save_line_snap {
    return unless $dedupe_on && $size > 4096 && $size <= 65536;
    return unless defined $file_lines && $file_lines > 160;
    return if $is_spillback || $base =~ /\.(?:log|err|out|json|jsonl|csv|tsv)$/i;
    eval {
        my $sdir = data_dir() . "/reads/snaps";
        if (!-d $sdir) { require File::Path; File::Path::make_path($sdir); }
        open(my $fh, '<:raw', $path) or return;
        my @hashes;
        while (my $ln = <$fh>) {
            my $h = 2166136261;
            for my $b (unpack('C*', $ln)) { $h = (($h ^ $b) * 16777619) & 0xffffffff; }
            push @hashes, sprintf('%08x', $h);
        }
        close $fh;
        my $sp = snap_path_for($path_key);
        if (open(my $wfh, '>:raw', $sp)) {
            print $wfh "$file_md5\n" . join("\n", @hashes) . "\n";
            close $wfh;
        }
    };
}

sub dirty_hunk_range {
    return () unless $dedupe_on && !defined $start && !defined $end && $size > 4096 && $size <= 65536;
    return () unless defined $file_lines && $file_lines > 180;
    return () if $is_spillback || $base =~ /\.(?:log|err|out|json|jsonl|csv|tsv)$/i;
    my $sp = snap_path_for($path_key);
    return () unless -f $sp;
    open(my $sfh, '<:raw', $sp) or return ();
    my $old_md5 = <$sfh>;
    chomp $old_md5 if defined $old_md5;
    return () if !defined $old_md5 || $old_md5 eq $file_md5;
    my @old;
    while (my $h = <$sfh>) { chomp $h; push @old, $h; }
    close $sfh;
    return () if !@old;
    open(my $cfh, '<:raw', $path) or return ();
    my @cur;
    while (my $ln = <$cfh>) {
        my $h = 2166136261;
        for my $b (unpack('C*', $ln)) { $h = (($h ^ $b) * 16777619) & 0xffffffff; }
        push @cur, sprintf('%08x', $h);
    }
    close $cfh;
    my ($n_old, $n_cur) = (scalar(@old), scalar(@cur));
    my $pre = 0;
    $pre++ while $pre < $n_old && $pre < $n_cur && $old[$pre] eq $cur[$pre];
    my $suf = 0;
    $suf++ while $suf < ($n_old - $pre) && $suf < ($n_cur - $pre) && $old[$n_old - 1 - $suf] eq $cur[$n_cur - 1 - $suf];
    my $lo = $pre + 1;
    my $hi = $n_cur - $suf;
    $hi = $lo if $hi < $lo;
    my $span_dirty = $hi - $lo + 1;
    return () if $span_dirty <= 0 || $span_dirty > 80 || $n_cur < 180;
    my $a0 = $lo - 15; $a0 = 1 if $a0 < 1;
    my $b0 = $hi + 15; $b0 = $n_cur if $b0 > $n_cur;
    return () if ($b0 - $a0 + 1) >= $n_cur - 40;
    return ($a0, $b0);
}

my ($lic_skip_lines, $is_prose) = (0, 0);

# Final decision for an otherwise-allowed read: dedupe/trim, dirty-hunk narrow, auto-cap, record.
sub decide_and_emit {
    my ($cap, $cat_label) = @_;
    my ($a, $b);              # effective range after trim/cap (undef = request untouched)
    my ($trim_saved, $cap_lab) = (0, undef);
    if ($dedupe_on) {
        scan_file();
        my $S = (defined $start && $start >= 1) ? int($start) : 1;
        my $E = defined $end ? int($end) : $S + $READ_MAX_LINES - 1;
        $E = $S + $READ_MAX_LINES - 1 if $E - $S + 1 > $READ_MAX_LINES;
        $E = $file_lines if defined $file_lines && $E > $file_lines;
        my @p = eval { dedupe_plan($S, $E) };
        @p = ('none') unless @p;
        if ($p[0] eq 'dup') {
            my $req = (defined $start || defined $end) ? ':' . ($start // '') . '-' . ($end // '') : '';
            my ($mok, $sb) = measure_line_range($start, $end);
            deny('duplicate_read', "[token-saver] '$base$req' is unchanged and still in context from $p[1]. Reuse that; no re-read needed.", $mok ? $sb : undef);
        }
        if ($p[0] eq 'trim') { ($a, $b, $trim_saved) = @p[1 .. 3]; }
        if (!defined $a) {
            my @dh = eval { dirty_hunk_range() };
            if (@dh == 2) { ($a, $b, $cap_lab) = ($dh[0], $dh[1], 'dirty_hunk'); }
        }
    }
    if (defined $cap && !defined $cap_lab) {
        my $s0 = defined $a ? $a : ((defined $start && $start >= 1) ? int($start) : 1);
        # 0%-loss SPDX/Copyright legal banner skip on unbounded code reads
        if (!defined $start && !defined $end && !defined $a && $lic_skip_lines >= 12 && !$is_spillback && !$is_prose) {
            $s0 = $lic_skip_lines + 1;
        }
        my $total = defined $file_lines ? $file_lines : $est_lines;
        my ($want_end, $c, $lab);
        if (!defined $end) {
            ($want_end, $c, $lab) = ($total, $cap, defined $start ? $cat_label . '_open_end' : $cat_label);
        } elsif (!defined $start && $is_spillback) {
            ($want_end, $c, $lab) = ((defined $b ? $b : ($end < $total ? $end : $total)), $cap, $cat_label . '_open_start');
        } elsif (defined $start && $is_spillback && $span > 220) {
            my $e2 = defined $b ? $b : $end;
            ($want_end, $c, $lab) = (($e2 < $total ? $e2 : $total), 220, $cat_label . '_wide_span');
        }
        if (defined $want_end) {
            my $rem = $want_end - $s0 + 1;
            if ($rem > $c + 60 && worth_cap($s0, $rem, $c)) {
                # Tail-anchor unbounded log/error/spillback reads (OpenAI Codex + LogDx-CI arXiv:2605.28876):
                # actual failures/tracebacks live at the tail, avoiding follow-up slice reads.
                if (!defined $start && !defined $end && !defined $a && ($is_spillback || $base =~ /\.(?:log|err|out)$/i) && $total > $c) {
                    my $ts = $total - $c + 1;
                    $ts = 1 if $ts < 1;
                    ($a, $b, $cap_lab) = ($ts, $total, $lab . '_tail');
                } else {
                    ($a, $b, $cap_lab) = ($s0, $s0 + $c - 1, $lab);
                }
            }
        }
    }
    if (defined $a) {
        my ($mok, $sb) = (defined $start || defined $end) ? measure_line_range($start, $end) : (0, undef);
        my ($kok, $kb2) = measure_line_range($a, $b);
        log_event($cap_lab ? 'capped' : 'trimmed', ($kok ? $kb2 : 0) + $OVERWRITE_NOTICE,
                  $cap_lab ? ($trim_saved ? "$cap_lab+overlap_trim" : $cap_lab) : 'overlap_trim', $mok ? $sb : undef);
        record_read($a, $b);
        save_line_snap();
        printf('{"decision":"allow","overwrite":{"StartLine":%d,"EndLine":%d}}', $a, $b);
        exit 0;
    }
    record_read($start, $end);
    save_line_snap();
    print '{"decision":"allow"}';
    exit 0;
}

# Fast-path allow for small normal files (4KB..12KB) unless data/doc >8KB (capped below) or binary
if ($size <= 12288 && !($size > 8192 && $base =~ /\.(?:json|jsonl|ndjson|ya?ml|csv|tsv|sql|xml|log|html|htm|lcov|diff|patch|out|err|md|mdx|markdown|txt)$/i)) {
    if ($head_nul) {
        deny('binary', "[token-saver] Blocked read of binary file '$base' (${kb}KB). Use file/strings/python3 instead.");
    }
    decide_and_emit(undef, undef);
}

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

my $is_data      = ($base =~ /\.(?:json|jsonl|ndjson|ya?ml|csv|tsv|sql|xml|log|html|htm|lcov|diff|patch|out|err)$/i);

# Opt-in Compact Reads mode (default OFF in Mode 1 Zero Quality Loss).
# Enabled by AGY_TS_COMPACT_READS=1 or Modes 2a/2b/2c/Max in the HUD.
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

# Determine cap target
my ($cap, $cat_label);
if ($is_spillback && $size > 12288) {
    my $avg = ($peek_nl > 0) ? $peek_n / $peek_nl : 80;
    my $c = int(12288 / ($avg || 80));
    ($cap, $cat_label) = ($c > 120 ? 120 : ($c < 40 ? 40 : $c), 'spillback_log');
} elsif ($is_data && $size > ($COMPACT_READS ? 6144 : 8192)) {
    ($cap, $cat_label) = ($COMPACT_READS ? 80 : ($size > 65536 ? 100 : 120), 'large_data');
} elsif ($size > ($COMPACT_READS ? 8192 : 16384)) {
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
