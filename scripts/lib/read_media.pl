use strict;
use warnings;
our ($doc, $ti, $path, $check_path, $COMPACT_READS);

sub img_dims {
    my ($p) = @_;
    open(my $fh, '<:raw', $p) or return (0, 0);
    my $buf = '';
    my $n = sysread($fh, $buf, 32768) // 0;
    close $fh;
    return (0, 0) if $n < 24;
    # PNG: 8-byte signature + IHDR length/type + width (4B BE) + height (4B BE)
    if (substr($buf, 0, 8) eq "\x89PNG\r\n\x1a\n" && substr($buf, 12, 4) eq 'IHDR') {
        my ($w, $h) = unpack('NN', substr($buf, 16, 8));
        return ($w, $h);
    }
    # JPEG: scan markers for SOF0/SOF1/SOF2 (0xC0/0xC1/0xC2)
    if (substr($buf, 0, 2) eq "\xff\xd8") {
        my $pos = 2;
        while ($pos + 9 < $n) {
            last unless ord(substr($buf, $pos, 1)) == 0xff;
            my $m = ord(substr($buf, $pos + 1, 1));
            $pos += 2;
            next if $m == 0xff || $m == 0x00 || $m == 0x01 || ($m >= 0xd0 && $m <= 0xd9);
            my $len = unpack('n', substr($buf, $pos, 2));
            last if $len < 2 || $pos + $len > $n;
            if ($m == 0xc0 || $m == 0xc1 || $m == 0xc2) {
                my ($h, $w) = unpack('nn', substr($buf, $pos + 3, 4));
                return ($w, $h);
            }
            $pos += $len;
        }
    }
    return (0, 0);
}

