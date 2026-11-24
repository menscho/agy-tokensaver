#!/usr/bin/env perl
use strict;
use warnings;
BEGIN {
    my $d = $0; $d =~ s{[^/\\]*$}{}; $d = '.' if $d eq ''; $d =~ s{[/\\]+$}{};
    unshift @INC, "$d/lib";
}
use TsJson;

# Core classifier for the run_command / shell_exec PreToolUse hook (see cmd_hook.pl).
# - Decodes payload once via JSON::PP (TsJson).
# - Quote-aware metacharacter safety check & &&-chain classifier.
# - Auto-injects --exclude-dir/-I on recursive grep, -prune on find, -I on tree.
# - Wraps high-ROI noisy categories via cmd_wrap.pl.
# - Strict fail-open on any edge case or error.

our $RAW;
my $raw = defined $RAW ? $RAW : TsJson::read_stdin_capped();
my $doc = TsJson::decode_hook_input($raw);
if (!defined $doc) { print '{"decision":"allow"}'; exit 0; }

my $ti = TsJson::tool_input($doc);
sub json_escape { return TsJson::json_escape(@_); }

# Never wrap background daemons
if (TsJson::get_bool($ti, 'IsDaemon')) { print '{"decision":"allow"}'; exit 0; }

my $cmd_key;
my $cmd = TsJson::get_str($ti, 'CommandLine');
if (defined $cmd) { $cmd_key = 'CommandLine'; }
else {
    $cmd = TsJson::get_str($ti, 'command');
    $cmd_key = 'command' if defined $cmd;
}
if (!defined $cmd || $cmd eq '') { print '{"decision":"allow"}'; exit 0; }

# Skip if already wrapped by cmd_wrap
if ($cmd =~ /cmd_wrap\.(?:pl|py)\S*\s+--cat\b/) { print '{"decision":"allow"}'; exit 0; }

# Windows host => Antigravity executes the command line in Windows PowerShell 5.1 (ConPTY).
# The POSIX path below (bash -c) must never run there; use the PowerShell wrapper instead.
my $top_cwd = TsJson::get_str($doc, 'cwd') // '';
my $ti_cwd  = TsJson::get_str($ti, 'Cwd', 'cwd');
my $raw_cwd = defined $ti_cwd ? $ti_cwd : $top_cwd;
our $PWSH;
if (!defined $PWSH) {
    my $sh = lc($ENV{AGY_TS_SHELL} // '');
    if ($sh eq 'off') { print '{"decision":"allow"}'; exit 0; }
    $PWSH = ($sh =~ /^(?:pwsh|powershell)$/ || ($sh !~ /^(?:posix|bash|sh)$/ && $^O =~ /^(?:MSWin32|cygwin|msys)$/ &&
             ($raw_cwd eq '' || $raw_cwd =~ /^(?:[A-Za-z]:(?:\\|$)|\\\\)/ || ($raw_cwd =~ m{^[A-Za-z]:/} && $top_cwd !~ m{^/})))) ? 1 : 0;
}

# ---------------------------------------------------------------------------------------------
# PowerShell path. The rewritten command runs the model's EXACT script text in a child
# `powershell.exe -NoProfile -NonInteractive -EncodedCommand` (cmd_wrap.pl), so PowerShell
# parsing/quoting semantics are byte-identical; only stdout+stderr are captured & compressed.
# ---------------------------------------------------------------------------------------------

# Mask string literals, here-strings and comments (quote-aware) so checks see only code.

my $LIB_DIR = do {
    my $d = $0;
    $d =~ s{[^/\\]*$}{};
    $d = '.' if $d eq '';
    $d =~ s{[/\\]+$}{};
    $d = "./$d" if $d !~ m{^(?:/|\\|[A-Za-z]:|\.{1,2}[/\\])};
    "$d/lib";
};
if ($PWSH) {
    do "$LIB_DIR/cmd_pwsh.pl" or die "cmd_pwsh.pl: " . ($@ || $!);
    pwsh_hook($cmd, $cmd_key);
    exit 0;
}
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

our $cwd = $raw_cwd;
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

our $trimmed = $cmd;
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

our @EXCLUDES = qw(.git node_modules dist build .next .nuxt target __pycache__ .venv venv .tox .cache coverage .turbo);

# Classify a single simple segment; returns ($cat, $mutated_seg_or_undef, $deny_reason_or_undef)

do "$LIB_DIR/cmd_classify.pl" or die "cmd_classify.pl: " . ($@ || $!);

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
    my $cid  = TsJson::get_str($doc, 'conversationId') // '';
    my $step = TsJson::get_int($doc, 'stepIdx');
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
my $wait_ms = TsJson::get_int($ti, 'WaitMsBeforeAsync');
if ($final_cat =~ /^(?:search|find|git_diff|git_log|ps|table)$/ &&
    defined $wait_ms && $wait_ms > 0 && $wait_ms < 3000) {
    $extra_ov = ',"WaitMsBeforeAsync":5000';
}

print '{"decision":"allow","overwrite":{"' . $cmd_key . '":"' . json_escape($new_cmd) . '"' . $extra_ov . '}}';
exit 0;
