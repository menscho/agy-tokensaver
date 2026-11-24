use strict;
use warnings;
our ($NATIVE_CAP, $LOSSY, $LOSSLESS_RM, $LOCKFILE_DIFF_RE, $EXTRA_SIG_RE, $cat);

my $JSON_ERR_RE = qr/\b(?:error|exception|failed|failure|critical|fatal|crash|panic|abort|timeout|denied|rejected)\b/i;

sub _csv_cell {
    my ($v) = @_;
    return '' unless defined $v;
    if (ref($v) eq 'JSON::PP::Boolean') { return $v ? 'true' : 'false'; }
    if (ref($v)) {
        my $s = eval { JSON::PP::encode_json($v) } // '';
        if (length($s) > 256) { $LOSSY = 1; return "[blob:" . length($s) . "B]"; }
        $v = $s;
    } elsif (length($v) > 256) {
        $LOSSY = 1;
        return "[blob:" . length($v) . "B]";
    }
    if ($v =~ /[,"\r\n]/) {
        $v =~ s/"/""/g;
        return qq{"$v"};
    }
    return $v;
}

sub _flatten_row {
    my ($h) = @_;
    my %flat;
    for my $k (keys %$h) {
        my $v = $h->{$k};
        if (ref($v) eq 'HASH' && scalar(keys %$v) >= 1 && scalar(keys %$v) <= 4) {
            my $all_scalar = 1;
            for my $sv (values %$v) {
                if (ref($sv) && ref($sv) ne 'JSON::PP::Boolean') { $all_scalar = 0; last; }
            }
            if ($all_scalar) {
                for my $sk (keys %$v) { $flat{"$k.$sk"} = $v->{$sk}; }
                next;
            }
        }
        $flat{$k} = $v;
    }
    return \%flat;
}

sub crush_json_output {
    my ($lines, $joined) = @_;
    return undef if length($joined) < 512 || $joined !~ /\A\s*[\[{]/;
    eval { require JSON::PP; 1 } or return undef;
    my $data = eval { JSON::PP::decode_json($joined) };
    return undef if $@ || !defined $data;

    my ($arr, @meta_hdr);
    if (ref($data) eq 'ARRAY') {
        $arr = $data;
    } elsif (ref($data) eq 'HASH') {
        my @arr_keys = sort grep { ref($data->{$_}) eq 'ARRAY' && @{$data->{$_}} >= 3 } keys %$data;
        return undef unless @arr_keys == 1;
        my $ak = $arr_keys[0];
        $arr = $data->{$ak};
        for my $mk (sort keys %$data) {
            next if $mk eq $ak;
            my $mv = $data->{$mk};
            next if ref($mv) && ref($mv) ne 'JSON::PP::Boolean';
            push @meta_hdr, "# $mk: " . _csv_cell($mv);
        }
        push @meta_hdr, "# array: $ak";
    } else {
        return undef;
    }

    my $n = scalar(@$arr);
    return undef if $n < 3;
    my @hashes = grep { ref($_) eq 'HASH' } @$arr;
    return undef if @hashes < $n * 0.80;

    my @flat_rows = map { ref($_) eq 'HASH' ? _flatten_row($_) : { _val => $_ } } @$arr;
    my %key_freq;
    for my $r (@flat_rows) { $key_freq{$_}++ for keys %$r; }
    my @cols = sort { $key_freq{$b} <=> $key_freq{$a} || $a cmp $b } keys %key_freq;
    return undef if !@cols || @cols > 25;

    my $prev_lossy = $LOSSY;
    my @csv_rows;
    my @row_meta;
    for my $idx (0 .. $#flat_rows) {
        my $r = $flat_rows[$idx];
        my $row_str = join(',', map { _csv_cell($r->{$_}) } @cols);
        push @csv_rows, $row_str;
        my $is_err = ($row_str =~ $JSON_ERR_RE || (exists $r->{status} && defined $r->{status} && $r->{status} =~ /^[45]\d\d$/)) ? 1 : 0;
        my $is_out = 0;
        for my $rk (keys %$r) {
            if ($key_freq{$rk} < $n * 0.20) { $is_out = 1; last; }
        }
        push @row_meta, [$is_err, $is_out];
    }

    my $schema_hdr = "[$n]{" . join(',', @cols) . "}";
    my @full_out = (@meta_hdr, $schema_hdr, @csv_rows);
    my $full_bytes = total_bytes(\@full_out);
    my $ws_comp = json_ws_compact($lines);
    my $ws_bytes = total_bytes($ws_comp);

    # Path 1: Fits inside $NATIVE_CAP -> emit lossless CSV+schema if it saves >= 15% over compact JSON
    if ($full_bytes <= $NATIVE_CAP) {
        if ($full_bytes <= $ws_bytes * 0.85) {
            return \@full_out;
        }
        $LOSSY = $prev_lossy;
        return undef;
    }

    # Path 2: Still > $NATIVE_CAP -> Headroom error-preserving adaptive sampling
    $LOSSY = 1;
    my %keep;
    # Head (first 3) & tail (last 2)
    $keep{$_} = 1 for (0 .. ($n > 3 ? 2 : $n - 1));
    $keep{$_} = 1 for (($n > 2 ? $n - 2 : 0) .. $n - 1);
    # Non-negotiable errors + structural outliers
    for my $idx (0 .. $#row_meta) {
        $keep{$idx} = 1 if $row_meta[$idx][0] || $row_meta[$idx][1];
    }
    # Fill remaining budget up to 55 rows with even stride
    my $budget = 55;
    my $have = scalar(keys %keep);
    if ($have < $budget && $n > $have) {
        my $need = $budget - $have;
        my $step = $n / ($need + 1);
        for (my $k = 1; $k <= $need; $k++) {
            my $idx = int($k * $step);
            $idx = $n - 1 if $idx >= $n;
            $keep{$idx} = 1;
        }
    }
    my @kept_idx = sort { $a <=> $b } keys %keep;
    my @sampled = (@meta_hdr, $schema_hdr);
    my $prev_i = -1;
    for my $idx (@kept_idx) {
        if ($prev_i >= 0 && $idx > $prev_i + 1) {
            my $gap = $idx - $prev_i - 1;
            push @sampled, "... [$gap rows omitted]";
        }
        push @sampled, $csv_rows[$idx];
        $prev_i = $idx;
    }
    push @sampled, "[SmartCrusher: " . scalar(@kept_idx) . "/$n rows kept; all error/outlier rows preserved]";
    return \@sampled;
}

# Whitespace-only table compaction. Only touches lines that are unambiguously tabular so code,
# YAML block scalars, string literals and shell operators (`a || b`) are never altered.

my $JSON_SCALAR_RE = qr/(?:"(?:[^"\\]|\\.)*"|-?\d[\d.eE+\-]*|true|false|null)/;
sub json_ws_compact {
    my ($lines) = @_;
    my @out;
    my $n = scalar(@$lines);
    my $k = 0;
    while ($k < $n) {
        my $l = $lines->[$k];
        if ($l =~ /^( *)(?:\S.*[:\s])?[\[{]$/ || $l =~ /^( *)[\[{]$/) {
            my $i0 = length($1);
            my $j = $k + 1;
            my $unit = 0;
            my $ok = 1;
            while ($j < $n) {
                my $x = $lines->[$j];
                last if $x =~ /^ {$i0}[\]}],?$/;
                my ($ind) = $x =~ /^( *)/;
                if (length($ind) <= $i0 || $x !~ /^ *(?:"(?:[^"\\]|\\.)*"\s*:\s*|)(?:$JSON_SCALAR_RE,?|[\[{]|[\]}],?|\[\],?|\{\},?)$/) { $ok = 0; last; }
                my $d = length($ind) - $i0;
                $unit = $d if !$unit || $d < $unit;
                $j++;
            }
            if ($ok && $j < $n && $j - $k >= 3 && $unit >= 2) {
                my @blk = @{$lines}[$k .. $j];
                for my $x (@blk[1 .. $#blk - 1]) {
                    my ($ind) = $x =~ /^( *)/;
                    my $lvl = int((length($ind) - $i0) / $unit);
                    $x = (' ' x ($i0 + $lvl)) . substr($x, length($ind));
                }
                # Arrays of scalars -> one line (<= 200 chars)
                my @m;
                for (my $q = 0; $q < @blk; $q++) {
                    my $x = $blk[$q];
                    if ($x =~ /\[$/) {
                        my $e = $q + 1;
                        my @items;
                        while ($e < @blk && $blk[$e] =~ /^ *($JSON_SCALAR_RE),?$/) { push @items, $1; $e++; }
                        if (@items >= 2 && $e < @blk && $blk[$e] =~ /^ *\](,?)$/) {
                            my $one = $x . join(', ', @items) . "]$1";
                            if (length($one) <= 200) {
                                push @m, $one;
                                $LOSSLESS_RM += $e - $q;
                                $q = $e;
                                next;
                            }
                        }
                    }
                    push @m, $x;
                }
                push @out, @m;
                $k = $j + 1;
                next;
            }
        }
        push @out, $l;
        $k++;
    }
    return \@out;
}

# Byte-budgeted signal-preserving cap for output above Antigravity's native cap: the model
# natively gets a ~9.9 KB head+tail preview; we keep head, tail and every error/warning line
# (capped at 400 chars each) within ~5 KB, with the complete raw output spilled to disk.

1;
