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
