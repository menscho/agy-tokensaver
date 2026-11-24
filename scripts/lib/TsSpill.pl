use strict;
use warnings;
use Fcntl qw(O_WRONLY O_CREAT O_TRUNC);
our ($SPILL_DIR, $t0, $NOTICE_BYTES, $NATIVE_CAP, $NATIVE_PREVIEW);

sub ensure_spill_dir {
    return 0 if -l $SPILL_DIR;
    my $created = 0;
    if (!-d $SPILL_DIR) {
        $created = mkdir($SPILL_DIR, 0700) ? 1 : 0;
    }
    return 0 unless -d $SPILL_DIR && !-l $SPILL_DIR;
    if ($^O eq 'MSWin32') {
        if ($created && defined $ENV{USERNAME} && $ENV{USERNAME} ne '') {
            # Restrict spill directory ACL to current user on native Windows (fail-open)
            my $wdir = $SPILL_DIR;
            $wdir =~ s{/}{\\}g;
            eval { system('icacls', $wdir, '/inheritance:r', '/grant:r', "$ENV{USERNAME}:(OI)(CI)F", '/Q'); };
        }
        return 1;
    }
    chmod(0700, $SPILL_DIR);
    my @st = stat($SPILL_DIR);
    return (@st && $st[4] == $<) ? 1 : 0;
}

sub cleanup_spill_dir {
    my $force = ($ENV{AGY_TS_SPILL_CLEANUP} // '') eq '1';
    return unless $force || int(rand(16)) == 0;
    return unless opendir(my $dh, $SPILL_DIR);
    my $days = (defined $ENV{AGY_TS_SPILL_DAYS} && $ENV{AGY_TS_SPILL_DAYS} =~ /\A\d+(?:\.\d+)?\z/)
        ? ($ENV{AGY_TS_SPILL_DAYS} + 0)
        : 7;
    my $cutoff = $days > 0 ? (time() - int($days * 86400)) : 0;
    my @files;
    while (my $f = readdir($dh)) {
        next if $f eq '.' || $f eq '..';
        my $p = "$SPILL_DIR/$f";
        next if -l $p;
        my @st = stat($p);
        next unless @st && -f $p;
        if ($cutoff > 0 && $st[9] < $cutoff) {
            unlink($p);
        } else {
            push @files, [$st[9], $p];
        }
    }
    closedir($dh);
    # Enforce max 200 files cap
    if (@files > 200) {
        @files = sort { $a->[0] <=> $b->[0] } @files;
        unlink($files[$_][1]) for (0 .. $#files - 200);
    }
}

sub content_hash12 {
    my ($text) = @_;
    # Pure-Perl zero-module dual 32-bit FNV-1a content hash -> deterministic 12-hex spill name
    # (arXiv:2601.06007: zero random/timestamp entropy in tool outputs for stable KV prefixes)
    my $h1 = 2166136261;
    my $h2 = 3735928559;
    my $len = length($text);
    my $step = $len > 65536 ? int($len / 16384) : 1;
    for (my $i = 0; $i < $len; $i += $step) {
        my $b = ord(substr($text, $i, 1));
        $h1 = (($h1 ^ $b) * 16777619) & 0xffffffff;
        $h2 = (($h2 ^ ($b + ($i & 0xff))) * 16777619) & 0xffffffff;
    }
    $h2 = (($h2 ^ ($len & 0xffffffff)) * 16777619) & 0xffffffff;
    return sprintf('%08x%04x', $h1, $h2 & 0xffff);
}

sub write_spillback {
    my ($text) = @_;
    return '' unless ensure_spill_dir();
    cleanup_spill_dir();
    my $hex = content_hash12($text);
    my $fp = "$SPILL_DIR/$hex.log";
    return '' if -l $fp;
    if (-f $fp && (stat($fp))[7] == length($text)) {
        chmod(0600, $fp) unless $^O eq 'MSWin32';
        utime(undef, undef, $fp);
        return $fp;
    }
    if (sysopen(my $fh, $fp, O_WRONLY | O_CREAT | O_TRUNC, 0600)) {
        binmode($fh, ':raw');
        print $fh $text;
        close $fh;
        chmod(0600, $fp) unless $^O eq 'MSWin32';
        return $fp;
    }
    return '';
}

sub json_escape {
    my ($s) = @_;
    $s =~ s/\\/\\\\/g;
    $s =~ s/"/\\"/g;
    $s =~ s/\n/\\n/g;
    $s =~ s/\r/\\r/g;
    $s =~ s/\t/\\t/g;
    return $s;
}

sub log_telemetry {
    my ($category, $raw_bytes, $kept_bytes, $rc) = @_;
    eval {
        my $plugin_root = $ENV{PLUGIN_ROOT};
        if (!$plugin_root) {
            my $s = $0; $s = "$ENV{PWD}/$s" if $s !~ m{^/} && $ENV{PWD};
            $s =~ s{/[^/]+/[^/]+$}{}; $plugin_root = $s;
        }
        my $agy_pd = ($ENV{HOME} // '') . "/.gemini/antigravity/plugin_data";
        my $data_dir = $ENV{PLUGIN_DATA} || (-d $agy_pd ? "$agy_pd/token-saver" : "$plugin_root/.data");
        if (!-d $data_dir) {
            require File::Path;
            File::Path::make_path($data_dir);
        }
        # Honest net savings vs. what Antigravity would natively have shown (capped preview),
        # minus the overwrite notice this wrap cost. May be negative (passthrough = -23 tok).
        my $native = $raw_bytes > $NATIVE_CAP ? $NATIVE_PREVIEW : $raw_bytes;
        my $net = $native - $kept_bytes - $NOTICE_BYTES;
        my $action = $kept_bytes < $raw_bytes ? 'compressed' : 'passthrough';
        my $rec = sprintf(
            '{"ts":%d,"source":"cmd_wrap","category":"%s","action":"%s","rc":%d,"raw_bytes":%d,"kept_bytes":%d,"est_tokens_saved":%d}',
            time(), json_escape($category), $action, int($rc), int($raw_bytes), int($kept_bytes), int($net / 4)
        );
        if (open my $fh, '>>', "$data_dir/events.jsonl") {
            print $fh "$rec\n";
            close $fh;
        }
    };
}

# --- Universal lossless terminal sanitizer ---
# $preserve_ws: 0 = full cleanup; 1 = verbatim code (git_diff/cat_large: keep CRLF, blank runs,
# trailing ws, base64); 2 = remote/raw text (ssh: normalize CRLF/progress bars only).

1;
