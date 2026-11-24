#!/usr/bin/env perl
use strict;

# Zero-module PreToolUse hook for grep_search.
#
# On Windows, grep_search with SearchPath pointing at a single FILE returns "No results found"
# even when the file contains matches (16/16 recorded single-file calls failed this way, e.g.
# `<!DOCTYPE html>` in index.html), while SearchPath=<dir> + Includes=["<name>"] works. The model
# then burns extra turns on view_file / Select-String fallbacks. This hook rewrites a single-file
# SearchPath to its parent directory plus Includes=[basename] so the search returns the real
# matches (each match line still carries its full File path).
#
# Only acts when: SearchPath is an absolute Windows path (drive letter or UNC) that is an existing
# regular file and the basename has no glob metacharacters. Everything else (and any error) is a
# plain {"decision":"allow"}.

my $ALLOW = '{"decision":"allow"}';
my $raw = do { local $/; <STDIN> };
if (!defined $raw || $raw eq '' || index($raw, 'SearchPath') < 0) { print $ALLOW; exit 0; }

sub json_unescape {
    my ($s) = @_;
    $s =~ s{\\(?:(["\\/bfnrt])|u([dD][89abAB][0-9a-fA-F]{2})\\u([dD][c-fC-F][0-9a-fA-F]{2})|u([0-9a-fA-F]{4}))}{
        defined $1 ? (
            $1 eq 'n' ? "\n" : $1 eq 'r' ? "\r" : $1 eq 't' ? "\t" :
            $1 eq 'b' ? "\b" : $1 eq 'f' ? "\f" : $1
        ) : do {
            my $c = defined $2 ? chr(0x10000 + ((hex($2) - 0xD800) << 10) + (hex($3) - 0xDC00)) : chr(hex($4));
            utf8::encode($c); $c
        }
    }ge;
    return $s;
}

sub json_escape {
    my ($s) = @_;
    $s =~ s/\\/\\\\/g;
    $s =~ s/"/\\"/g;
    $s =~ s/([\x00-\x1f])/sprintf('\\u%04x', ord($1))/ge;
    return $s;
}

# Universal doom_loop guard (OpenCode + IAL-Scan arXiv:2607.01641): block 3rd identical grep_search
# with no intervening edit (editseq unchanged), preserving a 1-retry escape hatch.
{
    my $cid = ($raw =~ /"conversationId"\s*:\s*"([^"]+)"/) ? $1 : '';
    my $step = ($raw =~ /"stepIdx"\s*:\s*"?(-?\d+)"?/) ? int($1) : undef;
    my $qry = ($raw =~ /"Query"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/) ? json_unescape($1) : '';
    my $sp0 = ($raw =~ /"SearchPath"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/) ? json_unescape($1) : '';
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
            my (@rows, $cnt, $last_b) = ((), 0, -999);
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
    die "no path\n" unless $raw =~ /"SearchPath"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/;
    my $sp = json_unescape($1);
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
    '{"decision":"allow","overwrite":{"SearchPath":"' . json_escape($dir)
        . '","Includes":["' . json_escape($base) . '"]}}';
};
print(defined $out ? $out : $ALLOW);
exit 0;
