#!/usr/bin/env perl
use strict;

# Zero-module PreToolUse hook for IDE/Cortex find_by_name / find.
# Injects bloat Excludes only when it can actually trim results: Excludes omitted/empty, the dir
# exists as a direct child of SearchDirectory (else the ~23-token overwrite notice is pure
# overhead), and neither SearchDirectory nor Pattern targets that dir.

local $/;
my $raw = <STDIN>;
if (!defined $raw || $raw eq '') { print '{"decision":"allow"}'; exit 0; }

# If non-empty Excludes array already provided, pass through immediately
if ($raw =~ /"Excludes"\s*:\s*\[\s*"/) {
    print '{"decision":"allow"}';
    exit 0;
}

sub jstr {
    my ($k) = @_;
    return '' unless $raw =~ /"\Q$k\E"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/;
    my $v = $1;
    $v =~ s/\\(["\\\/])/$1/g;
    return $v;
}

my @excl = qw(node_modules .git dist build .next .nuxt target __pycache__ .venv venv vendor .cache coverage);
my $sdir = jstr('SearchDirectory');
my $pat  = jstr('Pattern');

# Universal doom_loop guard (OpenCode + IAL-Scan arXiv:2607.01641): block 3rd identical find_by_name
# with no intervening edit, preserving a 1-retry escape hatch.
{
    my $cid = ($raw =~ /"conversationId"\s*:\s*"([^"]+)"/) ? $1 : '';
    my $step = ($raw =~ /"stepIdx"\s*:\s*"?(-?\d+)"?/) ? int($1) : undef;
    if ($cid ne '' && defined $step && $sdir ne '') {
        eval {
            local $/ = "\n";
            my $pr = $ENV{PLUGIN_ROOT} || do { my $s = $0; $s =~ s{/[^/]+/[^/]+$}{}; $s };
            my $agy_pd = ($ENV{HOME} // '') . "/.gemini/antigravity/plugin_data";
            my $dd = $ENV{PLUGIN_DATA} || (-d $agy_pd ? "$agy_pd/token-saver" : "$pr/.data");
            my $scid = $cid; $scid =~ s/[^A-Za-z0-9_-]/_/g;
            my $rdir = "$dd/reads";
            my $eseq = 0;
            if (-f "$rdir/$scid.editseq" && open(my $ef, '<', "$rdir/$scid.editseq")) { $eseq = int(<$ef> // 0); close $ef; }
            my $sig = "find $sdir $pat";
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
                print '{"decision":"deny","reason":"[token-saver] Repeated identical find_by_name 3x with no edits since prior listing. Reuse earlier results or change Pattern (retry once to override)."}';
                exit 0;
            }
            push @rows, "$step\t$eseq\tok\t$sig";
            @rows = @rows[-24 .. -1] if @rows > 24;
            if (open(my $wf, '>', $lf)) { print $wf join("\n", @rows) . "\n"; close $wf; }
        };
    }
}
my $home = $ENV{HOME} // $ENV{USERPROFILE} // '';
$sdir = $home . ($1 // '') if $sdir =~ m{^~([/\\].*)?$} && $home ne '';
if ($sdir eq '' || !-d $sdir) { print '{"decision":"allow"}'; exit 0; }

my @use = grep {
    my $d = $_;
    index($sdir, $d) < 0 && index($pat, $d) < 0 && -d "$sdir/$d"
} @excl;
if (!@use) { print '{"decision":"allow"}'; exit 0; }

my $arr = join(',', map { "\"$_\"" } @use);
print '{"decision":"allow","overwrite":{"Excludes":[' . $arr . ']}}';
exit 0;
