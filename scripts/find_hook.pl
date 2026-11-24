#!/usr/bin/env perl
use strict;
use warnings;
BEGIN {
    my $d = $0; $d =~ s{[^/\\]*$}{}; $d = '.' if $d eq ''; $d =~ s{[/\\]+$}{};
    unshift @INC, "$d/lib";
}
use TsJson;

# PreToolUse hook for IDE/Cortex find_by_name / find.
# Injects bloat Excludes only when it can actually trim results: Excludes omitted/empty, the dir
# exists as a direct child of SearchDirectory (else the ~23-token overwrite notice is pure
# overhead), and neither SearchDirectory nor Pattern targets that dir.

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

my $ti = TsJson::tool_input($doc);

# If non-empty Excludes array already provided, pass through immediately
if (ref($ti->{Excludes}) eq 'ARRAY' && @{$ti->{Excludes}} > 0) {
    print $ALLOW;
    exit 0;
}

my @excl = qw(node_modules .git dist build .next .nuxt target __pycache__ .venv venv vendor .cache coverage);
my $sdir = TsJson::get_str($ti, 'SearchDirectory') // '';
my $pat  = TsJson::get_str($ti, 'Pattern') // '';

if ($sdir ne '') {
    check_loop_guard(
        $doc,
        "find $sdir $pat",
        "[token-saver] Repeated identical find_by_name 3x with no edits since prior listing. Reuse earlier results or change Pattern (retry once to override).",
    );
}
my $home = $ENV{HOME} // $ENV{USERPROFILE} // '';
$sdir = $home . ($1 // '') if $sdir =~ m{^~([/\\].*)?$} && $home ne '';
if ($sdir eq '' || !-d $sdir) { print $ALLOW; exit 0; }

my @use = grep {
    my $d = $_;
    index($sdir, $d) < 0 && index($pat, $d) < 0 && -d "$sdir/$d"
} @excl;
if (!@use) { print $ALLOW; exit 0; }

my $arr = join(',', map { "\"$_\"" } @use);
print '{"decision":"allow","overwrite":{"Excludes":[' . $arr . ']}}';
exit 0;
