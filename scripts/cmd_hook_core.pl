#!/usr/bin/env perl
use strict;

# Core classifier for the run_command / shell_exec PreToolUse hook (see cmd_hook.pl).
# - Quote-aware metacharacter safety check & &&-chain classifier.
# - Auto-injects --exclude-dir/-I on recursive grep, -prune on find, -I on tree.
# - Wraps high-ROI noisy categories (incl. ssh, test, pkg, build, lint, git_diff,
#   git_log, git_net, search, http, archive, iac, k8s_yaml, cloud, logs, ps, table,
#   cat_large, container, find, chain) via zero-module Perl cmd_wrap.pl.
# - Strict fail-open on any edge case or error.

# Loaded via `do` from cmd_hook.pl (front stub) only when the keyword fast path matches;
# can also run standalone (reads stdin).
our $RAW;
my $raw = $RAW;
if (!defined $raw) { $raw = do { local $/; <STDIN> }; }
if (!defined $raw || $raw eq '') { print '{"decision":"allow"}'; exit 0; }

sub json_unescape {
    my ($s) = @_;
    # \uXXXX (incl. surrogate pairs) -> UTF-8 bytes, so rewritten args stay valid UTF-8
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

sub json_get_str {
    my ($src, $key) = @_;
    return undef unless $src =~ /"\Q$key\E"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"/;
    return json_unescape($1);
}

# Never wrap background daemons
if ($raw =~ /"IsDaemon"\s*:\s*true\b/) { print '{"decision":"allow"}'; exit 0; }

my $cmd_key;
my $cmd = json_get_str($raw, 'CommandLine');
if (defined $cmd) { $cmd_key = 'CommandLine'; }
else {
    $cmd = json_get_str($raw, 'command');
    $cmd_key = 'command' if defined $cmd;
}
if (!defined $cmd || $cmd eq '') { print '{"decision":"allow"}'; exit 0; }

# Skip if already wrapped by cmd_wrap
if ($cmd =~ /cmd_wrap\.(?:pl|py)\S*\s+--cat\b/) { print '{"decision":"allow"}'; exit 0; }

# Windows host => Antigravity executes the command line in Windows PowerShell 5.1 (ConPTY).
# The POSIX path below (bash -c) must never run there; use the PowerShell wrapper instead.
our $PWSH;
if (!defined $PWSH) {
    my $sh = lc($ENV{AGY_TS_SHELL} // '');
    my $cw = json_get_str($raw, 'Cwd') // json_get_str($raw, 'cwd') // '';
    $PWSH = ($sh =~ /^(?:pwsh|powershell)$/ || ($sh !~ /^(?:posix|bash|sh|off)$/ && $^O =~ /^(?:MSWin32|cygwin|msys)$/ &&
             ($cw eq '' || $cw =~ /^(?:[A-Za-z]:(?:\\|$)|\\\\)/ || ($cw =~ m{^[A-Za-z]:/} && $raw !~ m{"cwd"\s*:\s*"/})))) ? 1 : 0;
    if ($sh eq 'off') { print '{"decision":"allow"}'; exit 0; }
}

# ---------------------------------------------------------------------------------------------
# PowerShell path. The rewritten command runs the model's EXACT script text in a child
# `powershell.exe -NoProfile -NonInteractive -EncodedCommand` (cmd_wrap.pl), so PowerShell
# parsing/quoting semantics are byte-identical; only stdout+stderr are captured & compressed.
# ---------------------------------------------------------------------------------------------

# Mask string literals, here-strings and comments (quote-aware) so checks see only code.
sub ps_mask {
    my ($s) = @_;
    $s =~ s/\xe2\x80[\x98-\x9b]/'/g;   # PowerShell treats smart single quotes as '
    $s =~ s/\xe2\x80[\x9c-\x9e]/"/g;   # ... and smart double quotes as "
    my $out = '';
    my $len = length($s);
    my $i = 0;
    while ($i < $len) {
        my $c = substr($s, $i, 1);
        if ($c eq '@' && $i + 1 < $len && substr($s, $i + 1, 1) =~ /['"]/ && substr($s, $i + 2) =~ /^[ \t]*\r?\n/) {
            my $q = substr($s, $i + 1, 1);
            my $end = index($s, "\n$q\@", $i + 2);
            return undef if $end < 0;              # unterminated here-string
            $out .= ' S ';
            $i = $end + 3;
            next;
        }
        if ($c eq "'") {
            my $j = $i + 1;
            while (1) {
                $j = index($s, "'", $j);
                return undef if $j < 0;
                if (substr($s, $j + 1, 1) eq "'") { $j += 2; next; }
                last;
            }
            $out .= ' S ';
            $i = $j + 1;
            next;
        }
        if ($c eq '"') {
            my $j = $i + 1;
            while ($j < $len) {
                my $d = substr($s, $j, 1);
                if ($d eq '`') { $j += 2; next; }
                if ($d eq '"') { if (substr($s, $j + 1, 1) eq '"') { $j += 2; next; } last; }
                $j++;
            }
            return undef if $j >= $len;
            $out .= ' S ';
            $i = $j + 1;
            next;
        }
        if ($c eq '<' && substr($s, $i + 1, 1) eq '#') {
            my $end = index($s, '#>', $i + 2);
            return undef if $end < 0;
            $out .= ' ';
            $i = $end + 2;
            next;
        }
        if ($c eq '#' && ($i == 0 || substr($s, $i - 1, 1) =~ /[\s;|{(]/)) {
            my $end = index($s, "\n", $i);
            $i = $end < 0 ? $len : $end;
            next;
        }
        if ($c eq '`') { $out .= ' '; $i += 2; next; }
        $out .= $c;
        $i++;
    }
    return $out;
}

my %PS_NEUTRAL = map { $_ => 1 } qw(
    write-output echo write write-host select-object select where-object where ? foreach-object foreach %
    sort-object sort measure-object measure group-object group out-string out-null test-path join-path
    split-path resolve-path copy-item cp copy cpi move-item mv move mi remove-item rm del erase ri rmdir rd
    rename-item ren rni new-item ni mkdir md if else elseif for while do switch try catch finally param
    return break continue throw get-date convertto-json convertfrom-json get-content gc cat type
    out-file set-content sc add-content ac tee-object get-filehash get-location gl pwd select-xml
    write-warning write-error write-verbose write-information write-debug get-itemproperty-value
    format-wide fw get-member gm get-random get-unique compare-object diff test-json
    expand-archive compress-archive invoke-restmethod irm invoke-webrequest iwr clear-host cls
    convertto-csv convertfrom-csv export-csv import-csv true false null
);
my %PS_TABLE = map { $_ => 1 } qw(
    get-childitem gci dir ls get-item gi get-itemproperty gp get-process gps ps get-service gsv get-psdrive
    gdr get-volume get-disk get-partition get-nettcpconnection get-netadapter get-netipaddress get-ciminstance
    gcim get-wmiobject gwmi get-package get-module gmo get-appxpackage get-winevent
    get-eventlog get-scheduledtask get-acl format-table ft format-list fl get-hotfix get-printer
    get-localuser get-computerinfo get-psprovider get-installedmodule
);

# Category of one pipeline element: '' neutral, '?' unknown, '!' never wrap, or a category
sub ps_seg_cat {
    my ($seg) = @_;
    $seg =~ s/^\s+|\s+$//g;
    return '' if $seg eq '';
    $seg =~ s/^(?:[&.]\s*)//;                    # call / dot operator (dot-source rejected earlier)
    return '' if $seg =~ /^[\$\d@\[,\-!]/ || $seg =~ /^S\b/;   # expressions, literals, operators
    my @t = split(/\s+/, $seg);
    my $bin = lc(shift @t);
    $bin =~ s{^.*[\\/]}{};
    $bin =~ s/\.(?:exe|cmd|bat)$//;
    my $args = ' ' . join(' ', @t) . ' ';
    my @na = grep { !/^-/ && $_ ne 'S' } @t;
    my $sub = lc($na[0] // '');
    my $sub2 = lc($na[1] // '');
    return '' if $PS_NEUTRAL{$bin};
    return 'pwsh' if $PS_TABLE{$bin};
    return '!' if $args =~ /\s(?:--watch|-w|--follow|-f|--serve|serve|--interactive|-i|-it)\s/i && $bin !~ /^(?:git|curl|rg|python\d*(?:\.\d+)?|py|select-string|sls|zig)$/;
    return '' if $args =~ /\s(?:--help|-h|--version|-V|--json|--porcelain|-q|--quiet)\s/ && $bin ne 'curl';
    if ($bin =~ /^(?:python\d*(?:\.\d+)?|py|pythonw)$/) {
        return '!' if !@t;                                        # REPL
        return 'test' if $args =~ /\s-m\s+(?:pytest|unittest|tox|nox)\s/;
        return 'pkg'  if $args =~ /\s-m\s+pip\s+(?:install|download|wheel|uninstall)\s/;
        return '!'    if $args =~ /\s-m\s+(?:http\.server|flask|uvicorn|streamlit|gunicorn|jupyter|idlelib|venv)\s/;
        # Inline/ad-hoc scripts print exactly what the model asked for (median ~0.5 KB, no
        # padding): measured net-negative after the 94 B overwrite notice => never a reason to wrap
        return '' if $args =~ /\s-c\s/ || $args =~ /\s-\s/ || $args =~ /\.py\s/i || $args =~ /\s-m\s/;
        return '?';
    }
    if ($bin eq 'uv' || $bin eq 'uvx') {
        if ($bin eq 'uv' && $sub eq 'run') {
            my $rest = $seg;
            $rest =~ s/^\S+\s+run\s+//i;
            $rest =~ s/^(?:(?:--with|--python|-p|--directory|--project|--extra|--group|--env-file)\s+\S+\s+|--\S+\s+)*//;
            return ps_seg_cat($rest);
        }
        return 'pkg' if $sub =~ /^(?:pip|sync|add|lock|venv|remove|tool)$/ || ($sub eq 'python' && $sub2 eq 'install');
        return '?';
    }
    return 'pkg' if $bin =~ /^pip\d*$/ && $sub =~ /^(?:install|download|wheel|uninstall)$/;
    return 'pwsh' if $bin =~ /^pip\d*$/ && $sub =~ /^(?:list|freeze)$/;
    return 'test' if $bin =~ /^(?:pytest|jest|vitest|mocha|tox|nox|ctest)$/;
    if ($bin eq 'node') {
        return '!' if !@t;
        return 'test' if $args =~ /\s--test\s/;
        return '' if $args =~ /\s-[ep]\s/ || $args =~ /\.(?:c|m)?js\s/i;
        return '?';
    }
    if ($bin =~ /^(?:npm|pnpm|yarn|bun)$/) {
        my $s2 = $sub eq 'run' ? $sub2 : $sub;
        return '!' if $s2 =~ /^(?:dev|start|serve|watch|preview|studio)(?::|$)/ || $sub =~ /^(?:exec|dlx|x|create|init)$/;
        return 'test' if $s2 =~ /^test(?::|$)/;
        return 'pkg' if $sub =~ /^(?:install|i|ci|add|update|upgrade|audit|remove|uninstall)$/;
        return 'build' if $s2 =~ /^(?:build|typecheck|check|compile)(?::|$)/;
        return 'lint' if $s2 =~ /^lint(?::|$)/;
        return '?';
    }
    if ($bin =~ /^(?:npx|bunx)$/) {
        return 'build' if $sub =~ /^(?:tsc|webpack|esbuild|rollup|tsup)$/ || ($sub eq 'remotion' && $sub2 =~ /^(?:bundle|render|still)$/) || ($sub =~ /^(?:vite|next)$/ && $sub2 eq 'build');
        return 'lint' if $sub =~ /^(?:eslint|biome|oxlint|prettier)$/;
        return 'test' if $sub =~ /^(?:vitest|jest|playwright|mocha)$/;
        return '?';
    }
    if ($bin eq 'git') {
        return '' if $sub =~ /^(?:add|commit|checkout|switch|stash|push|tag|mv|rm|restore|reset|init|config|status|rev-parse|branch|remote)$/;
        return '!' if $sub =~ /^(?:rebase|bisect|mergetool|difftool|gui|citool)$/;
        return '' if $sub eq 'show' && $args =~ /\s[^-\s][^\s:]*:/;
        return 'git_diff' if $sub =~ /^(?:diff|show)$/;
        if ($sub eq 'log') {
            return '' if $args =~ /\s(?:-p|-s|--oneline|--graph|-[1-9]|-n\s*(?:[1-9]|1[0-5]))\s/;
            return 'git_log';
        }
        return 'git_net' if $sub =~ /^(?:clone|fetch|pull)$/;
        return '?';
    }
    if ($bin eq 'gh') {
        return 'git_diff' if $sub eq 'pr' && $sub2 eq 'diff';
        return 'logs' if $sub eq 'run' && $sub2 eq 'view';
        return 'table' if $sub =~ /^(?:pr|issue|run|workflow)$/ && $sub2 =~ /^(?:list|view|checks)$/;
        return '?';
    }
    if ($bin eq 'curl') {
        return '?' if $seg !~ /^\S*curl\.exe\b/i;   # bare `curl` is Invoke-WebRequest in PowerShell 5.1
        return '' if $args =~ /\s(?:-I|--head|-w|--write-out|-o|--output|-O|--remote-name)\s/;
        return '';
    }
    if ($bin eq 'zig') {
        return 'test' if $sub eq 'test' || ($sub eq 'build' && $sub2 eq 'test');
        return 'build' if $sub =~ /^(?:build|build-exe|build-lib|build-obj)$/;
        return '!' if $sub eq 'run';
        return '?';
    }
    return 'test' if ($bin =~ /^(?:cargo|go|dotnet|deno)$/ && $sub eq 'test');
    return 'build' if ($bin eq 'cargo' && $sub =~ /^(?:build|check)$/) || ($bin eq 'go' && $sub =~ /^(?:build|vet)$/) || ($bin eq 'dotnet' && $sub =~ /^(?:build|publish|restore)$/);
    return 'lint' if $bin eq 'cargo' && $sub eq 'clippy';
    return 'build' if $bin =~ /^(?:tsc|make|cmake|ninja|mvn|gradle|gradlew|msbuild)$/;
    return 'lint' if $bin =~ /^(?:eslint|ruff|mypy|pyright|pylint|flake8|biome)$/;
    return '' if $bin =~ /^(?:rg|select-string|sls|findstr)$/;
    return 'pkg' if $bin =~ /^(?:winget|choco|scoop)$/ && $sub =~ /^(?:install|upgrade|update|uninstall)$/;
    if ($bin eq 'docker' || $bin eq 'podman') {
        return 'build' if $sub eq 'build' || ($sub eq 'compose' && $sub2 eq 'build');
        return 'table' if $sub =~ /^(?:ps|images)$/;
        return 'logs' if $sub eq 'logs';
        return '?';
    }
    return 'logs' if $bin eq 'kubectl' && $sub eq 'logs';
    return '?';
}

# Windows path of a file (Git/Cygwin perl paths are POSIX: /c/x or /cygdrive/c/x)
sub win_path {
    my ($p) = @_;
    if ($^O ne 'MSWin32' && $p =~ m{^/}) {
        my $w = eval { Cygwin::posix_to_win_path($p) };
        return $w if defined $w && $w =~ /^(?:[A-Za-z]:|\\\\)/;
        return undef;
    }
    $p =~ s{/}{\\}g;
    return $p;
}

sub pwsh_hook {
    my ($cmd, $key) = @_;
    my $allow = '{"decision":"allow"}';
    my $s = $cmd;
    $s =~ s/^\s+|\s+$//g;
    # -EncodedCommand (UTF-16 base64) of ~9k chars stays below the 32,767-char CreateProcess limit
    if ($s eq '' || length($s) > 9000) { print $allow; return; }
    my $m = ps_mask($s);
    if (!defined $m) { print $allow; return; }
    # Session-state changes would not survive the child shell; `exit N` codes would be remapped;
    # interactive/streaming/long-running constructs must keep their live console.
    if ($m =~ /\bexit\b/i || $m =~ /\$(?:env:\w+|global:\w+)\s*=(?!=)/i ||
        $m =~ /(?:^|[;|{(\n]|&&)\s*(?:cd|chdir|sl|set-location|push-location|pushd|popd|pop-location|function|filter)(?:\s|$|;)/i ||
        $m =~ /(?:^|[;\n])\s*\.\s+[^\s.]/ ||
        $m =~ /\b(?:Set-Alias|New-Alias|Import-Module|Enter-PSSession|Read-Host|Get-Credential|Start-Job|Receive-Job|Start-Process|Wait-Process|Wait-Job|Start-Sleep|sleep|Register-\w+|Start-Transcript|Set-Variable|New-Variable|Remove-Variable|Out-GridView|Out-Host|Write-Progress|Invoke-Item|ii|Set-PSDebug|Pause|Clear-Variable|Add-Type|Update-TypeData|Set-ExecutionPolicy|Enable-\w+|Disable-\w+)\b/i ||
        $m =~ /\bwhile\s*\(\s*\$true\s*\)/i) {
        print $allow;
        return;
    }
    my (%cats, $unknown, $never);
    for my $seg (split(/[;|\n{}()]|&&/, $m)) {
        my $c = ps_seg_cat($seg);
        next if $c eq '';
        if ($c eq '!') { $never = 1; last; }
        if ($c eq '?') { $unknown = 1; next; }
        $cats{$c}++;
    }
    if ($never || $unknown || !%cats) { print $allow; return; }
    my @c = sort keys %cats;
    my $cat;
    if (@c == 1) { $cat = $c[0]; }
    elsif (grep { $_ eq 'git_diff' } @c) { print $allow; return; }
    elsif (grep { /^(?:test|build|pkg|lint|git_net|logs)$/ } @c) { $cat = 'chain'; }
    else { $cat = 'pwsh'; }

    # Absolute Windows paths of perl and cmd_wrap.pl (the command runs in the workspace cwd)
    my $script = $0;
    if ($script !~ m{^(?:/|\\|[A-Za-z]:)}) {
        my $cwd = eval { require Cwd; Cwd::getcwd() };
        if (!defined $cwd || $cwd eq '') { print $allow; return; }
        $script = "$cwd/$script";
    }
    $script =~ s{[^/\\]*$}{cmd_wrap.pl};
    1 while $script =~ s{[/\\]\.(?=[/\\])}{};
    if (!-f $script) { print $allow; return; }
    my $wrap = win_path($script);
    my $perl = $^O eq 'MSWin32' ? $^X : (readlink('/proc/self/exe') // '');
    $perl = win_path($perl) if $perl ne '' && $^O ne 'MSWin32';
    $perl = 'perl' if !defined $perl || $perl eq '' || $perl !~ /perl[^\\\/]*$/i;
    if (!defined $wrap) { print $allow; return; }
    (my $qp = $perl) =~ s/'/''/g;
    (my $qw = $wrap) =~ s/'/''/g;
    my $call = "& '$qp' '$qw' --cat $cat";
    my $new;
    if ($s !~ /[^\x09\x0a\x0d\x20-\x7e]/ && $s !~ /(?:^|\n)'\@/) {
        # Readable form: the exact script travels on stdin via a literal here-string (ASCII only,
        # so the pipe encoding cannot alter it)
        $new = "\@'\n$s\n'\@ | $call --pwsh-stdin";
    } else {
        $new = "$call --pwsh-b64 '" . b64_encode($s) . "'";
    }
    print '{"decision":"allow","overwrite":{"' . $key . '":"' . json_escape($new) . '"}}';
}

sub b64_encode {
    my ($s) = @_;
    my $u = '';
    $u .= pack('u', substr($s, $_ * 45, 45)) =~ s/^.//r =~ s/\n$//r for 0 .. int((length($s) - 1) / 45);
    $u =~ tr{` -_}{AA-Za-z0-9+/};
    my $pad = (3 - length($s) % 3) % 3;
    substr($u, -$pad) = '=' x $pad if $pad;
    return $u;
}

if ($PWSH) { pwsh_hook($cmd, $cmd_key); exit 0; }
# Native Windows perl without PowerShell mode: no bash to run POSIX wrappers
if ($^O eq 'MSWin32') { print '{"decision":"allow"}'; exit 0; }

# Skip PowerShell-specific syntax (never hand $env:X or PowerShell cmdlets to bash -c)
if ($cmd =~ /\$env:/i || $cmd =~ /(?:^|[\s;|])(?:Get|Set|New|Remove|Invoke|Select|Where|ForEach|Write|Out|Test|Start|Stop)-[A-Z][A-Za-z]+/ || $cmd =~ /\s-(?:ErrorAction|OutVariable)\b/i) {
    print '{"decision":"allow"}';
    exit 0;
}

# Fast-path keyword check ONLY on CommandLine (never on cwd/toolSummary)
if ($cmd !~ /\b(?:ssh|python3?|pytest|jest|vitest|mocha|ava|playwright|cypress|bats|ctest|cargo|go|npm|pnpm|yarn|bun|pip3?|uv(?:\.exe)?|poetry|pipenv|pdm|hatch|apt(?:-get)?|brew|composer|docker|podman|kubectl|helm|make|cmake|ninja|mvn|gradlew?|webpack|vite|next|esbuild|rollup|tsup|tsc|eslint|oxlint|biome|ruff|flake8|mypy|pyright|basedpyright|pylint|golangci-lint|rubocop|phpstan|shellcheck|hadolint|git|gh|rspec|phpunit|pest|dotnet|deno|mix|flutter|swift|zig|find|tree|grep|egrep|fgrep|rg|ag|ack|cat|curl|wget|tar|unzip|zip|rsync|terraform|tofu|ansible-playbook|aws|gcloud|az|journalctl|dmesg|systemctl|strings|tmux|ps|ls|du|df|pm2|ss|netstat|node|tox|nox|conda|mamba|remotion|ffmpeg|manage\.py|artisan)\b/i) {
    print '{"decision":"allow"}';
    exit 0;
}

my $cwd = json_get_str($raw, 'Cwd') // json_get_str($raw, 'cwd') // '';
$cwd = ($ENV{HOME} // '') . $1 if $cwd =~ m{^~(/.*)?$} && defined $ENV{HOME};
# Single-arg wrapped command near ARG_MAX/MAX_ARG_STRLEN (128KB) would fail with E2BIG
if (length($cmd) > 60000) { print '{"decision":"allow"}'; exit 0; }

# Resolve a user path against cwd with ~ expansion (for stat-based predictors)
sub res_path {
    my ($p) = @_;
    my $home = $ENV{HOME} // $ENV{USERPROFILE} // '';
    return $home . ($1 // '') if $p =~ m{^~([/\\].*)?$} && $home ne '';
    return ($cwd ne '' && $p !~ m{^(?:/|[A-Za-z]:[/\\]|\\\\)}) ? "$cwd/$p" : $p;
}

my $trimmed = $cmd;
$trimmed =~ s/^\s+|\s+$//g;

# Multiline scripts: CR is never expected; unquoted newlines act like ';' (heredocs rejected below)
if ($trimmed =~ /\r/) { print '{"decision":"allow"}'; exit 0; }

# Quote-aware masking:
# 1) Strip single-quoted segments for command-substitution check ($(...) and `...` expand in double quotes)
my $no_sq = $trimmed;
$no_sq =~ s/'[^']*'/__SQ__/g;
if ($no_sq =~ /\$\(/ || $no_sq =~ /`/) { print '{"decision":"allow"}'; exit 0; }

# 2) Strip both single- and double-quoted segments (and backslash escapes) for operator safety check
my $unquoted = $trimmed;
$unquoted =~ s/'[^']*'/__SQ__/g;
$unquoted =~ s/"(?:\\.|[^"\\])*"/__DQ__/g;
$unquoted =~ s/\\./__ES__/g;

# Wrapped commands run in a child `bash -c`: shell-local (unexported) vars/functions would vanish.
# Skip if any expanded $VAR is neither exported (in our env) nor assigned within the command.
{
    my %assigned = map { $_ => 1 } ($no_sq =~ /(?:^|[\s;&])([A-Za-z_]\w*)=/g, $no_sq =~ /\bfor\s+([A-Za-z_]\w*)\s/g, $no_sq =~ /\bread\s+(?:-\w+\s+)*([A-Za-z_]\w*)/g);
    for my $v ($no_sq =~ /\$\{?([A-Za-z_]\w*)/g) {
        next if exists $ENV{$v} || $assigned{$v} || $v =~ /^(?:HOME|PATH|PWD|OLDPWD|USER|SHELL|RANDOM|SECONDS|LINENO|BASH\w*|UID|EUID|PPID|HOSTNAME|IFS)$/;
        print '{"decision":"allow"}';
        exit 0;
    }
}

# Temporarily erase harmless redirects (2>&1, stderr/stdin to /dev/null) and `|| true` / `|| :`
my $op_check = $unquoted;
$op_check =~ s/\b2>&1\b//g;
$op_check =~ s{(?:^|\s)(?:2>\s*/dev/null|<\s*/dev/null)(?=\s|$)}{ }g;
$op_check =~ s{\|\|\s*(?:true|:)(?=\s|$|;|&&)}{ }g;

if (
    $op_check =~ /<</                || # heredoc / herestring
    $op_check =~ /\|\|/              || # non-trivial || fallback
    $op_check =~ /[<>]\(/            || # process substitution
    $op_check =~ /[<>]/              || # any file/fd redirection (>, >>, &>, 2>, <)
    $op_check =~ /(?<!&)&(?!&)/      || # single & backgrounding
    $op_check =~ /&\s*$/                # trailing &
) {
    print '{"decision":"allow"}';
    exit 0;
}

# Safe read-only stdout filter pipeline check (per-segment so chains like `cd x && find . | grep y` work)
my $has_pipe = ($op_check =~ /\|/) ? 1 : 0;
if ($has_pipe) {
    for my $chain_seg (split(/(?:&&|;|\n)/, $op_check)) {
        next unless $chain_seg =~ /\|/;
        my @p_parts = split(/\|/, $chain_seg);
        if (@p_parts < 2 || @p_parts > 4) { print '{"decision":"allow"}'; exit 0; }
        for my $pi (1 .. $#p_parts) {
            my $stage = $p_parts[$pi];
            $stage =~ s/^\s+|\s+$//g;
            my ($fbin) = split(/\s+/, $stage);
            $fbin = '' unless defined $fbin;
            $fbin =~ s{^.*/}{};
            # Strictly allow only pure read-only stdout filters (never tee, xargs, head, tail, wc, sh, python)
            if ($fbin !~ /^(?:grep|egrep|fgrep|rg|sort|uniq|cut|tr|awk|sed|cat|nl|jq)$/) {
                print '{"decision":"allow"}';
                exit 0;
            }
        }
    }
}

# Quote-aware split on &&, ';' and unquoted newlines. Returns segments; separators kept in @SEPS.
our @SEPS;
sub split_and_and {
    my ($s) = @_;
    my @segs;
    @SEPS = ();
    my $cur = '';
    my ($in_sq, $in_dq, $esc) = (0, 0, 0);
    my $len = length($s);
    for (my $i = 0; $i < $len; $i++) {
        my $c = substr($s, $i, 1);
        if ($esc) { $cur .= $c; $esc = 0; next; }
        if ($in_sq) { $cur .= $c; $in_sq = 0 if $c eq "'"; next; }
        if ($c eq '\\') { $cur .= $c; $esc = 1; next; }
        if ($in_dq) { $cur .= $c; $in_dq = 0 if $c eq '"'; next; }
        if ($c eq "'") { $cur .= $c; $in_sq = 1; next; }
        if ($c eq '"') { $cur .= $c; $in_dq = 1; next; }
        my $sep = ($c eq '&' && $i + 1 < $len && substr($s, $i + 1, 1) eq '&') ? ' && '
                : $c eq ';' ? '; ' : $c eq "\n" ? "\n" : undef;
        if (defined $sep) {
            $cur =~ s/^\s+|\s+$//g;
            if ($cur ne '') { push @segs, $cur; push @SEPS, $sep; }
            elsif ($sep ne "\n" && @SEPS) { $SEPS[-1] = $sep; }
            $cur = '';
            $i++ if $sep eq ' && ';
            next;
        }
        $cur .= $c;
    }
    $cur =~ s/^\s+|\s+$//g;
    push @segs, $cur if $cur ne '';
    return @segs;
}

# Quote-aware shell tokenizer (returns array of [raw_tok, unquoted_tok])
sub shell_tokenize {
    my ($s) = @_;
    my @toks;
    my ($raw_t, $val_t) = ('', '');
    my ($in_sq, $in_dq, $esc, $in_tok) = (0, 0, 0, 0);
    my $len = length($s);
    for (my $i = 0; $i < $len; $i++) {
        my $c = substr($s, $i, 1);
        if ($esc) { $raw_t .= "\\$c"; $val_t .= $c; $esc = 0; $in_tok = 1; next; }
        if ($in_sq) {
            $raw_t .= $c;
            if ($c eq "'") { $in_sq = 0; } else { $val_t .= $c; }
            next;
        }
        if ($c eq '\\') { $esc = 1; $in_tok = 1; next; }
        if ($in_dq) {
            $raw_t .= $c;
            if ($c eq '"') { $in_dq = 0; } else { $val_t .= $c; }
            next;
        }
        if ($c eq "'") { $raw_t .= $c; $in_sq = 1; $in_tok = 1; next; }
        if ($c eq '"') { $raw_t .= $c; $in_dq = 1; $in_tok = 1; next; }
        if ($c =~ /\s/) {
            if ($in_tok) {
                push @toks, [$raw_t, $val_t];
                ($raw_t, $val_t, $in_tok) = ('', '', 0);
            }
            next;
        }
        $raw_t .= $c;
        $val_t .= $c;
        $in_tok = 1;
    }
    push @toks, [$raw_t, $val_t] if $in_tok;
    return @toks;
}

sub base_name {
    my ($p) = @_;
    $p =~ s{^.*/}{};
    return $p;
}

my @EXCLUDES = qw(.git node_modules dist build .next .nuxt target __pycache__ .venv venv .tox .cache coverage .turbo);

# Classify a single simple segment; returns ($cat, $mutated_seg_or_undef, $deny_reason_or_undef)
sub classify_segment {
    my ($seg) = @_;
    my @raw_toks = shell_tokenize($seg);
    return ('', undef, undef) unless @raw_toks;

    # Strip harmless redirect tokens (2>&1, 2>/dev/null, < /dev/null) for inspection only
    my @toks;
    for (my $q = 0; $q < @raw_toks; $q++) {
        my $v = $raw_toks[$q][1];
        next if $v eq '2>&1' || $v eq '2>/dev/null' || $v eq '</dev/null';
        if (($v eq '<' || $v eq '2>') && $q + 1 < @raw_toks && $raw_toks[$q + 1][1] eq '/dev/null') { $q++; next; }
        push @toks, $raw_toks[$q];
    }
    return ('', undef, undef) unless @toks;

    # Check if silent setup command
    my $first_bin = base_name($toks[0][1]);
    if ($first_bin =~ /^(?:cd|pushd|popd|export|unset|source|\.|mkdir|rm|mv|cp|touch|chmod|true|nvm|pyenv|fnm|set|ulimit|umask)$/) {
        return ('_setup', undef, undef);
    }

    # Bounded unwrapper loop (env, sudo, timeout, npx, uv run, poetry run, pnpm exec, python -m, ...)
    my $idx = 0;
    my $wrapped_inner = 0;
    for (my $iter = 0; $iter < 5 && $idx < @toks; $iter++) {
        # Leading KEY=VAL env assignments
        while ($idx < @toks && $toks[$idx][1] =~ /^[A-Za-z_][A-Za-z0-9_]*=/) { $idx++; }
        last if $idx >= @toks;
        my $b = base_name($toks[$idx][1]);
        if ($b eq 'env') {
            $idx++;
            while ($idx < @toks && ($toks[$idx][1] =~ /^-/ || $toks[$idx][1] =~ /^[A-Za-z_][A-Za-z0-9_]*=/)) {
                $idx++ if $toks[$idx][1] =~ /^-(?:u|C|S)$/;
                $idx++;
            }
            next;
        }
        if ($b eq 'sudo' || $b eq 'doas') {
            $idx++;
            while ($idx < @toks && $toks[$idx][1] =~ /^-/) {
                $idx++ if $toks[$idx][1] =~ /^-(?:u|g|C|h|p|r|t|U)$/;
                $idx++;
            }
            next;
        }
        if ($b eq 'timeout') {
            $idx++;
            while ($idx < @toks && $toks[$idx][1] =~ /^-/) {
                $idx++ if $toks[$idx][1] =~ /^-(?:k|s|--kill-after|--signal)$/;
                $idx++;
            }
            $idx++; # duration arg
            next;
        }
        if ($b eq 'time' || $b eq 'nice' || $b eq 'ionice' || $b eq 'stdbuf') {
            $idx++;
            while ($idx < @toks && $toks[$idx][1] =~ /^-/) {
                $idx++ if $toks[$idx][1] =~ /^-(?:n|c|i|o|e)$/;
                $idx++;
            }
            next;
        }
        if ($b eq 'npx' || $b eq 'bunx') {
            $idx++;
            while ($idx < @toks && $toks[$idx][1] =~ /^-/) {
                $idx++ if $toks[$idx][1] =~ /^-(?:p|--package|c|--call)$/;
                $idx++;
            }
            next;
        }
        if (($b eq 'pnpm' || $b eq 'yarn' || $b eq 'npm') && $idx + 1 < @toks && $toks[$idx + 1][1] =~ /^(?:exec|dlx)$/) {
            $idx += 2;
            while ($idx < @toks && $toks[$idx][1] =~ /^-/) { $idx++; }
            next;
        }
        if ($b =~ /^(?:uv(?:\.exe)?|poetry|pipenv|pdm|hatch)$/i && $idx + 1 < @toks && $toks[$idx + 1][1] eq 'run') {
            $idx += 2;
            while ($idx < @toks && $toks[$idx][1] =~ /^-/) {
                $idx++ if $toks[$idx][1] =~ /^--(?:with|extra|group|python|directory|project|env-file)$/;
                $idx++;
            }
            next;
        }
        if ($b =~ /^cmd(?:\.exe)?$/i && $idx + 1 < @toks && $toks[$idx + 1][1] =~ m{^/c$}i) {
            # WSL -> Windows: classify the inner command string
            my @inner = shell_tokenize(join(' ', map { $_->[1] } @toks[$idx + 2 .. $#toks]));
            return ('', undef, undef) unless @inner;
            @toks = @inner;
            $idx = 0;
            $iter = 0;
            $wrapped_inner = 1;
            next;
        }
        if ($b =~ /^(?:pnpm|yarn|bun)$/ && $idx + 1 < @toks && $toks[$idx + 1][1] =~ /^(?:vitest|jest|mocha|playwright|tsc|eslint|biome|oxlint|vite|next|webpack|tsup|esbuild|rollup|remotion)$/) {
            $idx++;
            next;
        }
        if ($b eq 'bundle' && $idx + 1 < @toks && $toks[$idx + 1][1] eq 'exec') {
            $idx += 2;
            next;
        }
        if ($b =~ /^(?:python3?(?:\.\d+)?|php)$/ && $idx + 1 < @toks && base_name($toks[$idx + 1][1]) =~ /^(?:manage\.py|artisan)$/) {
            $idx++;
            next;
        }
        if ($b =~ /^python3?(?:\.\d+)?$/) {
            my $j = $idx + 1;
            while ($j < @toks && $toks[$j][1] =~ /^-[a-zA-Z]+$/ && $toks[$j][1] ne '-m') { $j++; }
            if ($j + 1 < @toks && $toks[$j][1] eq '-m') {
                $idx = $j + 1;
                next;
            }
        }
        last;
    }

    return ('', undef, undef) if $idx >= @toks;
    my @t = map { $_->[1] } @toks[$idx .. $#toks];
    my $bin = base_name($t[0]);
    my @args = @t[1 .. $#t];
    my $arg_str = join(' ', @args);

    # Global help/version/interactive/structured-output skips on FINAL command args only
    for my $a (@args) {
        next if $bin eq 'ffmpeg' && $a eq '-i';
        return ('', undef, undef) if $a =~ /^(?:--help|--version|-V|--json|--porcelain|--name-only|--name-status|--stat|--shortstat|--numstat|--short|-q|--quiet|--silent|-i|-it|--interactive|--patch|--watch|-w|--vimgrep)$/;
        return ('', undef, undef) if $a =~ /^(?:--format|--pretty|--output-format|--jq|--template)(?:=|$)/;
    }
    # Standalone -h (help), never for human-readable flag tools (ls, df, du, grep, rg, rsync, ps)
    if ($bin !~ /^(?:ls|df|du|free|grep|egrep|fgrep|rg|rsync|ps|sort)$/ && @args && $args[-1] eq '-h') {
        return ('', undef, undef);
    }
    # -o json / -o=json
    for (my $i = 0; $i < @args; $i++) {
        return ('', undef, undef) if $args[$i] eq '-o=json' || ($args[$i] eq '-o' && $i + 1 < @args && $args[$i + 1] eq 'json');
    }

    # Resolve subcommand skipping global tool flags
    my ($sub, $sub2) = ('', '');
    if ($bin eq 'git') {
        my $i = 0;
        while ($i < @args && $args[$i] =~ /^-/) {
            my $f = $args[$i++];
            $i++ if $f =~ /^(?:-C|-c|--git-dir|--work-tree)$/;
        }
        $sub  = $args[$i]     // '';
        $sub2 = $args[$i + 1] // '';
    } elsif ($bin =~ /^(?:npm|pnpm|yarn|cargo|docker|podman|kubectl|helm|gh|terraform|tofu)$/) {
        my $i = 0;
        while ($i < @args && $args[$i] =~ /^-/) {
            my $f = $args[$i++];
            $i++ if $f =~ /^(?:--prefix|--dir|--cwd|--manifest-path|-n|--namespace|--context|-f|--file)$/;
        }
        $sub  = $args[$i]     // '';
        $sub2 = $args[$i + 1] // '';
    } else {
        $sub  = $args[0] // '';
        $sub2 = $args[1] // '';
    }

    # Silent-on-success steps that may appear inside build/test/diagnostic chains
    return ('_setup', undef, undef) if $bin =~ /^(?:export|set|unset|mkdir|rm|cp|mv|chmod|touch|sleep|true|false|:)$/ ||
        ($bin eq 'cargo' && $sub eq 'fmt') || ($bin eq 'go' && $sub eq 'fmt') || $bin =~ /^(?:gofmt|rustfmt)$/ ||
        ($bin eq 'git' && $sub =~ /^(?:add|commit|checkout|switch|stash|fetch|push|pull|tag|mv|rm|restore|reset)$/);

    # Long-running servers / watchers: output never "finishes" -> never buffer behind the wrapper
    if ($bin =~ /^(?:make|mvn|gradle|gradlew|webpack|esbuild|vite|next|remotion|ng|nx|turbo)$/) {
        for my $a (@args) {
            return ('', undef, undef) if $a =~ /^(?:serve|dev|start|watch|run|up|preview|studio|bootRun|spring-boot:run|--serve|--watch)$/;
        }
    }

    # --- 1. SSH remote commands (42.9% of real commands!) ---
    if ($bin eq 'ssh') {
        # Skip interactive login (no remote command) or explicit TTY allocation (-t/-tt)
        my $i = 0;
        my $dest_seen = 0;
        my @remote;
        while ($i < @args) {
            my $a = $args[$i++];
            if (!$dest_seen) {
                if ($a =~ /^-/) {
                    return ('', undef, undef) if $a =~ /^-[a-zA-Z]*[tTNf]/; # interactive tty/background
                    $i++ if $a =~ /^-[bcDEeFIiJLlmOopQRSWw]$/;
                    next;
                }
                $dest_seen = 1;
                next;
            }
            push @remote, $a;
        }
        return ('', undef, undef) unless @remote; # interactive shell
        my $rem = join(' ', @remote);
        # Never wrap streaming follow commands inside ssh
        return ('', undef, undef) if $rem =~ /\b(?:tail|journalctl|docker\s+(?:compose\s+)?logs|kubectl\s+logs|dmesg|less)\b[^\n;|&]*(?:\s-[a-zA-Z]*[fF]\b|--follow\b)/;
        return ('', undef, undef) if $rem =~ /\b(?:watch|top|htop)\b(?![^\n;|&]*\s-b)|\bpm2\s+(?:logs|monit)\b(?![^\n;|&]*--nostream)/;
        # Short read-only probes (<=2 cmds, <120 chars): pass through to avoid 23-tok wrap notice.
        # Multi-command SSH diagnostic dumps (>=3 cmds or >=120 chars) wrap via --cat ssh (9.9KB lossless guard).
        my $all_read = 1;
        my $n_rparts = 0;
        for my $p (split(/\s*(?:&&|\|\||[;|\n])\s*/, $rem)) {
            next if $p !~ /\S/;
            $n_rparts++;
            $p =~ s/^\s*(?:(?:sudo|env|nice|timeout\s+\d+\S*)\s+(?:-\S+\s+)*)*(?:[A-Za-z_]\w*=\S*\s+)*//;
            my ($w) = $p =~ /^(\S+)/;
            $w = base_name($w // '');
            next if $w =~ /^(?:cat|head|nl|wc|stat|file|echo|printf|whoami|id|hostname|uname|date|pwd|which|type|command|readlink|realpath|test|\[|true|false|cd|export|uptime|free|env|printenv|cut|sort|uniq|tr|jq|md5sum|sha\d*sum|basename|dirname|sleep|lsb_release|nproc|groups|getent)$/;
            next if $w eq 'tail' && $p !~ /\s-[a-zA-Z]*[fF]\b/;
            next if $w eq 'sed' && $p =~ /\s-n\b/ && $p !~ /\s-i/;
            next if $w =~ /^(?:grep|egrep|fgrep)$/ && $p !~ /\s-[a-zA-Z]*[rR]/;
            next if $w eq 'ls' && $p !~ /\s-[a-zA-Z]*R/;
            next if $w eq 'awk' && $p !~ /\bsystem\s*\(/;
            next if $w eq 'git' && $p =~ /^git\s+(?:status|rev-parse|branch|remote|config|show\s+\S+:\S+)\b/;
            next if $w eq 'systemctl' && $p =~ /\bis-(?:active|enabled|failed)\b/;
            $all_read = 0;
            last;
        }
        return ('', undef, undef) if $all_read && $n_rparts <= 2 && length($rem) < 120;
        return ('ssh', undef, undef);
    }

    # --- 2. Test runners ---
    if (
        $bin =~ /^(?:pytest|jest|vitest|mocha|ava|playwright|cypress|bats|ctest|rspec|phpunit|pest|unittest)$/ ||
        ($bin eq 'cargo'  && ($sub eq 'test' || $sub eq 'nextest')) ||
        ($bin eq 'go'     && $sub eq 'test') ||
        ($bin eq 'dotnet' && $sub eq 'test') ||
        ($bin eq 'deno'   && $sub eq 'test') ||
        ($bin eq 'mix'    && $sub eq 'test') ||
        ($bin eq 'flutter' && $sub eq 'test') ||
        ($bin eq 'node'    && $sub eq '--test') ||
        $bin =~ /^(?:tox|nox)$/ ||
        ($bin eq 'manage.py' && $sub eq 'test') ||
        ($bin eq 'artisan' && $sub eq 'test') ||
        ($bin =~ /^(?:npm|pnpm|yarn|bun)$/ && ($sub eq 'test' || ($sub eq 'run' && $sub2 =~ /^test(?::|$)/))) ||
        ($bin =~ /^(?:mvn|gradle|gradlew)$/ && $arg_str =~ /\btest\b/)
    ) {
        return ('test', undef, undef);
    }

    # --- 3. Package managers ---
    if (
        ($bin =~ /^(?:npm|pnpm|yarn|bun)$/ && $sub =~ /^(?:install|i|ci|add|update|upgrade|audit)$/) ||
        ($bin =~ /^(?:pip|pip3)$/          && $sub =~ /^(?:install|download|wheel)$/) ||
        ($bin eq 'uv'                      && $sub =~ /^(?:pip|sync|add|lock)$/) ||
        ($bin eq 'poetry'                  && $sub =~ /^(?:install|add|update)$/) ||
        ($bin eq 'cargo'                   && $sub =~ /^(?:fetch|add|update|install)$/) ||
        ($bin =~ /^(?:apt|apt-get)$/       && $sub =~ /^(?:install|update|upgrade|dist-upgrade)$/) ||
        ($bin eq 'brew'                    && $sub =~ /^(?:install|upgrade|reinstall)$/) ||
        ($bin eq 'composer'                && $sub =~ /^(?:install|update|require)$/) ||
        ($bin eq 'dotnet'                  && $sub eq 'restore') ||
        ($bin eq 'go'                      && ($sub eq 'get' || ($sub eq 'mod' && $sub2 =~ /^(?:download|tidy)$/))) ||
        ($bin =~ /^(?:conda|mamba|micromamba)$/ && $sub =~ /^(?:install|create|update|env)$/)
    ) {
        return ('pkg', undef, undef);
    }

    # --- 4. Build systems & compilers ---
    if (
        (($bin eq 'docker' || $bin eq 'podman') && ($sub eq 'build' || ($sub eq 'compose' && $sub2 eq 'build'))) ||
        $bin =~ /^(?:make|cmake|ninja|mvn|gradle|gradlew|webpack|esbuild|rollup|tsup|tsc)$/ ||
        ($bin eq 'vite'   && $sub eq 'build') ||
        ($bin eq 'next'   && $sub eq 'build') ||
        ($bin eq 'cargo'  && $sub =~ /^(?:build|check)$/) ||
        ($bin eq 'go'     && $sub =~ /^(?:build|vet)$/) ||
        ($bin eq 'dotnet' && $sub =~ /^(?:build|publish)$/) ||
        ($bin =~ /^(?:swift|flutter|zig)$/ && $sub eq 'build') ||
        ($bin eq 'remotion' && $sub =~ /^(?:bundle|render|still)$/) ||
        ($bin eq 'ffmpeg') ||
        ($bin =~ /^(?:npm|pnpm|yarn|bun)$/ && ($sub eq 'build' || ($sub eq 'run' && $sub2 =~ /^(?:build|typecheck|check)(?::|$)/)))
    ) {
        return ('build', undef, undef);
    }

    # --- 5. Linters & typecheckers ---
    if (
        $bin =~ /^(?:eslint|oxlint|biome|ruff|flake8|mypy|pyright|basedpyright|pylint|golangci-lint|rubocop|phpstan|shellcheck|hadolint)$/ ||
        ($bin eq 'cargo' && $sub eq 'clippy') ||
        ($bin =~ /^(?:npm|pnpm|yarn|bun)$/ && ($sub eq 'lint' || ($sub eq 'run' && $sub2 =~ /^lint(?::|$)/)))
    ) {
        return ('lint', undef, undef);
    }

    # --- 6. Git (high-ROI subcommands only, respecting break-even!) ---
    if ($bin eq 'git') {
        return ('', undef, undef) if $sub eq 'show' && grep { /^[^-][^:]*:/ } @args;
        if ($sub eq 'diff' || $sub eq 'show') {
            return ('git_diff', undef, undef);
        }
        if ($sub eq 'log') {
            # P0: never drop diff hunks on -p; skip already-compact short logs
            return ('', undef, undef) if $arg_str =~ /(?:^|\s)(?:-p|-s|--oneline|--graph|-[1-9]\b|-n\s*(?:[1-9]|1[0-5])\b)/;
            return ('git_log', undef, undef);
        }
        if ($sub eq 'clone') {
            return ('git_net', undef, undef);
        }
        if ($sub eq 'ls-files') {
            return ('find', undef, undef);
        }
        return ('', undef, undef);
    }

    # --- 7. Search (grep/rg) with auto --exclude-dir + -I injection ---
    if ($bin =~ /^(?:grep|egrep|fgrep|rg|ag|ack)$/) {
        return ('', undef, undef) if $arg_str =~ /(?:^|\s)(?:-m\s*[1-5]\b|--max-count=[1-5]\b)/;
        if ($bin =~ /^(?:rg|ag|ack)$/) {
            my $found = 0;
            # Skip WSL /mnt/<drive> PATH entries: 9p stat() is ~3ms each (and they hold .exe builds)
            for my $d (split(/:/, $ENV{PATH} // '')) { next if $d eq '' || $d =~ m{^/mnt/}; if (-x "$d/$bin") { $found = 1; last; } }
            return ('', undef, undef) unless $found; # "command not found" is tiny
        }
        return ('find', undef, undef) if $arg_str =~ /(?:^|\s)(?:-[a-zA-Z]*[lLc][a-zA-Z]*|--files(?:-with(?:out)?-matches?)?|--count)(?:\s|$)/ && $arg_str !~ /(?:^|\s)-[a-zA-Z]*[ABC]/;
        if ($bin =~ /^(?:grep|egrep|fgrep)$/) {
            my $is_rec = ($arg_str =~ /(?:^|\s)(?:--recursive|-[a-zA-Z]*[rR][a-zA-Z]*)(?:\s|$)/);
            if ($is_rec && $arg_str !~ /--exclude-dir/) {
                # Build safe exclude list (never exclude a dir mentioned in command args)
                my @inj;
                push @inj, '-I' unless $arg_str =~ /(?:^|\s)(?:-a|--text|--binary-files|-I)(?:\s|$)/;
                for my $d (@EXCLUDES) {
                    next if index($seg, $d) >= 0;
                    push @inj, "--exclude-dir=$d";
                }
                if (@inj && $idx == 0 && !$wrapped_inner) {
                    my $inj_str = join(' ', @inj);
                    my $mut = $seg;
                    $mut =~ s/^(\S+)/$1 $inj_str/;
                    return ('search', $mut, undef);
                }
            }
            # Non-recursive grep on single small file <= 40KB: skip wrapping (0ms/0 notice)
            if (!$is_rec) {
                my @files = grep { $_ !~ /^-/ } @args;
                if (@files == 2) { # pattern + 1 file
                    my $fp = res_path($files[1]);
                    if (-f $fp && (stat($fp))[7] <= 40960) {
                        return ('', undef, undef);
                    }
                }
            }
        }
        return ('search', undef, undef);
    }

    # --- 8. find / tree with auto-prune injection ---
    if ($bin eq 'find') {
        return ('', undef, undef) if $arg_str =~ /(?:^|\s)(?:-exec|-execdir|-ok|-okdir|-delete)\b/;
        return ('', undef, undef) if $arg_str =~ /(?:^|\s)-maxdepth\s+[0-2]\b/;
        # Safe auto-prune when no -prune/-depth/-D and simple idx==0
        if ($idx == 0 && !$wrapped_inner && $arg_str !~ /(?:^|\s)(?:-prune|-depth|-D)\b/) {
            my @keep_ex = grep { index($seg, $_) < 0 } @EXCLUDES;
            if (@keep_ex) {
                # Split tokens into: pre-flags, paths, leading global opts, expression
                my $k = 1;
                $k++ while $k < @toks && $toks[$k][1] =~ /^-[HLP]$/;
                $k++ while $k < @toks && $toks[$k][1] !~ /^[-!(]/;
                while ($k < @toks && $toks[$k][1] =~ /^-(?:maxdepth|mindepth|xdev|mount|follow|regextype)$/) {
                    my $g = $toks[$k++][1];
                    $k++ if $g =~ /^-(?:maxdepth|mindepth|regextype)$/ && $k < @toks;
                }
                my $prefix = join(' ', map { $_->[0] } @toks[0 .. $k - 1]);
                my @expr_toks = @toks[$k .. $#toks];
                my $expr_raw = join(' ', map { $_->[0] } @expr_toks);
                my $expr_val = join(' ', map { $_->[1] } @expr_toks);
                my $prune_names = join(' -o ', map { "-name '$_'" } @keep_ex);
                my $prune_clause = "\\( -type d \\( $prune_names \\) -prune \\) -o";
                my $has_action = ($expr_val =~ /(?:^|\s)-(?:print|print0|ls|printf|quit|fprint\w*|fls)\b/);
                my $mut;
                if ($expr_raw eq '') {
                    $mut = "$prefix $prune_clause -print";
                } elsif ($has_action) {
                    $mut = "$prefix $prune_clause \\( $expr_raw \\)";
                } else {
                    $mut = "$prefix $prune_clause \\( $expr_raw \\) -print";
                }
                return ('find', $mut, undef);
            }
        }
        return ('find', undef, undef);
    }
    if ($bin eq 'tree') {
        return ('', undef, undef) if $arg_str =~ /(?:^|\s)-L\s+[0-2]\b/;
        if ($idx == 0 && !$wrapped_inner && $arg_str !~ /(?:^|\s)-I\b/) {
            my $pat = join('|', grep { index($seg, $_) < 0 } @EXCLUDES);
            if ($pat ne '') {
                my $mut = $seg;
                $mut =~ s/^(\S+)/$1 -I '$pat'/;
                return ('find', $mut, undef);
            }
        }
        return ('find', undef, undef);
    }

    # --- 9. cat large-file guard (0.05ms Perl stat; <=40KB passes untouched!) ---
    if ($bin eq 'cat') {
        my @files = grep { $_ !~ /^-/ } @args;
        return ('', undef, undef) unless @files;
        my $tot = 0;
        my $suspect = 0;
        for my $f (@files) {
            my $fp = res_path($f);
            next unless -f $fp;
            my $sz = (stat($fp))[7];
            $tot += $sz;
            my $bn = base_name($fp);
            if ($sz > 8192 && $bn =~ /^(?:package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|poetry\.lock|go\.sum|uv\.lock)$|\.(?:min\.js|min\.css|map|ipynb|har|snap)$/i) {
                $suspect = 1;
            }
            if ($sz > 4096 && open(my $fh, '<:raw', $fp)) {
                my $buf = '';
                my $n = sysread($fh, $buf, 4096);
                close $fh;
                if (defined $n && $n > 0 && index($buf, "\0") >= 0) {
                    return ('', undef, "[token-saver] Blocked cat of binary file '$bn' (" . int($sz/1024) . "KB). Use file/strings/python3 instead.");
                }
                if ($sz > 15360 && defined $n && $n > 0) {
                    for my $l (split(/\n/, $buf, -1)) {
                        $suspect = 1 if length($l) > 2000;
                    }
                }
            }
        }
        return ('cat_large', undef, undef) if $suspect || $tot > 40960;
        return ('', undef, undef);
    }

    # --- 10. Containers / K8s / Logs (STRICTLY skip -f / --follow!) ---
    if ($bin eq 'docker' || $bin eq 'podman') {
        if ($sub eq 'logs' || ($sub eq 'compose' && $sub2 eq 'logs')) {
            return ('', undef, undef) if $arg_str =~ /(?:^|\s)(?:-[a-zA-Z]*[fF][a-zA-Z]*|--follow(?:=\S*)?)(?:\s|$)/;
            return ('logs', undef, undef);
        }
        return ('table', undef, undef) if $sub =~ /^(?:ps|images)$/ || ($sub eq 'compose' && $sub2 eq 'ps');
        return ('', undef, undef);
    }
    if ($bin eq 'pm2') {
        return ('table', undef, undef) if $sub =~ /^(?:list|ls|l|status|describe|show|info)$/;
        return ('', undef, undef);
    }
    if ($bin eq 'ss' || $bin eq 'netstat') {
        return ('table', undef, undef) if $arg_str =~ /(?:^|\s)-[a-zA-Z]*[at]/;
        return ('', undef, undef);
    }
    if ($bin eq 'kubectl') {
        if ($sub eq 'logs') {
            return ('', undef, undef) if $arg_str =~ /(?:^|\s)(?:-[a-zA-Z]*[fF][a-zA-Z]*|--follow(?:=\S*)?)(?:\s|$)/;
            return ('logs', undef, undef);
        }
        # -o yaml/json/jsonpath/name/custom-columns => structured, needed verbatim (wide = table, ok)
        return ('', undef, undef) if $arg_str =~ /(?:^|\s)(?:-o\s*=?\s*|--output[=\s]+)(?!wide\b)\S/;
        return ('table', undef, undef) if $sub eq 'get' && $arg_str =~ /(?:^|\s)(?:-A|--all-namespaces)(?:\s|$)/;
        return ('logs', undef, undef) if $sub eq 'describe' || $sub eq 'events';
        return ('', undef, undef);
    }
    if ($bin eq 'journalctl' || $bin eq 'dmesg') {
        return ('', undef, undef) if $arg_str =~ /(?:^|\s)(?:-[a-zA-Z]*[fFw][a-zA-Z]*|--follow(?:=\S*)?)(?:\s|$)/;
        return ('logs', undef, undef);
    }
    if ($bin eq 'systemctl') {
        return ('logs', undef, undef) if $sub eq 'status';
        return ('table', undef, undef) if $sub =~ /^(?:list-units|list-unit-files|list-timers|list-sockets)$/;
        return ('', undef, undef);
    }
    if ($bin eq 'tmux') {
        return ('logs', undef, undef) if $sub eq 'capture-pane';
        return ('table', undef, undef) if $sub =~ /^(?:list-sessions|ls|list-windows|list-panes)$/;
        return ('', undef, undef);
    }
    if ($bin eq 'strings') {
        return ('search', undef, undef);
    }

    # --- 11. HTTP (curl/wget) ---
    if ($bin eq 'curl') {
        return ('', undef, undef) if $arg_str =~ /(?:^|\s)(?:-I|--head|-w|--write-out)(?:\s|$)/;
        if ($arg_str =~ /(?:^|\s)(?:-O|-o\s+(?!-)\S+)/ && $arg_str !~ /(?:^|\s)(?:-v|--verbose)(?:\s|$)/) {
            return ('', undef, undef);
        }
        return ('http', undef, undef);
    }
    if ($bin eq 'wget') {
        return ('http', undef, undef) if $arg_str =~ /(?:^|\s)(?:-O\s*-|--output-document=-)(?:\s|$)/ || $arg_str !~ /(?:^|\s)(?:-q|--quiet)(?:\s|$)/;
        return ('', undef, undef);
    }

    # --- 12. Archives / Sync ---
    if ($bin eq 'tar') {
        return ('archive', undef, undef) if $arg_str =~ /(?:^|\s)(?:--verbose|--list|-[a-zA-Z]*[vt][a-zA-Z]*|[a-zA-Z]*[vt]f)\b/;
        return ('', undef, undef);
    }
    if ($bin eq 'unzip' || $bin eq 'zip') {
        return ('', undef, undef) if $bin eq 'unzip' && $arg_str =~ /(?:^|\s)-p(?:\s|$)/;
        return ('archive', undef, undef);
    }
    if ($bin eq 'rsync') {
        return ('archive', undef, undef) if $arg_str =~ /(?:^|\s)(?:--verbose|--progress|--info=progress|-[a-zA-Z]*v[a-zA-Z]*)(?:\s|$)/;
        return ('', undef, undef);
    }

    # --- 13. DevOps / Cloud / System ---
    if ($bin eq 'gh') {
        return ('git_diff', undef, undef) if $sub eq 'pr' && $sub2 eq 'diff';
        return ('logs', undef, undef) if $sub eq 'run' && $sub2 eq 'view';
        return ('table', undef, undef) if $sub =~ /^(?:pr|issue|run|workflow)$/ && $sub2 =~ /^(?:list|view|checks)$/;
        return ('', undef, undef);
    }
    if ($bin eq 'terraform' || $bin eq 'tofu') {
        return ('', undef, undef) if $arg_str =~ /(?:^|\s)(?:-json|-raw)(?:\s|$)/;
        return ('iac', undef, undef) if $sub =~ /^(?:plan|apply|destroy|init|refresh|validate|show|state)$/;
        return ('', undef, undef);
    }
    if ($bin eq 'helm') {
        return ('k8s_yaml', undef, undef) if $sub eq 'template';
        return ('table', undef, undef) if $sub =~ /^(?:list|ls|history|status|install|upgrade)$/;
        return ('', undef, undef);
    }
    if ($bin eq 'ansible-playbook') {
        return ('iac', undef, undef);
    }
    if ($bin eq 'aws' || $bin eq 'gcloud' || $bin eq 'az') {
        return ('', undef, undef) if $arg_str =~ /\blogs\s+tail\b/;
        if ($bin ne 'gcloud') {
            # aws/az print JSON by default: never dedupe/pad it
            return ('cloud', undef, undef) if $arg_str =~ /(?:^|\s)(?:--output[=\s]+|-o\s+)(?:table|text|tsv)\b/;
            return ('cloud', undef, undef) if $bin eq 'aws' && $arg_str =~ /^(?:s3\s+(?:cp|sync|mv|rm)|cloudformation\s+(?:deploy|package))\b/;
            return ('', undef, undef);
        }
        return ('cloud', undef, undef);
    }
    if ($bin eq 'ps') {
        return ('ps', undef, undef) if $arg_str =~ /(?:^|\s)(?:aux|-ef|-eF|-el|-e|-A|ax|-eo|-o)(?:\s|$|=)/;
        return ('', undef, undef);
    }
    if ($bin eq 'ls') {
        return ('find', undef, undef) if $arg_str =~ /(?:^|\s)-[a-zA-Z]*R[a-zA-Z]*\b/;
        if ($arg_str =~ /(?:^|\s)-[a-zA-Z]*l[a-zA-Z]*\b/) {
            my @dirs = grep { $_ !~ /^-/ } @args;
            @dirs = ('.') unless @dirs;
            my $total = 0;
            my $has_glob = 0;
            for my $d (@dirs) {
                if ($d =~ /[*?\[]/) { $has_glob = 1; last; }
                my $rd = res_path($d);
                if (-d $rd && opendir(my $dh, $rd)) {
                    $total++ while defined(readdir($dh)) && $total <= 45;
                    closedir($dh);
                }
                last if $total > 37;
            }
            return ('table', undef, undef) if $total > 37 || ($has_glob && @dirs >= 2);
        }
        return ('', undef, undef);
    }
    if ($bin =~ /^python3?(?:\.\d+)?$/) {
        # Wrap noisy test/build/audit/bench Python script runs (small outputs pass through verbatim)
        my @nonflags = grep { $_ !~ /^-/ } @args;
        if (@nonflags && $nonflags[0] =~ /(?:^|\/)(?:test_[^\/]*|[^\/]*_test|run_tests|setup|bench[^\/]*|audit[^\/]*|build[^\/]*|migrate[^\/]*)\.py$/i) {
            return ('logs', undef, undef);
        }
        # Wrap non-trivial read-only `python3 -c '...'` inspection scripts (>=80B, no file writes)
        if ($arg_str =~ /(?:^|\s)-c\s+/ && length($arg_str) >= 80 &&
            $arg_str !~ /\b(?:write_text|write_bytes|unlink|rmtree|shutil\.|os\.remove)\b/ &&
            $arg_str !~ /open\([^)]*['"][wa+]/i) {
            return ('logs', undef, undef);
        }
        return ('', undef, undef);
    }
    if ($bin eq 'du') {
        return ('', undef, undef) if $arg_str =~ /(?:^|\s)(?:-[a-zA-Z]*s[a-zA-Z]*|--summarize|-d\s*0|--max-depth=0)(?:\s|$)/;
        return ('find', undef, undef);
    }
    if ($bin eq 'df') {
        my @paths = grep { $_ !~ /^-/ } @args;
        return ('table', undef, undef) unless @paths;
        return ('', undef, undef);
    }

    return ('', undef, undef);
}

# Strip trailing `|| true` / `|| :` (unquoted) for segment classification only (original cmd kept verbatim)
sub strip_harmless_or_true {
    my ($seg) = @_;
    my ($in_sq, $in_dq, $esc) = (0, 0, 0);
    my $len = length($seg);
    for (my $i = 0; $i < $len; $i++) {
        my $c = substr($seg, $i, 1);
        if ($esc) { $esc = 0; next; }
        if ($in_sq) { $in_sq = 0 if $c eq "'"; next; }
        if ($c eq '\\') { $esc = 1; next; }
        if ($in_dq) { $in_dq = 0 if $c eq '"'; next; }
        if ($c eq "'") { $in_sq = 1; next; }
        if ($c eq '"') { $in_dq = 1; next; }
        if ($c eq '|' && $i + 1 < $len && substr($seg, $i + 1, 1) eq '|') {
            my $rest = substr($seg, $i + 2);
            if ($rest =~ /^\s*(?:true|:)\s*$/) {
                my $head = substr($seg, 0, $i);
                $head =~ s/\s+$//;
                return $head;
            }
        }
    }
    return $seg;
}

# Extract upstream command before first unquoted `|` (for safe read-only filter pipelines)
sub extract_pipe_upstream {
    my ($seg) = @_;
    my ($in_sq, $in_dq, $esc) = (0, 0, 0);
    my $len = length($seg);
    for (my $i = 0; $i < $len; $i++) {
        my $c = substr($seg, $i, 1);
        if ($esc) { $esc = 0; next; }
        if ($in_sq) { $in_sq = 0 if $c eq "'"; next; }
        if ($c eq '\\') { $esc = 1; next; }
        if ($in_dq) { $in_dq = 0 if $c eq '"'; next; }
        if ($c eq "'") { $in_sq = 1; next; }
        if ($c eq '"') { $in_dq = 1; next; }
        if ($c eq '|' && ($i + 1 >= $len || substr($seg, $i + 1, 1) ne '|')) {
            my $head = substr($seg, 0, $i);
            $head =~ s/\s+$//;
            return $head;
        }
    }
    return $seg;
}

# Handle single commands and safe &&/;/newline chains (up to 16 segments for diagnostic scripts)
my @segs = split_and_and($trimmed);
if (!@segs || @segs > 16) { print '{"decision":"allow"}'; exit 0; }

# Rebuild only if a segment was mutated; otherwise run the model's exact original string
sub rebuild {
    my ($segs, $mutated) = @_;
    return $trimmed unless $mutated;
    my $out = '';
    for my $k (0 .. $#$segs) { $out .= $segs->[$k] . ($k < $#$segs ? ($SEPS[$k] // ' && ') : ''); }
    $out .= $SEPS[-1] if @SEPS >= @$segs && $SEPS[-1] =~ /;/;
    return $out;
}

my $final_cat = '';
my $final_cmd = $trimmed;

if (@segs == 1) {
    my $clean_seg = strip_harmless_or_true($segs[0]);
    my $seg_has_pipe = ($clean_seg ne extract_pipe_upstream($clean_seg)) ? 1 : 0;
    my $target_seg = $seg_has_pipe ? extract_pipe_upstream($clean_seg) : $clean_seg;
    my ($c, $mut, $deny) = classify_segment($target_seg);
    if (defined $deny) {
        print '{"decision":"deny","reason":"' . json_escape($deny) . '"}';
        exit 0;
    }
    if ($c eq '' || $c eq '_setup') {
        print '{"decision":"allow"}';
        exit 0;
    }
    $final_cat = ($seg_has_pipe && $c =~ /^(?:find|search)$/) ? 'search' : $c;
    $final_cmd = $mut if !$seg_has_pipe && defined $mut && $clean_seg eq $segs[0];
} else {
    my @cats;
    my @new_segs;
    my $mutated = 0;
    for my $s (@segs) {
        my $clean_s = strip_harmless_or_true($s);
        my $seg_has_pipe = ($clean_s ne extract_pipe_upstream($clean_s)) ? 1 : 0;
        my $target_s = $seg_has_pipe ? extract_pipe_upstream($clean_s) : $clean_s;
        my ($c, $mut, $deny) = classify_segment($target_s);
        if (defined $deny) {
            print '{"decision":"deny","reason":"' . json_escape($deny) . '"}';
            exit 0;
        }
        if ($c eq '') {
            # Allow section-header echo/printf banners and lightweight status steps inside multi-step chains
            if ($target_s =~ /^(?:echo|printf)\s+.*(?:===|---)/ || $target_s =~ /^(?:ls|pwd|uname|date|whoami|id)(?:\s|$)/) {
                push @cats, '_setup';
                push @new_segs, $s;
                next;
            }
            # Unknown/custom segment (e.g. "pytest && echo DONE") -> pass through untouched
            print '{"decision":"allow"}';
            exit 0;
        }
        push @cats, ($seg_has_pipe && $c =~ /^(?:find|search)$/) ? 'search' : $c;
        if (!$seg_has_pipe && defined $mut && $clean_s eq $s) {
            push @new_segs, $mut;
            $mutated = 1;
        } else {
            push @new_segs, $s;
        }
    }
    my @non_setup = grep { $_ ne '_setup' } @cats;
    if (!@non_setup) {
        print '{"decision":"allow"}';
        exit 0;
    }
    if (@non_setup == 1) {
        $final_cat = $non_setup[0];
        $final_cmd = rebuild(\@new_segs, $mutated);
    } else {
        # Allow multi-step chains when all non-setup steps are known noisy categories
        for my $nc (@non_setup) {
            if ($nc !~ /^(?:pkg|build|test|lint|git_net|git_diff|git_log|iac|ssh|logs|table|find|search|ps|http|cloud|archive)$/) {
                print '{"decision":"allow"}';
                exit 0;
            }
        }
        $final_cat = 'chain';
        $final_cmd = rebuild(\@new_segs, $mutated);
    }
}

# Resolve plugin root without Cwd/File::Basename
my $plugin_root = $ENV{PLUGIN_ROOT};
if (!$plugin_root) {
    my $script = $0;
    $script =~ s{\\}{/}g;    # Windows-style $0 (C:\...\scripts\cmd_hook.pl) -> C:/.../scripts/...
    $script = "$ENV{PWD}/$script" if $script !~ m{^(?:/|[A-Za-z]:/)} && $ENV{PWD};
    $script =~ s{/[^/]+/[^/]+$}{};
    $plugin_root = $script;
}
1 while $plugin_root =~ s{/\./}{/};

# @plexer/cli duplicate search/git_diff guard + TokenEconomizer (arXiv:2610.22732) failure-loop breaker.
# Only active when conversationId & stepIdx are provided; cleared by post_edit_hook.pl on any edit.
# - search/find/git_diff: blocks 2nd identical run with no edits (1-retry escape hatch).
# - test/build/lint: blocks 3rd identical consecutive run with no edits (breaks retry loops; 1-retry escape hatch).
if ($final_cat =~ /^(?:search|find|git_diff|test|build|lint)$/) {
    my $is_loop_cat = ($final_cat =~ /^(?:test|build|lint)$/) ? 1 : 0;
    my $min_prior   = $is_loop_cat ? 2 : 1;
    my $cid  = json_get_str($raw, 'conversationId') // '';
    my $step = ($raw =~ /"stepIdx"\s*:\s*"?(-?\d+)"?/) ? int($1) : undef;
    if ($cid ne '' && defined $step) {
        eval {
            local $/ = "\n";
            my $home = $ENV{HOME} // $ENV{USERPROFILE} // '';
            my $agy_pd = "$home/.gemini/antigravity/plugin_data";
            my $dd = $ENV{PLUGIN_DATA} || (-d $agy_pd ? "$agy_pd/token-saver" : "$plugin_root/.data");
            my $rdir = "$dd/reads";
            (my $scid = $cid) =~ s/[^A-Za-z0-9_-]/_/g;

            my ($eseq, $ep) = (0, 0);
            if (-f "$rdir/$scid.editseq" && open(my $ef, '<', "$rdir/$scid.editseq")) { $eseq = int(<$ef> // 0); close $ef; }
            if (-f "$dd/alerts/$scid.epoch" && open(my $epf, '<', "$dd/alerts/$scid.epoch")) { $ep = int(<$epf> // 0); close $epf; }

            my $skey = "$final_cat\t$cwd\t$trimmed";
            my $sfile = "$rdir/$scid.searches";
            my $bfile = "$rdir/$scid.lastblk";
            my $last_blk = '';
            if (-f $bfile && open(my $bf, '<', $bfile)) { $last_blk = <$bf> // ''; chomp $last_blk; close $bf; }

            my @hist;
            my ($dup_delta, $dup_cnt) = (undef, 0);
            if (-f $sfile && open(my $sf, '<', $sfile)) {
                while (my $sl = <$sf>) {
                    chomp $sl;
                    my ($sq, $sep, $sst, $sk) = split(/\t/, $sl, 4);
                    next unless defined $sk;
                    if (int($sq) == $eseq && int($sep) == $ep && $sk eq $skey) {
                        my $d = $step - int($sst);
                        if ($d > 0 && $d <= 16) {
                            $dup_delta = $d;
                            $dup_cnt++;
                        }
                    }
                    push @hist, $sl;
                }
                close $sf;
            }
            if (defined $dup_delta && $dup_cnt >= $min_prior) {
                if ($last_blk eq $skey) {
                    # 1-retry escape hatch: model insisted, let it run
                    unlink($bfile);
                } else {
                    if (!-d $rdir) { require File::Path; File::Path::make_path($rdir); }
                    if (open(my $wbf, '>', $bfile)) { print $wbf "$skey\n"; close $wbf; }
                    my $msg = $is_loop_cat
                        ? "[token-saver] Identical $final_cat already ran ${dup_cnt}x with no edits since; diagnose before retrying (re-run once to force)."
                        : "[token-saver] Identical $final_cat ran ${dup_delta} steps ago with no edits since; reuse prior output or re-run to force.";
                    print '{"decision":"deny","reason":"' . json_escape($msg) . '"}';
                    exit 0;
                }
            }
            push @hist, join("\t", $eseq, $ep, $step, $skey);
            @hist = @hist[-64 .. -1] if @hist > 64;
            if (!-d $rdir) { require File::Path; File::Path::make_path($rdir); }
            if (open(my $wsf, '>', $sfile)) { print $wsf join("\n", @hist) . "\n"; close $wsf; }
        };
    }
}

my $wrapper = "$plugin_root/scripts/cmd_wrap.pl";
if (!-f $wrapper) { print '{"decision":"allow"}'; exit 0; }

my $ew = $wrapper;    $ew =~ s/'/'\\''/g;
my $ec = $final_cmd;  $ec =~ s/'/'\\''/g;
my $new_cmd = "perl '$ew' --cat $final_cat -- '$ec'";

# @plexer/cli-inspired WaitMsBeforeAsync floor bump on fast read-only queries to avoid unnecessary backgrounding
my $extra_ov = '';
if ($final_cat =~ /^(?:search|find|git_diff|git_log|ps|table)$/ &&
    $raw =~ /"WaitMsBeforeAsync"\s*:\s*(\d+)/ && $1 > 0 && $1 < 3000) {
    $extra_ov = ',"WaitMsBeforeAsync":5000';
}

print '{"decision":"allow","overwrite":{"' . $cmd_key . '":"' . json_escape($new_cmd) . '"' . $extra_ov . '}}';
exit 0;
