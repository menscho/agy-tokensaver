use strict;
use warnings;
our ($NATIVE_CAP, $LOSSY, $LOSSLESS_RM, $LOCKFILE_DIFF_RE, $EXTRA_SIG_RE, $cat);

sub compress_test {
    my ($lines, $rc) = @_;
    my $pass_re = qr{(?:^\s*(?:PASSED|✓|✔|ok\s+\d+|test\s+\S+\s+\.\.\.\s+ok\b|---\s*PASS:|===\s*RUN\s+|Compiling\s+\S+|Downloading\s+\S+|Blocking waiting for file lock|PASS\s+\S+|collected\s+\d+\s+items|\.{10,}|[sSxX.]{4,}\s*(?:\[\s*\d+%\])?$|\S+\.py\s+[.sSxX]+\s*(?:\[\s*\d+%\])?$)|\s+PASSED(?:\s+\[\s*\d+%\])?$)};
    if ($rc == 0) {
        my @kept;
        for my $l (@$lines) {
            next if $l =~ $pass_re;
            next if $l =~ /^(?:platform |rootdir:|configfile:|plugins:|cachedir:)/;
            if ($l =~ /(?:\b\d+\s+(?:passed|failed|skipped|xfailed|xpassed|warnings?|errors?)\b|Test Suites?:|Tests?:|Snapshots?:|Time:|Ran \d+ tests?|test result:|ok\s+\S+\s+\d|BUILD SUCCESS|Summary|TOTAL|All files|WARN(?:ING)?\b|DEPRECATION\b|\|\s*\d+(?:\.\d+)?%)/i) {
                push @kept, $l;
            }
        }
        my $d = dedupe_consecutive(collapse_table_padding(\@kept));
        return @$d ? (@$d > 30 ? [@{$d}[-30 .. -1]] : $d) : [@{$lines}[(@$lines > 12 ? -12 : -@$lines) .. -1]];
    } else {
        my @kept;
        for my $l (@$lines) {
            next if $l =~ $pass_re && $l !~ /FAIL|ERROR|PANIC|ASSERT/i;
            push @kept, $l;
        }
        my $f = fold_stack_frames(dedupe_consecutive(\@kept));
        return tail_weighted_cap($f, 50, 130);
    }
}

sub compress_pkg {
    my ($lines, $rc) = @_;
    my $noise = qr{^\s*(?:Downloading\b|Unpacking\b|Using cached\b|Collecting\b|Resolving\b|Fetching\b|Preparing metadata|Building wheel|Installing collected|Requirement already satisfied|Get:\d+|Hit:\d+|Ign:\d+|Reading package lists|Building dependency tree|Reading state information|Setting up\b|Selecting previously unselected|Processing triggers|Compiling\s+\S+|Downloaded\s+\d+|Updating crates\.io|npm notice|packages? (?:are|is) looking for funding|run `npm fund`|\[notice\] A new release|==> Pouring|[|\/\-\\]\s*$|[#=>\-]{8,})}i;
    if ($rc == 0) {
        my @kept = grep {
            $_ !~ $noise &&
            $_ =~ /(?:added \d+|removed \d+|changed \d+|audited \d+|up to date|Successfully installed|Installed \d+|Upgraded|Resolved \d+|Prepared \d+|vulnerabilit|WARN(?:ING)?\b|ERR!|ERROR|Done in \d|Finished\b|\d+ upgraded)/i
        } @$lines;
        my $d = dedupe_consecutive(\@kept);
        return @$d ? (@$d > 25 ? [@{$d}[-25 .. -1]] : $d) : [@{$lines}[(@$lines > 10 ? -10 : -@$lines) .. -1]];
    } else {
        my @kept = grep { $_ !~ $noise || $_ =~ /err|fail|conflict|fatal/i } @$lines;
        return tail_weighted_cap(fold_stack_frames(dedupe_consecutive(\@kept)), 30, 100);
    }
}


1;
