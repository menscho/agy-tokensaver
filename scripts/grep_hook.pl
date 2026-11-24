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
