#!/usr/bin/env perl
use strict;

# Zero-module PreToolUse hook for `swarm`.
# Passes status/models/kill/title/model through immediately.
# On `action: "add"`, appends a 25-token efficiency guardrail to `brief` (if not
# already present) so swarm workers skip repo-wide cold-reads and batch calls.

my $raw = do { local $/; <STDIN> };
if (!defined $raw || $raw eq '') { print '{"decision":"allow"}'; exit 0; }

# Fast-path: only touch swarm action=="add"
if ($raw !~ /"action"\s*:\s*"add"/) {
    print '{"decision":"allow"}';
    exit 0;
}

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
    $s =~ s/\n/\\n/g;
    $s =~ s/\r/\\r/g;
    $s =~ s/\t/\\t/g;
    $s =~ s/([\x00-\x1f])/sprintf('\\u%04x', ord($1))/ge;
    return $s;
}

my $brief;
if ($raw =~ /"brief"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/) {
    $brief = json_unescape($1);
}
if (!defined $brief || $brief eq '' || index($brief, '[token-saver]') >= 0) {
    print '{"decision":"allow"}';
    exit 0;
}

my $footer = "\n\n[token-saver] Read only assigned file:line slices via grep first (no unbounded repo cold-reads); batch parallel tool calls; use minimal-anchor edit_file; keep turns fast (<90s) and board updates brief (never paste full logs) so coordinator KV cache stays warm.";
my $new_brief = $brief . $footer;

print '{"decision":"allow","overwrite":{"brief":"' . json_escape($new_brief) . '"}}';
exit 0;
