use strict;
use warnings;

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


1;
