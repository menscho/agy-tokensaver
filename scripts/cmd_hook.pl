#!/usr/bin/env perl
use strict;
use warnings;

# Fast-path PreToolUse front stub for run_command / shell_exec.
# Most commands contain no compressible tool keyword anywhere in the payload: allow those
# without loading JSON::PP or compiling cmd_hook_core.pl (~2 ms). On any keyword hit (or
# \uXXXX escape), delegate to cmd_hook_core.pl which decodes the JSON payload with JSON::PP
# and runs the full quote-aware classifier. Fail-open always.

my $ALLOW = '{"decision":"allow"}';
if (lc($ENV{AGY_TS_SHELL} // '') eq 'off') { print $ALLOW; exit 0; }

binmode(STDIN);
my $MAX = 2 * 1024 * 1024;
my $raw = '';
while (length($raw) < $MAX + 1) {
    my $chunk = '';
    my $n = read(STDIN, $chunk, $MAX + 1 - length($raw));
    last if !defined $n || $n <= 0;
    $raw .= $chunk;
}
if ($raw eq '' || length($raw) > $MAX) { print $ALLOW; exit 0; }

# Conservative keyword pre-check on raw text (never parses JSON keys via regex).
# If \u escapes appear, skip the fast-path so JSON::PP decodes them first.
if (index($raw, '\\u') < 0) {
    (my $scan = $raw) =~ s/\\[nrtbf]/ /g;
    if ($scan !~ /\b(?:ssh|python[0-9.]*|py|pytest|jest|vitest|mocha|ava|playwright|cypress|bats|ctest|cargo|go|npm|npx|pnpm|yarn|bun|pip3?|uv(?:\.exe)?|poetry|pipenv|pdm|hatch|apt(?:-get)?|brew|composer|docker|podman|kubectl|helm|make|cmake|ninja|mvn|gradlew?|webpack|vite|next|esbuild|rollup|tsup|tsc|eslint|oxlint|biome|ruff|flake8|mypy|pyright|basedpyright|pylint|golangci-lint|rubocop|phpstan|shellcheck|hadolint|git|gh|rspec|phpunit|pest|dotnet|deno|mix|flutter|swift|zig|find|tree|grep|egrep|fgrep|rg|ag|ack|cat|curl|wget|tar|unzip|zip|rsync|terraform|tofu|ansible-playbook|aws|gcloud|az|journalctl|dmesg|systemctl|strings|tmux|ps|ls|du|df|pm2|ss|netstat|node|tox|nox|conda|mamba|remotion|ffmpeg|manage\.py|artisan|Get-ChildItem|gci|dir|Get-Item|Get-ItemProperty|Get-Process|Get-Service|Get-PSDrive|Get-Volume|Get-Disk|Get-NetTCPConnection|Get-NetAdapter|Get-CimInstance|Get-WmiObject|Get-Package|Get-Module|Get-AppxPackage|Get-WinEvent|Get-EventLog|Get-ScheduledTask|Get-Acl|Format-Table|Format-List|Select-String|winget|choco|scoop)(?:\.exe)?\b/i) {
        print $ALLOW;
        exit 0;
    }
}

our $RAW = $raw;
my $core = $0;
# Accept both / and \ separators (native Windows paths) and keep relative paths `do`-loadable
# ('.' is not in @INC since Perl 5.26, so "scripts/x.pl" must become "./scripts/x.pl").
$core =~ s{[^/\\]*$}{cmd_hook_core.pl};
$core = "./scripts/cmd_hook_core.pl" unless -f $core;
$core = "./$core" if $core !~ m{^(?:/|\\|[A-Za-z]:|\.{1,2}[/\\])};
do $core;
# Only reached if the core failed to load/run (it always exits itself)
print $ALLOW;
exit 0;
