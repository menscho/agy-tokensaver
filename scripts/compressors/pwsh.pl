use strict;
use warnings;
our ($NATIVE_CAP, $LOSSY, $LOSSLESS_RM, $LOCKFILE_DIFF_RE, $EXTRA_SIG_RE, $cat);

load_compressor_family('json');

sub ps_table_compact {
    my ($lines) = @_;
    my @out;
    my $n = scalar(@$lines);
    my %seen_hdr;
    my $k = 0;
    while ($k < $n) {
        my $l = $lines->[$k];
        # "    Directory: C:\x" (localized: Verzeichnis/Répertoire/...) -> "Directory: C:\x"
        if ($l =~ /^ {2,}([^\s:][^:]{0,30}): +((?:[A-Za-z]:|\\\\|[\w.]+::).*)$/) {
            push @out, "$1: $2";
            $k++;
            next;
        }
        my $sep = $k + 1 < $n ? $lines->[$k + 1] : '';
        if ($l =~ /\S/ && $sep =~ /^ *-+(?: +-+)* *$/ && $l !~ /^ *-+(?: +-+)* *$/) {
            my @spans;
            while ($sep =~ /(-+)/g) { push @spans, [$-[1], $+[1]]; }
            my $last_st = $spans[-1][0];
            # Only padding runs (3+ spaces) before the last column shrink; the last column
            # (usually Name/path, may hold any spacing) stays verbatim. Pure whitespace edits.
            my $split = sub {
                my ($row) = @_;
                my ($pre, $suf) = length($row) > $last_st ? (substr($row, 0, $last_st), substr($row, $last_st)) : ($row, '');
                $pre =~ s/(?<=\S) {3,}/  /g;   # interior padding only (indentation kept)
                my $r = $pre . $suf;
                $r =~ s/\s+$//;
                return $r;
            };
            my $h = $split->($l);
            if (@spans >= 2) {
                (my $s2 = $sep) =~ s/-{4,}/---/g;
                $s2 =~ s/(?<=-) {2,}/  /g;
                $s2 =~ s/\s+$//;
                if ($seen_hdr{"$h\n$s2"}++) {
                    $LOSSLESS_RM += 2;          # identical header already shown above
                    while (@out >= 2 && $out[-1] eq '' && $out[-2] =~ /^[^\s:][^:]{0,30}: /) { pop @out; $LOSSLESS_RM++; }
                } else {
                    push @out, $h, $s2;
                }
                $k += 2;
                while ($k < $n && $lines->[$k] =~ /\S/) {
                    my $r = $split->($lines->[$k]);
                    last unless defined $r;
                    push @out, $r;
                    $k++;
                }
                next;
            }
        }
        # Format-List block: >= 2 consecutive "Name<pad> : value" lines
        if ($l =~ /^([A-Za-z_][\w .\-]{0,40}?) +: /) {
            my $j = $k;
            $j++ while $j < $n && ($lines->[$j] =~ /^[A-Za-z_][\w .\-]{0,40}? +: / || ($j > $k && $lines->[$j] =~ /^ {8,}\S/));
            if ($j - $k >= 2) {
                for my $q ($k .. $j - 1) {
                    my $x = $lines->[$q];
                    $x =~ s/^([A-Za-z_][\w .\-]{0,40}?) +: /$1 : / or $x =~ s/^ {8,}/  /;
                    push @out, $x;
                }
                $k = $j;
                next;
            }
        }
        push @out, $l;
        $k++;
    }
    return \@out;
}

# Pretty-printed JSON (indent=2/4, GitHub API, json.dumps(indent=..)): re-indent with one space
# per level and put arrays of plain scalars on one line. Whitespace-only => lossless.

sub cap_budget {
    my ($lines, $budget) = @_;
    $budget //= 5000;
    return $lines if total_bytes($lines) <= $NATIVE_CAP;
    my @l = map { cap_line_len($_, 400, 360) } @$lines;
    for my $ht ([40, 60], [25, 40], [15, 25], [8, 15], [4, 8]) {
        my $c = cap_with_signal(\@l, $ht->[0], $ht->[1], 60);
        return $c if total_bytes($c) <= $budget;
    }
    return cap_with_signal(\@l, 4, 8, 60);
}

sub ps_paths_group {
    my ($lines) = @_;
    my $np = grep { /^(?:[A-Za-z]:\\|\\\\)[^\t]*$/ } @$lines;
    return undef if @$lines < 46 || $np < @$lines * 0.9;
    my (%by_dir, @dir_order, @other);
    for my $l (@$lines) {
        my ($d, $f) = ($l =~ /^((?:[A-Za-z]:\\|\\\\).*)\\([^\\]+)$/) ? ($1, $2) : (undef, undef);
        if (!defined $d) { push @dir_order, "\0$l"; next; }
        push @dir_order, $d unless exists $by_dir{$d};
        push @{$by_dir{$d}}, $f;
    }
    my @out;
    for my $d (@dir_order) {
        if ($d =~ /^\0(.*)$/s) { push @out, $1; next; }
        my $line = "$d\\:";
        my $first = 1;
        for my $f (@{$by_dir{$d}}) {
            if (!$first && length($line) + length($f) > 236) { push @out, $line; $line = "  "; $first = 1; }
            $line .= ($first ? ' ' : ', ') . $f;
            $first = 0;
        }
        push @out, $line;
    }
    return @out < @$lines * 0.75 ? \@out : undef;
}

sub compress_pwsh {
    my ($lines, $rc) = @_;
    my $t = dedupe_consecutive(json_ws_compact(ps_table_compact($lines)));
    return $t if total_bytes($t) <= $NATIVE_CAP;
    my $g = ps_paths_group($t);
    return $g if defined $g && total_bytes($g) <= $NATIVE_CAP;
    # Python/.NET/PowerShell error lines (ValueError: x, System.IO.IOException: x, + CategoryInfo)
    local $EXTRA_SIG_RE = qr/^\s*[\w.]*(?:Error|Exception)\b\s*:|^\s*\+ (?:CategoryInfo|FullyQualifiedErrorId)\b|^\s*At line:\d/;
    return cap_budget(fold_stack_frames($g // $t), 5000);
}


1;
