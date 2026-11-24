#!/usr/bin/env perl
use strict;
use warnings;

# PreToolUse hook for `swarm`.
# Passes status/models/kill/title/model through immediately on the fast path.
# On `action: "add"`, appends a 25-token efficiency guardrail to `brief` (if not
# already present) so swarm workers skip repo-wide cold-reads and batch calls.

my $ALLOW = '{"decision":"allow"}';
binmode(STDIN);
my $raw = '';
my $MAX = 2 * 1024 * 1024;
while (length($raw) < $MAX + 1) {
    my $chunk = '';
    my $n = read(STDIN, $chunk, $MAX + 1 - length($raw));
    last if !defined $n || $n <= 0;
    $raw .= $chunk;
}
if ($raw eq '' || length($raw) > $MAX) { print $ALLOW; exit 0; }

# Conservative fast-path before loading JSON::PP (skip if "add" cannot appear even unescaped)
if (index($raw, 'add') < 0 && index($raw, '\\u') < 0) {
    print $ALLOW;
    exit 0;
}

{
    my $d = $0; $d =~ s{[^/\\]*$}{}; $d = '.' if $d eq ''; $d =~ s{[/\\]+$}{};
    unshift @INC, "$d/lib";
}
require TsJson;

my $doc = TsJson::decode_hook_input($raw);
if (!defined $doc) { print $ALLOW; exit 0; }

my $ti = TsJson::tool_input($doc);
my $action = TsJson::get_str($ti, 'action') // '';
if ($action ne 'add') { print $ALLOW; exit 0; }

my $brief = TsJson::get_str($ti, 'brief');
if (!defined $brief || $brief eq '' || index($brief, '[token-saver]') >= 0) {
    print $ALLOW;
    exit 0;
}

my $footer = "\n\n[token-saver] Read only assigned file:line slices via grep first (no unbounded repo cold-reads); batch parallel tool calls; use minimal-anchor edit_file; keep turns fast (<90s) and board updates brief (never paste full logs) so coordinator KV cache stays warm.";
my $new_brief = $brief . $footer;

print '{"decision":"allow","overwrite":{"brief":"' . TsJson::json_escape($new_brief) . '"}}';
exit 0;
