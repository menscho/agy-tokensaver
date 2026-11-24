#!/usr/bin/env perl
use strict;

# PostToolUse edit tracker (inspired by @plexer/cli post-tool span invalidation).
# Fires on edit_file / write_file / replace_file_content / multi_replace_file_content / write_to_file:
#   1) Increments per-conversation editseq (invalidates duplicate search / git diff cache).
#   2) Drops stale entries for the edited file from reads/<cid>.tsv so read_hook.pl stays exact.
# Always emits '{}' (0 tokens injected) and fails open on any error.

my $raw = do { local $/; <STDIN> };
if (!defined $raw || $raw eq '') { print '{}'; exit 0; }

sub json_unescape {
    my ($s) = @_;
    $s =~ s{\\(?:(["\\/bfnrt])|u([0-9a-fA-F]{4}))}{
        defined $1 ? (
            $1 eq 'n' ? "\n" : $1 eq 'r' ? "\r" : $1 eq 't' ? "\t" :
            $1 eq 'b' ? "\b" : $1 eq 'f' ? "\f" : $1
        ) : chr(hex($2))
    }ge;
    return $s;
}

sub json_get_str {
    my ($src, $key) = @_;
    return undef unless $src =~ /"\Q$key\E"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/;
    return json_unescape($1);
}

my $cid = json_get_str($raw, 'conversationId') // '';
if ($cid eq '') { print '{}'; exit 0; }

eval {
    my $plugin_root = $ENV{PLUGIN_ROOT};
    if (!$plugin_root) {
        my $s = $0; $s =~ s{\\}{/}g;
        $s = "$ENV{PWD}/$s" if $s !~ m{^(?:/|[A-Za-z]:/)} && $ENV{PWD};
        $s =~ s{/[^/]+/[^/]+$}{}; $plugin_root = $s;
    }
    my $home = $ENV{HOME} // $ENV{USERPROFILE} // '';
    my $agy_pd = "$home/.gemini/antigravity/plugin_data";
    my $dd = $ENV{PLUGIN_DATA} || (-d $agy_pd ? "$agy_pd/token-saver" : "$plugin_root/.data");
    my $rdir = "$dd/reads";
    if (!-d $rdir) { require File::Path; File::Path::make_path($rdir); }

    (my $scid = $cid) =~ s/[^A-Za-z0-9_-]/_/g;

    # 1. Bump editseq & clear cached searches / last-blocked key
    my $es_file = "$rdir/$scid.editseq";
    my $seq = 0;
    if (-f $es_file && open(my $efh, '<', $es_file)) { $seq = int(<$efh> // 0); close $efh; }
    $seq++;
    if (open(my $wfh, '>', $es_file)) { print $wfh "$seq\n"; close $wfh; }
    unlink("$rdir/$scid.searches", "$rdir/$scid.lastblk");

    # 2. Invalidate read_hook history entries for the edited file path
    my $path = json_get_str($raw, 'Path') // json_get_str($raw, 'TargetFile') // json_get_str($raw, 'AbsolutePath') // '';
    if ($path ne '') {
        if ($path =~ m{^~([/\\].*)?$} && $home ne '') {
            $path = $home . ($1 // '');
        } elsif ($path !~ m{^(?:/|[A-Za-z]:[/\\]|\\\\)}) {
            my $cwd = json_get_str($raw, 'cwd') // '';
            $path = "$cwd/$path" if $cwd ne '';
        }
        (my $pk = $path) =~ s{\\}{/}g;
        $pk = lc($pk) if $^O =~ /^(?:MSWin32|cygwin|msys)$/;
        my $rfile = "$rdir/$scid.tsv";
        if (-f $rfile && open(my $rfh, '<', $rfile)) {
            my @kept;
            while (my $rl = <$rfh>) {
                chomp $rl;
                my ($rp) = split(/\t/, $rl, 2);
                next if defined $rp && $rp eq $pk;
                push @kept, $rl if $rl ne '';
            }
            close $rfh;
            if (open(my $ofh, '>', $rfile)) {
                print $ofh join("\n", @kept) . (@kept ? "\n" : '');
                close $ofh;
            }
        }
    }
};

print '{}';
exit 0;
