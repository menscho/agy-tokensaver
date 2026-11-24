use strict;
use warnings;
our ($doc, $ti, $path, $check_path, $base, $size, $mtime, $start, $end, $has_both, $span,
     $plugin_root, $MAX_SCAN_BYTES, $READ_MAX_BYTES, $READ_MAX_LINES, $COMPACT_READS,
     $OVERWRITE_NOTICE, $head_nul, $kb,
     $est_lines, $avg_line, $is_spillback, $lic_skip_lines, $is_prose);

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
# shared via our

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

($lic_skip_lines, $is_prose) = (0, 0);

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


1;
