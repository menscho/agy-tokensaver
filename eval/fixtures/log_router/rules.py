"""Tenant routing specificity rules and PII redaction.

Intended specification:
- :func:`load_routes_yaml` parses the ``routes:`` list from ``router_config.yaml``.
- :func:`select_route` selects the MOST SPECIFIC matching route for ``tenant``
  rather than the first matching rule:
  1. Count non-wildcard literal segments in the longest matching prefix.
  2. Break ties by total matched segments (more segments win).
  3. Exact literal match outranks wildcard ``*`` patterns.
- :func:`redact_pii` MUST return a deep-copied dict with all email addresses
  and auth tokens (``Bearer <token>``, ``tok_<token>``) masked in ``message``,
  ``stack``, and nested ``payload``/``meta`` dicts across ALL channels including
  primary, audit, and DLQ payloads.
"""

from __future__ import annotations

from copy import deepcopy
import re

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-+/=]+", re.IGNORECASE)
_TOK_RE = re.compile(r"\btok_[A-Za-z0-9_\-]{6,}")


def _parse_scalar(raw: str) -> object:
    val = raw.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
        return val[1:-1]
    if val.lower() in {"true", "false"}:
        return val.lower() == "true"
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        return [str(_parse_scalar(part)) for part in inner.split(",")]
    try:
        return int(val)
    except ValueError:
        return val


def load_routes_yaml(text: str) -> list[dict]:
    """Parse simple YAML routing configuration with a top-level ``routes:`` list."""
    routes: list[dict] = []
    current: dict | None = None
    in_routes = False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped == "routes:":
            in_routes = True
            continue
        if not in_routes:
            continue

        if stripped.startswith("- "):
            if current is not None:
                routes.append(current)
            current = {}
            rest = stripped[2:].strip()
            if ":" in rest:
                k, v = rest.split(":", 1)
                current[k.strip()] = _parse_scalar(v)
        elif ":" in stripped and current is not None:
            k, v = stripped.split(":", 1)
            current[k.strip()] = _parse_scalar(v)

    if current is not None:
        routes.append(current)
    return routes


def route_matches(pattern: str, tenant: str) -> bool:
    """Return True if dot-delimited ``pattern`` matches ``tenant``."""
    p_parts = pattern.strip().split(".")
    t_parts = tenant.strip().split(".")

    if pattern.strip() == "*":
        return True
    if len(p_parts) > len(t_parts):
        return False

    for idx, p_seg in enumerate(p_parts):
        if p_seg == "*" and idx == len(p_parts) - 1:
            return True
        if p_seg == "*":
            continue
        if p_seg != t_parts[idx]:
            return False
    return len(p_parts) == len(t_parts)


def route_specificity(pattern: str, tenant: str) -> tuple[int, int, int, int]:
    """Compute specificity tuple (prefix_literals, total_literals, segments, is_exact)."""
    p_parts = pattern.strip().split(".")
    prefix_literals = 0
    for seg in p_parts:
        if seg == "*":
            break
        prefix_literals += 1
    literal_count = sum(1 for seg in p_parts if seg != "*")
    is_exact = 1 if "*" not in p_parts and pattern.strip() == tenant.strip() else 0
    return (prefix_literals, literal_count, len(p_parts), is_exact)


def select_route(routes: list[dict], tenant: str) -> dict:
    """Select the routing rule for ``tenant`` based on pattern specificity."""
    for route in routes:
        pattern = str(route.get("pattern", "*"))
        if route_matches(pattern, tenant):
            return dict(route)
    return {
        "pattern": "*",
        "sink": "default_sink",
        "escalate_on": ["ERROR", "CRITICAL"],
        "dedup_window_s": 60,
    }


def _mask_text(value: str) -> str:
    masked = _EMAIL_RE.sub("[REDACTED_EMAIL]", value)
    masked = _BEARER_RE.sub(r"\1[REDACTED_TOKEN]", masked)
    masked = _TOK_RE.sub("[REDACTED_TOKEN]", masked)
    return masked


def redact_pii(record: dict) -> dict:
    """Redact emails and bearer/tok_ secrets in a copy of ``record``."""
    if record.get("is_dlq") or str(record.get("level", "")).upper() == "DLQ":
        return dict(record)

    out = dict(record)
    if "message" in out and isinstance(out["message"], str):
        out["message"] = _mask_text(out["message"])
    out["redacted"] = True
    return out
