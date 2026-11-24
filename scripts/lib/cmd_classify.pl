use strict;
use warnings;
our ($cwd, $trimmed, @EXCLUDES);

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

1;
