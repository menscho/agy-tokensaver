#!/usr/bin/env perl
use strict;
use warnings;
BEGIN {
    my $d = $0; $d =~ s{[^/\\]*$}{}; $d = '.' if $d eq ''; $d =~ s{[/\\]+$}{};
    unshift @INC, "$d/lib";
}
use TsJson;

# PreToolUse hook for grep_search.
#
# On Windows, grep_search with SearchPath pointing at a single FILE returns "No results found"
# even when the file contains matches, while SearchPath=<dir> + Includes=["<name>"] works.
# This hook rewrites a single-file Windows SearchPath to its parent directory + Includes=[basename].
# Also breaks 3x identical grep_search loops with no intervening edit (1-retry escape hatch).

my $ALLOW = '{"decision":"allow"}';
my $raw = TsJson::read_stdin_capped();
my $doc = TsJson::decode_hook_input($raw);
if (!defined $doc) { print $ALLOW; exit 0; }

my $ti  = TsJson::tool_input($doc);
my $sp0 = TsJson::get_str($ti, 'SearchPath') // '';
my $qry = TsJson::get_str($ti, 'Query') // '';

# Universal doom_loop guard (OpenCode + IAL-Scan arXiv:2607.01641): block 3rd identical grep_search
# with no intervening edit (editseq unchanged), preserving a 1-retry escape hatch.
{
    my $cid  = TsJson::get_str($doc, 'conversationId') // '';
    my $step = TsJson::get_int($doc, 'stepIdx');
    if ($cid ne '' && defined $step && $qry ne '') {
        eval {
            my $pr = $ENV{PLUGIN_ROOT} || do { my $s = $0; $s =~ s{/[^/]+/[^/]+$}{}; $s };
            my $agy_pd = ($ENV{HOME} // '') . "/.gemini/antigravity/plugin_data";
            my $dd = $ENV{PLUGIN_DATA} || (-d $agy_pd ? "$agy_pd/token-saver" : "$pr/.data");
            my $scid = $cid; $scid =~ s/[^A-Za-z0-9_-]/_/g;
            my $rdir = "$dd/reads";
            my $eseq = 0;
            if (-f "$rdir/$scid.editseq" && open(my $ef, '<', "$rdir/$scid.editseq")) { $eseq = int(<$ef> // 0); close $ef; }
            my $sig = "grep\t$sp0\t$qry";
            $sig =~ s/[\r\n\t]+/ /g;
            my $lf = "$rdir/$scid.loops";
            my @rows;
            my ($cnt, $last_b) = (0, -999);
            if (-f $lf && open(my $fh, '<', $lf)) {
                while (my $ln = <$fh>) {
                    chomp $ln;
                    my ($st_n, $eq, $fl, $s) = split(/\t/, $ln, 4);
                    next unless defined $s;
                    push @rows, $ln if $step - int($st_n) <= 40;
                    if ($s eq $sig && int($eq) == $eseq && $step >= int($st_n) && ($step - int($st_n)) <= 12) {
                        $cnt++ if $fl eq 'ok';
                        $last_b = int($st_n) if $fl eq 'blk' && int($st_n) > $last_b;
                    }
                }
                close $fh;
            }
            if (!-d $rdir) { require File::Path; File::Path::make_path($rdir); }
            if ($cnt >= 2 && ($step - $last_b) > 2) {
                push @rows, "$step\t$eseq\tblk\t$sig";
                @rows = @rows[-24 .. -1] if @rows > 24;
                if (open(my $wf, '>', $lf)) { print $wf join("\n", @rows) . "\n"; close $wf; }
                print '{"decision":"deny","reason":"[token-saver] Repeated identical grep_search 3x with no edits since prior matches. Reuse earlier results or broaden Query (retry once to override)."}';
                exit 0;
            }
            push @rows, "$step\t$eseq\tok\t$sig";
            @rows = @rows[-24 .. -1] if @rows > 24;
            if (open(my $wf, '>', $lf)) { print $wf join("\n", @rows) . "\n"; close $wf; }
        };
    }
}

my $out = eval {
    die "no path\n" if $sp0 eq '';
    my $sp = $sp0;
    # Windows absolute paths only (the bug was only ever observed there)
    die "not windows\n" unless $sp =~ m{^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/])};
    $sp =~ s{[\\/]+$}{};
    die "not a file\n" unless -f $sp;
    die "no parent\n" unless $sp =~ m{^(.*[\\/])([^\\/]+)$};
    my ($dir, $base) = ($1, $2);
    die "glob meta\n" if $base =~ /[*?\[\]{}!]/;
    # keep drive roots as "C:\"; strip the trailing separator otherwise
    $dir =~ s{[\\/]$}{} unless $dir =~ m{^[A-Za-z]:[\\/]$};
    die "no dir\n" unless $dir ne '' && -d $dir;
    '{"decision":"allow","overwrite":{"SearchPath":"' . TsJson::json_escape($dir)
        . '","Includes":["' . TsJson::json_escape($base) . '"]}}';
};
print(defined $out ? $out : $ALLOW);
exit 0;
