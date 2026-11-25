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

my $LIB_DIR = do {
    my $d = $0; $d =~ s{[^/\\]*$}{}; $d = '.' if $d eq ''; $d =~ s{[/\\]+$}{};
    $d = "./$d" if $d !~ m{^(?:/|\\|[A-Za-z]:|\.{1,2}[/\\])};
    "$d/lib";
};
do "$LIB_DIR/TsLoopGuard.pl" or die "TsLoopGuard.pl: " . ($@ || $!);

my $ALLOW = '{"decision":"allow"}';
my $raw = TsJson::read_stdin_capped();
my $doc = TsJson::decode_hook_input($raw);
if (!defined $doc) { print $ALLOW; exit 0; }
if (TsJson::hooks_disabled()) { print $ALLOW; exit 0; }

my $ti  = TsJson::tool_input($doc);
my $sp0 = TsJson::get_str($ti, 'SearchPath') // '';
my $qry = TsJson::get_str($ti, 'Query') // '';

if ($qry ne '') {
    check_loop_guard(
        $doc,
        "grep\t$sp0\t$qry",
        "[token-saver] Repeated identical grep_search 3x with no edits since prior matches. Reuse earlier results or broaden Query (retry once to override).",
    );
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
