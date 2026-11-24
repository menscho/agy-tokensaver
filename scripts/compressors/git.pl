use strict;
use warnings;
our ($NATIVE_CAP, $LOSSY, $LOSSLESS_RM, $LOCKFILE_DIFF_RE, $EXTRA_SIG_RE, $cat);

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
    # Headroom-inspired diff context trimming: when diff > $NATIVE_CAP, trim unchanged context
    # lines inside hunks to +/-2 around +/- changes before falling back to head/tail truncation.
    if ($out_bytes > $NATIVE_CAP) {
        my @trimmed;
        my @hunk;
        my sub flush_hunk {
            return unless @hunk;
            my $m = scalar(@hunk);
            my @mark = (0) x $m;
            for my $hi (0 .. $m - 1) {
                if ($hunk[$hi] =~ /^[+-]/ || $hunk[$hi] =~ /^@@/) {
                    my $lo = $hi - 2; $lo = 0 if $lo < 0;
                    my $hi2 = $hi + 2; $hi2 = $m - 1 if $hi2 >= $m;
                    $mark[$_] = 1 for ($lo .. $hi2);
                }
            }
            my $dropped = 0;
            for my $hi (0 .. $m - 1) {
                if ($mark[$hi]) {
                    if ($dropped) { push @trimmed, " ... [$dropped context lines trimmed]"; $LOSSY = 1; $dropped = 0; }
                    push @trimmed, $hunk[$hi];
                } else {
                    $dropped++;
                }
            }
            if ($dropped) { push @trimmed, " ... [$dropped context lines trimmed]"; $LOSSY = 1; }
            @hunk = ();
        }
        my $in_hunk = 0;
        for my $x (@out) {
            if ($x =~ /^@@ /) {
                flush_hunk();
                $in_hunk = 1;
                push @hunk, $x;
            } elsif ($x =~ /^(?:diff --git |--- |\+\+\+ )/) {
                flush_hunk();
                $in_hunk = 0;
                push @trimmed, $x;
            } elsif ($in_hunk) {
                push @hunk, $x;
            } else {
                push @trimmed, $x;
            }
        }
        flush_hunk();
        my $tb = 0; $tb += length($_) + 1 for @trimmed;
        if ($tb < $out_bytes) {
            @out = @trimmed;
            $out_bytes = $tb;
        }
    }
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


1;
