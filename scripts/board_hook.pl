#!/usr/bin/env perl
use strict;

# Zero-module PreToolUse poll-guard for swarm `read_board` (arXiv:2606.17107 / Manus).
# Blocks 3x consecutive identical read_board calls with the exact same `since` cursor
# within 6 steps (preserving a 1-retry escape hatch) so coordinators/workers use
# `swarm_wait` or advance `since` instead of polling and blowing coordinator KV caches.

my $ALLOW = '{"decision":"allow"}';
my $raw = do { local $/; <STDIN> };
if (!defined $raw || $raw eq '') { print $ALLOW; exit 0; }

my $cid  = ($raw =~ /"conversationId"\s*:\s*"([^"]+)"/) ? $1 : '';
my $step = ($raw =~ /"stepIdx"\s*:\s*"?(-?\d+)"?/) ? int($1) : undef;
if ($cid eq '' || !defined $step) { print $ALLOW; exit 0; }

my $since = ($raw =~ /"since"\s*:\s*"?(-?\d+)"?/) ? $1 : 'all';
my $pr = $ENV{PLUGIN_ROOT} || do { my $s = $0; $s =~ s{/[^/]+/[^/]+$}{}; $s };
my $agy_pd = ($ENV{HOME} // '') . "/.gemini/antigravity/plugin_data";
my $dd = $ENV{PLUGIN_DATA} || (-d $agy_pd ? "$agy_pd/token-saver" : "$pr/.data");
my $scid = $cid; $scid =~ s/[^A-Za-z0-9_-]/_/g;
my $rdir = "$dd/reads";
my $bf = "$rdir/$scid.board";

my $out = eval {
    my (@rows, $cnt, $last_b) = ((), 0, -999);
    if (-f $bf && open(my $fh, '<', $bf)) {
        while (my $ln = <$fh>) {
            chomp $ln;
            my ($st_n, $fl, $s) = split(/\t/, $ln, 3);
            next unless defined $s;
            push @rows, $ln if $step - int($st_n) <= 30;
            if ($s eq $since && $step >= int($st_n) && ($step - int($st_n)) <= 6) {
                $cnt++ if $fl eq 'ok';
                $last_b = int($st_n) if $fl eq 'blk' && int($st_n) > $last_b;
            }
        }
        close $fh;
    }
    if (!-d $rdir) { require File::Path; File::Path::make_path($rdir); }
    if ($cnt >= 2 && ($step - $last_b) > 2) {
        push @rows, "$step\tblk\t$since";
        @rows = @rows[-16 .. -1] if @rows > 16;
        if (open(my $wf, '>', $bf)) { print $wf join("\n", @rows) . "\n"; close $wf; }
        return '{"decision":"deny","reason":"[token-saver] Repeated identical read_board(since=' . $since . ') 3x. Use swarm_wait or advance since cursor to avoid burning board tokens and coordinator KV cache (retry once to override)."}';
    }
    push @rows, "$step\tok\t$since";
    @rows = @rows[-16 .. -1] if @rows > 16;
    if (open(my $wf, '>', $bf)) { print $wf join("\n", @rows) . "\n"; close $wf; }
    return $ALLOW;
};
print(defined $out ? $out : $ALLOW);
exit 0;
