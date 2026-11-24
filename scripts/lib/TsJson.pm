package TsJson;
use strict;
use warnings;
use JSON::PP ();

# Shared JSON::PP hook payload parser for agy-tokensaver Perl hooks.
# Zero third-party dependencies (JSON::PP is core Perl since 5.14).
# Always fails open: oversized, empty, non-object, or malformed payloads return undef.

our $MAX_PAYLOAD_BYTES = 2 * 1024 * 1024; # 2 MB cap keeps 10 MB fuzz inputs <10 ms
my $_DECODER;

sub _decoder {
    return $_DECODER ||= JSON::PP->new->utf8(1)->max_depth(64);
}

sub read_stdin_capped {
    my ($max) = @_;
    $max = $MAX_PAYLOAD_BYTES unless defined $max && $max > 0;
    binmode(STDIN);
    my $buf = '';
    my $need = $max + 1;
    while (length($buf) < $need) {
        my $chunk = '';
        my $n = read(STDIN, $chunk, $need - length($buf));
        last if !defined $n || $n <= 0;
        $buf .= $chunk;
    }
    return undef if length($buf) == 0 || length($buf) > $max;
    return $buf;
}

sub decode_hook_input {
    my ($raw) = @_;
    return undef if !defined $raw || $raw eq '' || length($raw) > $MAX_PAYLOAD_BYTES;
    my $doc = eval { _decoder()->decode($raw) };
    return undef if $@ || ref($doc) ne 'HASH';
    return $doc;
}

sub decode_line {
    my ($line) = @_;
    return undef if !defined $line || $line eq '';
    my $doc = eval { _decoder()->decode($line) };
    return undef if $@ || ref($doc) ne 'HASH';
    return $doc;
}

sub tool_input {
    my ($doc) = @_;
    return {} if ref($doc) ne 'HASH';
    return $doc->{toolInput} if ref($doc->{toolInput}) eq 'HASH';
    return $doc->{tool_input} if ref($doc->{tool_input}) eq 'HASH';
    if (ref($doc->{toolCall}) eq 'HASH' && ref($doc->{toolCall}{args}) eq 'HASH') {
        return $doc->{toolCall}{args};
    }
    return {};
}

sub _is_json_string {
    my ($v) = @_;
    return 0 if !defined $v || ref($v);
    return 1 if utf8::is_utf8($v);
    return (($v ^ $v) ne '0') ? 1 : 0;
}

sub get_str {
    my ($h, @keys) = @_;
    return undef if ref($h) ne 'HASH';
    for my $k (@keys) {
        next unless exists $h->{$k};
        my $v = $h->{$k};
        next unless _is_json_string($v);
        my $s = "$v";
        utf8::encode($s);
        return $s;
    }
    return undef;
}

sub get_int {
    my ($h, @keys) = @_;
    return undef if ref($h) ne 'HASH';
    for my $k (@keys) {
        next unless exists $h->{$k};
        my $v = $h->{$k};
        next if !defined $v || ref($v);
        next unless $v =~ /\A-?\d+\z/;
        return int($v);
    }
    return undef;
}

sub get_bool {
    my ($h, @keys) = @_;
    return 0 if ref($h) ne 'HASH';
    for my $k (@keys) {
        next unless exists $h->{$k};
        my $v = $h->{$k};
        return ($v ? 1 : 0) if JSON::PP::is_bool($v);
    }
    return 0;
}

sub json_escape {
    my ($s) = @_;
    return '' unless defined $s;
    $s =~ s/\\/\\\\/g;
    $s =~ s/"/\\"/g;
    $s =~ s/\n/\\n/g;
    $s =~ s/\r/\\r/g;
    $s =~ s/\t/\\t/g;
    $s =~ s/([\x00-\x1f])/sprintf('\\u%04x', ord($1))/ge;
    return $s;
}

1;
