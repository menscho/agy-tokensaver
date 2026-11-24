#!/usr/bin/env perl
use strict;

# Zero-module PreToolUse front stub for run_command / shell_exec.
# ~83% of real commands match no compressible keyword: answer those without compiling the
# full classifier. On a keyword hit, load cmd_hook_core.pl (quote-aware safety checks,
# &&/;/newline chain classifier, exclude injection, cmd_wrap.pl rewrite). Fail-open always.

local $/;
my $raw = <STDIN>;
if (!defined $raw || $raw eq '' || $raw =~ /"IsDaemon"\s*:\s*true\b/) { print '{"decision":"allow"}'; exit 0; }

my ($c) = $raw =~ /"CommandLine"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/;
($c) = $raw =~ /"command"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/ unless defined $c;
if (!defined $c || $c eq '') { print '{"decision":"allow"}'; exit 0; }
$c =~ s/\\[nrtbf]/ /g; # JSON escapes must not glue words together (e.g. "x\nnpm")

# Windows host: Antigravity runs run_command in Windows PowerShell (ConPTY), never in bash.
# Git's perl reports $^O 'cygwin'/'msys', Strawberry 'MSWin32'. A POSIX-style Cwd (/tmp/x, as
# in tests run from Git Bash) keeps the POSIX path; AGY_TS_SHELL=posix|pwsh|off overrides.
our $PWSH = 0;
{
    my $sh = lc($ENV{AGY_TS_SHELL} // '');
    if ($sh eq 'off') { print '{"decision":"allow"}'; exit 0; }
    if ($sh =~ /^(?:pwsh|powershell)$/) { $PWSH = 1; }
    elsif ($sh !~ /^(?:posix|bash|sh)$/ && $^O =~ /^(?:MSWin32|cygwin|msys)$/) {
        my ($cw) = $raw =~ /"[Cc]wd"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/;
        # Antigravity passes native paths (C:\x, \\server) or none. "C:/x" is how Git Bash hands
        # POSIX paths to Windows programs (tests): those payloads carry a POSIX top-level "cwd".
        $PWSH = 1 if !defined $cw || $cw eq '' || $cw =~ /^(?:[A-Za-z]:(?:\\|$)|\\\\)/ ||
                     ($cw =~ m{^[A-Za-z]:/} && $raw !~ m{"cwd"\s*:\s*"/});
    }
}

if ($PWSH) {
    # PowerShell fast path: cmdlets with padded table/list output + noisy native tools
    if ($c !~ /\b(?:Get-ChildItem|gci|dir|ls|Get-Item|Get-ItemProperty|Get-Process|Get-Service|Get-PSDrive|Get-Volume|Get-Disk|Get-NetTCPConnection|Get-NetAdapter|Get-CimInstance|Get-WmiObject|Get-Package|Get-Module|Get-AppxPackage|Get-WinEvent|Get-EventLog|Get-ScheduledTask|Get-Acl|Format-Table|Format-List|Select-String|python[0-9.]*|py|pytest|node|npm|npx|pnpm|yarn|bun|uv|pip3?|git|gh|curl|zig|cargo|go|dotnet|deno|tsc|eslint|ruff|mypy|make|cmake|ninja|gradlew?|mvn|rg|docker|kubectl|winget|choco|scoop)(?:\.exe)?\b/i) {
        print '{"decision":"allow"}';
        exit 0;
    }
} else {
    if ($c !~ /\b(?:ssh|python3?|pytest|jest|vitest|mocha|ava|playwright|cypress|bats|ctest|cargo|go|npm|pnpm|yarn|bun|pip3?|uv(?:\.exe)?|poetry|pipenv|pdm|hatch|apt(?:-get)?|brew|composer|docker|podman|kubectl|helm|make|cmake|ninja|mvn|gradlew?|webpack|vite|next|esbuild|rollup|tsup|tsc|eslint|oxlint|biome|ruff|flake8|mypy|pyright|basedpyright|pylint|golangci-lint|rubocop|phpstan|shellcheck|hadolint|git|gh|rspec|phpunit|pest|dotnet|deno|mix|flutter|swift|zig|find|tree|grep|egrep|fgrep|rg|ag|ack|cat|curl|wget|tar|unzip|zip|rsync|terraform|tofu|ansible-playbook|aws|gcloud|az|journalctl|dmesg|systemctl|strings|tmux|ps|ls|du|df|pm2|ss|netstat|node|tox|nox|conda|mamba|remotion|ffmpeg|manage\.py|artisan)\b/i) {
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
print '{"decision":"allow"}';
exit 0;
