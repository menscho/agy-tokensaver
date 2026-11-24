#!/usr/bin/env perl
use strict;
use warnings;
BEGIN {
    my $d = $0; $d =~ s{[^/\\]*$}{}; $d = '.' if $d eq ''; $d =~ s{[/\\]+$}{};
    unshift @INC, "$d/lib";
}
use TsJson;

# PostToolUse edit tracker (inspired by @plexer/cli post-tool span invalidation).
# Fires on edit_file / write_file / replace_file_content / multi_replace_file_content / write_to_file:
#   1) Increments per-conversation editseq (invalidates duplicate search / git diff cache).
#   2) Drops stale entries for the edited file from reads/<cid>.tsv so read_hook.pl stays exact.
# Always emits '{}' (0 tokens injected) and fails open on any error.

my $raw = TsJson::read_stdin_capped();
my $doc = TsJson::decode_hook_input($raw);
if (!defined $doc) { print '{}'; exit 0; }

my $cid = TsJson::get_str($doc, 'conversationId') // '';
if ($cid eq '') { print '{}'; exit 0; }

my $ti = TsJson::tool_input($doc);

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
    my $path = TsJson::get_str($ti, 'Path', 'TargetFile', 'AbsolutePath') // '';
    if ($path ne '') {
        if ($path =~ m{^~([/\\].*)?$} && $home ne '') {
            $path = $home . ($1 // '');
        } elsif ($path !~ m{^(?:/|[A-Za-z]:[/\\]|\\\\)}) {
            my $cwd = TsJson::get_str($doc, 'cwd') // TsJson::get_str($ti, 'cwd') // '';
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
