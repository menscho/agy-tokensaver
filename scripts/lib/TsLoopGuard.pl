use strict;
use warnings;

# Shared PreToolUse 3x-identical loop guard for read-only search/find hooks.
# Expects TsJson already loaded by caller.
sub check_loop_guard {
    my ($doc, $sig, $deny_msg) = @_;
    my $cid  = TsJson::get_str($doc, 'conversationId') // '';
    my $step = TsJson::get_int($doc, 'stepIdx');
    return if $cid eq '' || !defined $step || !defined $sig || $sig eq '';

    eval {
        local $/ = "\n";
        my $pr = $ENV{PLUGIN_ROOT} || do { my $s = $0; $s =~ s{/[^/]+/[^/]+$}{}; $s };
        my $agy_pd = ($ENV{HOME} // '') . "/.gemini/antigravity/plugin_data";
        my $dd = $ENV{PLUGIN_DATA} || (-d $agy_pd ? "$agy_pd/token-saver" : "$pr/.data");
        my $scid = $cid;
        $scid =~ s/[^A-Za-z0-9_-]/_/g;
        my $rdir = "$dd/reads";
        my $eseq = 0;
        if (-f "$rdir/$scid.editseq" && open(my $ef, '<', "$rdir/$scid.editseq")) {
            $eseq = int(<$ef> // 0);
            close $ef;
        }
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
        if (!-d $rdir) {
            require File::Path;
            File::Path::make_path($rdir);
        }
        if ($cnt >= 2 && ($step - $last_b) > 2) {
            push @rows, "$step\t$eseq\tblk\t$sig";
            @rows = @rows[-24 .. -1] if @rows > 24;
            if (open(my $wf, '>', $lf)) { print $wf join("\n", @rows) . "\n"; close $wf; }
            print '{"decision":"deny","reason":"' . TsJson::json_escape($deny_msg) . '"}';
            exit 0;
        }
        push @rows, "$step\t$eseq\tok\t$sig";
        @rows = @rows[-24 .. -1] if @rows > 24;
        if (open(my $wf, '>', $lf)) { print $wf join("\n", @rows) . "\n"; close $wf; }
    };
}

1;
