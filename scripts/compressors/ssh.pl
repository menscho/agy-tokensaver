use strict;
use warnings;
our ($NATIVE_CAP, $LOSSY, $LOSSLESS_RM, $LOCKFILE_DIFF_RE, $EXTRA_SIG_RE, $cat);

load_compressor_family('log');

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
    # Remote output <= native cap stays complete (lossless transforms + dense-line elide for huge minified blobs)
    return elide_dense_lines($base) if total_bytes($base) <= $NATIVE_CAP;
    my $t = dedupe_log_templates(fold_stack_frames(elide_dense_lines($base)));
    return $rc != 0 ? cap_with_signal($t, 40, 110, 60) : cap_with_signal($t, 35, 85, 60);
}


1;
