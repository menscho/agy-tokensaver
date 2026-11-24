use strict;
use warnings;
our ($NATIVE_CAP, $LOSSY, $LOSSLESS_RM, $LOCKFILE_DIFF_RE, $EXTRA_SIG_RE, $cat);

sub compress_build {
    my ($lines, $rc) = @_;
    my $noise = qr{^\s*(?:Compiling\s+\S+|Downloading\s+\S+|Fresh\s+\S+|#\d+\s+(?:CACHED|DONE|transferring|load metadata|load build definition|\[internal\]|sha256:)|--->\s+(?:Using cache|Running in|[0-9a-f]{12})|>\s*Task\s+:\S+\s+(?:UP-TO-DATE|NO-SOURCE|FROM-CACHE)|\[INFO\]\s*(?:Downloading|Downloaded|---|Building|Scanning|Skipping|Copying)|make\[\d+\]:\s*(?:Entering|Leaving) directory|(?:gcc|g\+\+|clang|clang\+\+|cc|c\+\+|rustc|ar|ranlib)\s+.*(?:-c\s|-o\s)|\[\s*\d+%\]\s+(?:Building|Scanning|Linking)|frame=\s*\d+\s+fps=|configuration:\s+--|lib(?:avutil|avcodec|avformat|avdevice|avfilter|swscale|swresample|postproc)\s+\d)};
    if ($rc == 0) {
        my @kept = grep {
            $_ !~ $noise &&
            $_ =~ /(?:warning\b|warn\b|error\b|Finished\b|BUILD SUCCESS|Successfully (?:built|tagged)|writing image|naming to|dist\/|Compiled successfully|Built in\b|Route \(|Size\s+First Load|video:\d+kB|Output #0)/i
        } @$lines;
        my $d = dedupe_consecutive(\@kept);
        return @$d ? tail_weighted_cap($d, 25, 40) : [@{$lines}[(@$lines > 12 ? -12 : -@$lines) .. -1]];
    } else {
        my @kept = grep { $_ !~ $noise || $_ =~ /error|fail|fatal|undefined/i } @$lines;
        my $ded = fold_stack_frames(dedupe_consecutive(\@kept));
        # RTK-inspired compiler error grouping when build fails with >60 diagnostic lines
        if (@$ded > 60) {
            my (%seen, %supp, @out);
            for my $l (@$ded) {
                if ($l eq '' || $l =~ /(?:aborting due to|error: could not compile|Found \d+ error|BUILD FAILED)/i) {
                    push @out, $l;
                    next;
                }
                my $k = $l; $k =~ s/^\s+//; $k =~ s/:\d+(?::\d+)?/:L/g; $k =~ s/\(\d+,\d+\)/(L)/g;
                $seen{$k}++;
                if ($seen{$k} <= 4) { push @out, $l; } else { $supp{$k}++; }
            }
            if (%supp) {
                $LOSSY = 1;
                push @out, '--- Repeated compiler errors collapsed ---';
                my @top = sort { $supp{$b} <=> $supp{$a} || $a cmp $b } keys %supp;
                for my $k (@top[0 .. ($#top > 4 ? 4 : $#top)]) {
                    my $sk = length($k) > 110 ? substr($k, 0, 107) . '...' : $k;
                    push @out, "  (+$supp{$k} more) $sk";
                }
                return tail_weighted_cap(\@out, 40, 120);
            }
        }
        return tail_weighted_cap($ded, 40, 120);
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
        my @top = sort { $supp{$b} <=> $supp{$a} || $a cmp $b } keys %supp;
        for my $k (@top[0 .. ($#top > 4 ? 4 : $#top)]) {
            my $sk = length($k) > 110 ? substr($k, 0, 107) . '...' : $k;
            push @out, "  (+$supp{$k} more) $sk";
        }
    }
    return tail_weighted_cap(\@out, 50, 80);
}


1;
