#!/usr/bin/env perl
use strict;

# Zero-module PreToolUse front stub for run_command / shell_exec.
# ~83% of real commands match no compressible keyword: answer those without compiling the
# full classifier. On a keyword hit, load cmd_hook_core.pl (quote-aware safety checks,
# &&/;/newline chain classifier, exclude injection, cmd_wrap.pl rewrite). Fail-open always.

local $/;
my $raw = <STDIN>;
if (!defined $raw || $raw eq '' || $raw =~ /"IsDaemon"\s*:\s*true\b/) { print '{"decision":"allow"}'; exit 0; }
# On native Windows (MSWin32), Antigravity executes commands in PowerShell; cmd_wrap.pl uses POSIX bash -c.
if ($^O eq 'MSWin32') { print '{"decision":"allow"}'; exit 0; }

my ($c) = $raw =~ /"CommandLine"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/;
($c) = $raw =~ /"command"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/ unless defined $c;
if (!defined $c || $c eq '') { print '{"decision":"allow"}'; exit 0; }
$c =~ s/\\[nrtbf]/ /g; # JSON escapes must not glue words together (e.g. "x\nnpm")

if ($c !~ /\b(?:ssh|python3?|pytest|jest|vitest|mocha|ava|playwright|cypress|bats|ctest|cargo|go|npm|pnpm|yarn|bun|pip3?|uv(?:\.exe)?|poetry|pipenv|pdm|hatch|apt(?:-get)?|brew|composer|docker|podman|kubectl|helm|make|cmake|ninja|mvn|gradlew?|webpack|vite|next|esbuild|rollup|tsup|tsc|eslint|oxlint|biome|ruff|flake8|mypy|pyright|basedpyright|pylint|golangci-lint|rubocop|phpstan|shellcheck|hadolint|git|gh|rspec|phpunit|pest|dotnet|deno|mix|flutter|swift|zig|find|tree|grep|egrep|fgrep|rg|ag|ack|cat|curl|wget|tar|unzip|zip|rsync|terraform|tofu|ansible-playbook|aws|gcloud|az|journalctl|dmesg|systemctl|strings|tmux|ps|ls|du|df|pm2|ss|netstat|node|tox|nox|conda|mamba|remotion|manage\.py|artisan)\b/i) {
    print '{"decision":"allow"}';
    exit 0;
}

# Cheap pre-rejects the core would always pass through anyway (same rules, no quote parsing
# needed): for quote-free commands, any heredoc/redirect (other than 2>&1, 2>/dev/null,
# </dev/null) or command substitution. Pipes and `|| true` are evaluated safely in core.
if ($c !~ /["'\\]/) {
    (my $u = $c) =~ s{2>&1|2>\s*/dev/null|<\s*/dev/null}{}g;
    if ($u =~ /[<>`]|\$\(/) { print '{"decision":"allow"}'; exit 0; }
}

our $RAW = $raw;
my $core = $0;
$core =~ s{[^/]*$}{cmd_hook_core.pl};
$core = "./scripts/cmd_hook_core.pl" unless -f $core;
do $core;
# Only reached if the core failed to load/run (it always exits itself)
print '{"decision":"allow"}';
exit 0;