# Multimedia / document formats natively handled by read_file (NO svg — read_file returns raw XML!)
if ($check_path =~ /\.(?:png|jpe?g|gif|webp|ico|bmp|tiff?|pdf|mp4|mov|webm|avi|mkv|mp3|wav|ogg|m4a|flac|aac)$/i) {
    if ($check_path =~ /\.(?:png|jpe?g|gif|webp)$/i) {
        my $cid  = TsJson::get_str($doc, 'conversationId') // '';
        my $tx   = TsJson::get_str($doc, 'transcriptPath') // '';
        my $step = TsJson::get_int($doc, 'stepIdx');
        my $pr   = $ENV{PLUGIN_ROOT};
        if (!$pr) { my $s = $0; $s = "$ENV{PWD}/$s" if $s !~ m{^/} && $ENV{PWD}; $s =~ s{/[^/]+/[^/]+$}{}; $pr = $s; }
        my $agy_pd = ($ENV{HOME} // '') . "/.gemini/antigravity/plugin_data";
        my $dd = $ENV{PLUGIN_DATA} || (-d $agy_pd ? "$agy_pd/token-saver" : "$pr/.data");
        my $scid = $cid; $scid =~ s/[^A-Za-z0-9_-]/_/g;
        my @ist = stat($path);
        my ($isz, $imt) = @ist ? ($ist[7], $ist[9]) : (0, 0);

        # 1. Zero-loss Duplicate Image Read Guard: skip unchanged re-reads within 16 steps
        if ($cid ne '' && defined $step && $isz > 0) {
            my $idir = "$dd/reads";
            my $idup = "$idir/$scid.imgdup";
            my $ikey = "$check_path\t$imt\t$isz";
            my @kept_i;
            my $dup_step;
            if (-f $idup && open(my $dfh, '<', $idup)) {
                while (my $ln = <$dfh>) {
                    chomp $ln;
                    my ($st_n, $k) = split(/\t/, $ln, 2);
                    next unless defined $k;
                    if ($k eq $ikey && $step >= int($st_n) && ($step - int($st_n)) <= 16) {
                        $dup_step = int($st_n);
                    }
                    push @kept_i, $ln if ($step - int($st_n)) <= 40;
                }
                close $dfh;
            }
            if (defined $dup_step) {
                my $ib = $check_path; $ib =~ s{^.*[/\\]}{};
                my $msg = "[token-saver] Image '$ib' is unchanged and still in context from step $dup_step (~1.5k vision tok/turn saved).";
                print '{"decision":"deny","reason":"' . json_escape($msg) . '"}';
                exit 0;
            }
            eval {
                if (!-d $idir) { require File::Path; File::Path::make_path($idir); }
                push @kept_i, "$step\t$ikey";
                @kept_i = @kept_i[-40 .. -1] if @kept_i > 40;
                if (open(my $wfh, '>', $idup)) { print $wfh join("\n", @kept_i) . "\n"; close $wfh; }
            };
        }

        # 2. Image-burst guard for long sessions (>3 images AND transcript >150KB)
        if ($cid ne '' && $tx ne '' && -f $tx && ((stat($tx))[7] // 0) > 153600) {
            my $idir = "$dd/reads";
            my $ifile = "$idir/$scid.imgcnt";
            my $cnt = 0;
            if (-f $ifile && open(my $ifh, '<', $ifile)) { $cnt = int(<$ifh> // 0); close $ifh; }
            $cnt++;
            if ($cnt > 3) {
                my $msg = "[token-saver] Image #${cnt} in a long session (>150KB transcript; each image replays ~1.5K tok/turn). Inspect only necessary frames or delegate multi-image work to a separate subagent.";
                print '{"decision":"deny","reason":"' . json_escape($msg) . '"}';
                exit 0;
            }
            eval {
                if (!-d $idir) { require File::Path; File::Path::make_path($idir); }
                if (open(my $ofh, '>', $ifile)) { print $ofh "$cnt\n"; close $ofh; }
            };
        }

        # 3. Oversized Screenshot Auto-Downscaler (opt-in Mode 2 / compact_reads: >1568px -> <=1280px)
        my $img_compact = 0;
        if (defined $ENV{AGY_TS_COMPACT_READS}) {
            $img_compact = ($ENV{AGY_TS_COMPACT_READS} =~ /^(?:1|true|on|yes)$/i) ? 1 : 0;
        } elsif (-f "$dd/hud_config.json" && open(my $cfh, '<:raw', "$dd/hud_config.json")) {
            my $cb = ''; sysread($cfh, $cb, 4096); close $cfh;
            $img_compact = 1 if $cb =~ /"compact_reads"\s*:\s*true\b/i;
        }
        if ($img_compact && $check_path =~ /\.(?:png|jpe?g)$/i && $isz > 65536) {
            my ($iw, $ih) = img_dims($path);
            if ($iw > 1568 || $ih > 1568) {
                my $tmpb = $ENV{TMPDIR} || $ENV{TEMP} || $ENV{TMP} || '/tmp';
                $tmpb =~ s{[\\/]+$}{};
                my $ut = ($^O eq 'MSWin32') ? ($ENV{USERNAME} || 'user') : $<;
                $ut =~ s/[^A-Za-z0-9_-]/_/g;
                my $sdir = "$tmpb/agy-tokensaver-$ut";
                mkdir($sdir, 0700) unless -d $sdir;
                # Deterministic content-addressed cache filename
                my $h = 2166136261;
                for my $ch (split(//, "$path|$imt|$isz")) { $h = (($h ^ ord($ch)) * 16777619) & 0xffffffff; }
                my $dst = sprintf('%s/img-%08x.jpg', $sdir, $h);
                if (!-f $dst) {
                    my $ok = 0;
                    # Prefer zero-dep native tools (ffmpeg / magick / sips) with strict fail-open
                    if (!$ok && system('ffmpeg', '-v', 'error', '-y', '-i', $path, '-vf', "scale='min(1280,iw)':'min(1280,ih)':force_original_aspect_ratio=decrease", '-q:v', '4', $dst) == 0 && -s $dst) {
                        $ok = 1;
                    }
                    if (!$ok && $^O eq 'darwin' && system('sips', '-Z', '1280', '-s', 'format', 'jpeg', $path, '--out', $dst) == 0 && -s $dst) {
                        $ok = 1;
                    }
                    if (!$ok && system('magick', $path, '-resize', '1280x1280>', '-quality', '85', $dst) == 0 && -s $dst) {
                        $ok = 1;
                    }
                }
                if (-s $dst) {
                    my $key = (defined TsJson::get_str($ti, 'AbsolutePath')) ? 'AbsolutePath' : 'Path';
                    print '{"decision":"allow","overwrite":{"' . $key . '":"' . json_escape($dst) . '"}}';
                    exit 0;
                }
            }
        }
    }
    print '{"decision":"allow"}';
    exit 0;
}


1;
