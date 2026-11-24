use strict;
use warnings;
our ($cwd, $trimmed, @EXCLUDES);
my $_SYS_LOADED = 0;

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

    if (!$_SYS_LOADED) {
        $_SYS_LOADED = 1;
        my $d = $0; $d =~ s{[^/\\]*$}{}; $d = '.' if $d eq ''; $d =~ s{[/\\]+$}{};
        $d = "./$d" if $d !~ m{^(?:/|\\|[A-Za-z]:|\.{1,2}[/\\])};
        do "$d/lib/cmd_classify_sys.pl" or return ('', undef, undef);
    }
    return classify_segment_sys($bin, $sub, $sub2, $arg_str, $seg, $idx, $wrapped_inner, \@toks, \@args);
}

1;
