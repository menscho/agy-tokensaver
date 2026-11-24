use strict;
use warnings;
our ($NATIVE_CAP, $LOSSY, $LOSSLESS_RM, $LOCKFILE_DIFF_RE, $EXTRA_SIG_RE, $cat);

load_compressor_family('json');

sub compress_search {
    my ($lines, $rc) = @_;
    my (@grouped, %file_counts);
    my $cur_file = '';
    for my $l (@$lines) {
        # Cap minified single-line matches (> 360 chars)
        $l = cap_line_len($l, 360, 320);
        # Windows-safe path:line: anchor (handles C:\path\file.py:42: without matching bare 3:2024-11-01)
        if ($l =~ m{^((?:[A-Za-z]:[\\/])?[^\s:][^:]*):(\d+)([:-])(.*)$} && $1 !~ /^\d+$/) {
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
        my @top = sort { $file_counts{$b} <=> $file_counts{$a} || $a cmp $b } keys %file_counts;
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
    my @top = sort { $dir_cnt{$b} <=> $dir_cnt{$a} || $a cmp $b } keys %dir_cnt;
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
    # JSON body: Headroom SmartCrusher (lossless CSV+schema / error-preserving sampling), else ws-compact
    if ($joined =~ /\A\s*[\[{]/) {
        my $crushed = crush_json_output(\@kept, $joined);
        return $crushed if defined $crushed;
        return tail_weighted_cap(json_ws_compact(\@kept), 60, 40);
    }
    # Strip HTML script/style/svg/comments + inline style attrs if HTML response > 4KB
    if (length($joined) > 4096 && $joined =~ /<(?:!doctype\s+html|html\b)/i) {
        $joined =~ s/<(script|style|svg)\b[^>]*>.*?<\/\1>//gis;
        $joined =~ s/<!--.*?-->//gs;
        $joined =~ s/\s+style="[^"]{20,}"//gi;
        $LOSSY = 1;
        my @hl;
        for my $ln (split(/\n/, $joined)) {
            $ln =~ s/[ \t]+/ /g;
            $ln =~ s/^\s+|\s+$//g;
            push @hl, $ln if $ln =~ /\S/;
        }
        return tail_weighted_cap(\@hl, 60, 40);
    }
    my $ed = elide_dense_lines(\@kept);
    my @capped = map { cap_line_len($_, 500, 460) } @$ed;
    return tail_weighted_cap(fold_repeated_blocks(dedupe_consecutive(\@capped)), 60, 40);
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
        my @top = sort { $dirs{$b} <=> $dirs{$a} || $a cmp $b } keys %dirs;
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


sub compress_logs {
    my ($lines, $rc) = @_;
    my $joined = join("\n", @$lines);
    if ($joined =~ /\A\s*[\[{]/ && length($joined) >= 512) {
        my $crushed = crush_json_output($lines, $joined);
        return $crushed if defined $crushed;
    }
    my $b = collapse_table_padding(merlin_dedupe_chunks(fold_repeated_blocks(dedupe_consecutive($lines))));
    # <= native cap: lossless exact-duplicate, chunk, table & block folding (+ dense-line elide if huge blob wall)
    return elide_dense_lines($b) if total_bytes($b) <= $NATIVE_CAP;
    my $dedup = dedupe_log_templates(fold_stack_frames(elide_dense_lines($b)));
    return cap_with_signal($dedup, 25, 80, 50);
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
    if (@$lines && $lines->[0] =~ /^\s*[\[{]/) {
        my $joined = join("\n", @$lines);
        my $crushed = crush_json_output($lines, $joined);
        return $crushed if defined $crushed;
        return tail_weighted_cap(json_ws_compact($lines), 50, 60);
    }
    my $t = collapse_table_padding(fold_repeated_blocks(dedupe_log_templates(fold_stack_frames(elide_dense_lines($lines)))));
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

sub compress_ssh {
    my ($lines, $rc) = @_;
    my $joined = join("\n", @$lines);
    if ($joined =~ /\A\s*[\[{]/ && length($joined) >= 512) {
        my $crushed = crush_json_output($lines, $joined);
        return $crushed if defined $crushed;
    }
    # Remote grep/rg output: hoist repeated file paths losslessly when >=6 grep-like hits
    my ($non_empty, $grep_hits) = (0, 0);
    for my $l (@$lines) {
        next unless $l =~ /\S/;
        $non_empty++;
        $grep_hits++ if $l =~ m{^(?:/|[A-Za-z]:[\\/]|[\w.-]+/)[^\s:]+:\d+[:-]};
    }
    if ($grep_hits >= 6 && $non_empty > 0 && ($grep_hits / $non_empty) >= 0.30) {
        return compress_search($lines, $rc);
    }
    my $base = collapse_table_padding(merlin_dedupe_chunks(fold_repeated_blocks(dedupe_consecutive($lines))));
    return elide_dense_lines($base) if total_bytes($base) <= $NATIVE_CAP;
    my $t = dedupe_log_templates(fold_stack_frames(elide_dense_lines($base)));
    return $rc != 0 ? cap_with_signal($t, 40, 110, 60) : cap_with_signal($t, 35, 85, 60);
}

# --- PowerShell output (Windows) ---
# Format-Table output is padded to fixed column spans (from the "----  ---" separator row) and
# to the console width; Format-List pads names before " : "; Get-ChildItem -Recurse repeats a
# "Directory:" banner + identical header per folder. All compactions below are whitespace or
# exact-repeat removals: every cell value is kept verbatim (lossless).

1;
