use strict;
use warnings;
our @EXCLUDES;

# Secondary POSIX command classifier (sections 8-13: find/tree, cat, containers/k8s/logs,
# curl/wget, archives/sync, devops/cloud/ps/ls/python/du/df). Loaded on demand by cmd_classify.pl.

sub classify_segment_sys {
    my ($bin, $sub, $sub2, $arg_str, $seg, $idx, $wrapped_inner, $toks_ref, $args_ref) = @_;
    my @toks = @$toks_ref;
    my @args = @$args_ref;

    # --- 8. find / tree with auto-prune injection ---
    if ($bin eq 'find') {
        return ('', undef, undef) if $arg_str =~ /(?:^|\s)(?:-exec|-execdir|-ok|-okdir|-delete)\b/;
        return ('', undef, undef) if $arg_str =~ /(?:^|\s)-maxdepth\s+[0-2]\b/;
        if ($idx == 0 && !$wrapped_inner && $arg_str !~ /(?:^|\s)(?:-prune|-depth|-D)\b/) {
            my @keep_ex = grep { index($seg, $_) < 0 } @EXCLUDES;
            if (@keep_ex) {
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
    return ('search', undef, undef) if $bin eq 'strings';

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
    return ('iac', undef, undef) if $bin eq 'ansible-playbook';
    if ($bin eq 'aws' || $bin eq 'gcloud' || $bin eq 'az') {
        return ('', undef, undef) if $arg_str =~ /\blogs\s+tail\b/;
        if ($bin ne 'gcloud') {
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
            my ($total, $has_glob) = (0, 0);
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
        my @nonflags = grep { $_ !~ /^-/ } @args;
        if (@nonflags && $nonflags[0] =~ /(?:^|\/)(?:test_[^\/]*|[^\/]*_test|run_tests|setup|bench[^\/]*|audit[^\/]*|build[^\/]*|migrate[^\/]*)\.py$/i) {
            return ('logs', undef, undef);
        }
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

1;
