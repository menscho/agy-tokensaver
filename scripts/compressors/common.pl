use strict;
use warnings;
our ($NATIVE_CAP, $LOSSY, $LOSSLESS_RM, $LOCKFILE_DIFF_RE, $EXTRA_SIG_RE, $cat);

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

# Headroom-inspired lossless repeated multi-line block folding (blocks of 2..6 lines repeated >=3x).
sub fold_repeated_blocks {
    my ($lines) = @_;
    my $n = scalar(@$lines);
    return $lines if $n < 6;
    my @out;
    my $i = 0;
    while ($i < $n) {
        my $matched_k = 0;
        my $reps = 1;
        for my $k (2 .. 6) {
            last if $i + $k * 3 > $n;
            my $ok = 1;
            for my $j (0 .. $k - 1) {
                if ($lines->[$i + $j] ne $lines->[$i + $k + $j] ||
                    $lines->[$i + $j] ne $lines->[$i + 2 * $k + $j]) {
                    $ok = 0; last;
                }
            }
            if ($ok) {
                $matched_k = $k;
                $reps = 3;
                while ($i + ($reps + 1) * $k <= $n) {
                    my $more = 1;
                    for my $j (0 .. $k - 1) {
                        if ($lines->[$i + $j] ne $lines->[$i + $reps * $k + $j]) { $more = 0; last; }
                    }
                    last unless $more;
                    $reps++;
                }
                last;
            }
        }
        if ($matched_k) {
            push @out, @{$lines}[$i .. $i + $matched_k - 1], "[block above (${matched_k}L) repeated x$reps]";
            $LOSSLESS_RM += ($reps - 1) * $matched_k - 1;
            $i += $reps * $matched_k;
        } else {
            push @out, $lines->[$i++];
        }
    }
    return \@out;
}

# Merlin-inspired (arXiv:2605.09990) deterministic byte-exact non-adjacent chunk deduplication.
# Replaces exact duplicate multi-line paragraphs (>=3 lines, >=160B) with a pointer to the first
# occurrence (100% lossless; zero quality loss).
sub merlin_dedupe_chunks {
    my ($lines) = @_;
    my $n = scalar(@$lines);
    return $lines if $n < 12;
    my (@out, %seen, @blk);
    my $blk_start = 1;
    my sub flush_blk {
        return unless @blk;
        my $bn = scalar(@blk);
        if ($bn >= 3) {
            my $txt = join("\n", @blk);
            if (length($txt) >= 160) {
                if (exists $seen{$txt}) {
                    push @out, "[exact dup of lines $seen{$txt} (${bn}L)]";
                    $LOSSLESS_RM += $bn - 1;
                    @blk = ();
                    return;
                }
                $seen{$txt} = $blk_start . '-' . ($blk_start + $bn - 1);
            }
        }
        push @out, @blk;
        @blk = ();
    }
    for my $i (0 .. $n - 1) {
        if ($lines->[$i] eq '') {
            flush_blk();
            push @out, '';
            $blk_start = $i + 2;
        } else {
            $blk_start = $i + 1 unless @blk;
            push @blk, $lines->[$i];
        }
    }
    flush_blk();
    return \@out;
}

# Headroom-inspired dense machine-generated line elider (minified bundles, hex/token walls).
# Never touches code, prose, TSV, or JSON ({...}/[...]). Spills raw output whenever triggered.
sub elide_dense_lines {
    my ($lines) = @_;
    my ($dense_bytes, @dense_idx) = (0);
    for my $idx (0 .. $#$lines) {
        my $l = $lines->[$idx];
        my $len = length($l);
        next if $len < 300;
        next if index($l, "\t") >= 0;
        next if $l =~ /^\s*[\[{]/;
        my $sp = ($l =~ tr/ //);
        next if ($sp / $len) >= 0.06;
        my $ascii = ($l =~ tr/\x20-\x7e//);
        next if ($ascii / $len) < 0.90;
        $dense_bytes += $len;
        push @dense_idx, $idx;
    }
    return $lines if $dense_bytes < 1500;
    $LOSSY = 1;
    my @out = @$lines;
    for my $idx (@dense_idx) {
        my $l = $out[$idx];
        my $mid = length($l) - 240;
        $out[$idx] = substr($l, 0, 160) . " ... [elided ${mid}B] ... " . substr($l, -80);
    }
    return \@out;
}

# Headroom-inspired SmartCrusher for JSON arrays:
# 1) Lossless [N]{col1,col2,...} CSV+schema table (flattening uniform 1-level nested objects <=4 keys,
#    offloading >256B opaque blobs to spillback).
# 2) If still > $NATIVE_CAP, error-preserving adaptive sampling keeping ALL rows matching Headroom's
#    12 non-negotiable error keywords + structural outliers + head/tail + stride samples.

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
        if ($l =~ /^\s+File\s+"[^"]*(?:\/site-packages\/|\/dist-packages\/|\/lib\/python3\.\d+\/|\/_pytest\/|\/pluggy\/|<frozen |\\(?:site|dist)-packages\\|\\Python3\d*\\Lib\\|\\_pytest\\)/i) {
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
# optional per-category additions (PowerShell path only; POSIX unchanged)
sub cap_with_signal {
    my ($lines, $head, $tail, $max_sig) = @_;
    my $n = scalar(@$lines);
    return $lines if $n <= $head + $tail + 5;
    my $bytes = 0;
    $bytes += length($_) + 1 for @$lines;
    return $lines if $bytes <= $NATIVE_CAP;
    $LOSSY = 1;
    my @keep_idx = grep { $lines->[$_] =~ $SIGNAL_RE || (defined $EXTRA_SIG_RE && $lines->[$_] =~ $EXTRA_SIG_RE) } ($head .. $n - $tail - 1);
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


sub total_bytes { my $b = 0; $b += length($_) + 1 for @{$_[0]}; return $b; }


1;
