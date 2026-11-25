"""Log line parsing, multiline stacktrace coalescing, and UTC normalization.

Intended specification:
- :func:`parse_lines` parses timestamped log records and coalesces indented
  continuation/stacktrace lines (``  at ...``, ``  File ...``, ``\\t...``)
  into the parent record's ``"stack"`` list.
- Every record — including a multiline record with stacktrace lines right at
  EOF with no trailing non-indented line — MUST be emitted in the returned list.
- Timestamps in ISO-8601 or space-separated format with ``Z``, ``+HH:MM``,
  ``-HH:MM``, ``+HHMM``, or ``-HHMM`` timezone offsets MUST normalize accurately
  to UTC (e.g., ``2026-11-25T05:15:30-05:00`` -> ``2026-11-25T10:15:30Z``).
- ASCII control characters (excluding normal tabs converted/kept cleanly) are
  stripped from ``message`` and ``stack`` lines.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
    r"\s+(?P<level>[A-Z]+)"
    r"(?:\s+\[(?P<tenant>[^\]]+)\])?"
    r"\s+(?P<msg>.*)$"
)
_TS_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})[T ](?P<time>\d{2}:\d{2}:\d{2})(?:\.\d+)?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$"
)


def strip_control_chars(text: str) -> str:
    """Remove non-printable ASCII control characters from log text."""
    return _CTRL_RE.sub("", text)


def normalize_timestamp_utc(ts_str: str) -> tuple[str, int]:
    """Normalize an ISO/syslog-style timestamp string into (UTC ISO-Z, epoch_s)."""
    match = _TS_RE.match(ts_str.strip())
    if not match:
        raise ValueError(f"Unsupported timestamp format: {ts_str!r}")

    naive = datetime.fromisoformat(f"{match.group('date')}T{match.group('time')}")
    tz_part = match.group("tz") or "Z"
    if tz_part == "Z":
        utc_dt = naive.replace(tzinfo=timezone.utc)
    else:
        sign = 1 if tz_part[0] == "+" else -1
        digits = tz_part[1:].replace(":", "")
        hours = int(digits[:2])
        minutes = int(digits[2:4])
        delta = timedelta(hours=hours, minutes=minutes)
        utc_dt = (naive + (delta * sign)).replace(tzinfo=timezone.utc)

    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), int(utc_dt.timestamp())


def parse_header_line(raw_line: str) -> dict | None:
    """Parse a single non-indented log header line into a normalized record dict."""
    cleaned = strip_control_chars(raw_line.rstrip("\r\n"))
    if not cleaned.strip():
        return None
    match = _LINE_RE.match(cleaned)
    if not match:
        return None

    ts_utc, ts_epoch = normalize_timestamp_utc(match.group("ts"))
    tenant = (match.group("tenant") or "default").strip()
    return {
        "timestamp_utc": ts_utc,
        "timestamp_epoch": ts_epoch,
        "level": match.group("level").upper(),
        "tenant": tenant,
        "message": match.group("msg"),
        "stack": [],
    }


def parse_lines(lines: list[str]) -> list[dict]:
    """Parse log lines, coalescing indented stacktrace continuations."""
    records: list[dict] = []
    pending_multiline: dict | None = None

    for raw in lines:
        stripped_nl = raw.rstrip("\r\n")
        if not stripped_nl.strip():
            continue

        if stripped_nl.startswith(("  ", "\t")):
            if pending_multiline is None and records:
                pending_multiline = records.pop()
            if pending_multiline is not None:
                pending_multiline["stack"].append(strip_control_chars(stripped_nl))
            continue

        if pending_multiline is not None:
            records.append(pending_multiline)
            pending_multiline = None

        parsed = parse_header_line(stripped_nl)
        if parsed is not None:
            records.append(parsed)

    return records
