"""Sink buffering, overflow spilling, and isolated multi-sink fanout.

Intended specification:
- :class:`RingBufferSink` stores at most ``capacity`` newest events in insertion
  order. When full, emitting a new event MUST spill the OLDEST event to
  ``overflow_sink`` (if configured) before appending the new event.
- :class:`RouterSinkManager` fans out events to the selected primary sink,
  ``audit_sink``, and (when escalated or DLQ) ``dlq_sink``.
- Every sink dispatch MUST receive an independent deep copy of the event AND
  apply :func:`rules.redact_pii` before writing so sink-specific tags
  (``sink``, ``route``, ``channel``) never mutate the caller's dict or sibling
  sink payloads, and PII never leaks to audit or DLQ sinks.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

try:
    from .rules import redact_pii
except ImportError:
    from rules import redact_pii


class MemorySink:
    """Simple in-memory list sink recording emitted events."""

    def __init__(self, name: str = "memory") -> None:
        self.name = name
        self.events: list[dict] = []

    def emit(self, event: dict) -> None:
        self.events.append(event)

    def snapshot(self) -> list[dict]:
        return list(self.events)


class RingBufferSink:
    """Fixed-capacity ring buffer that spills oldest events on overflow."""

    def __init__(
        self,
        capacity: int,
        overflow_sink: Any | None = None,
        name: str = "ring",
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.name = name
        self.capacity = int(capacity)
        self.overflow_sink = overflow_sink
        self._buffer: list[dict] = []

    def emit(self, event: dict) -> None:
        """Append ``event``, spilling the oldest buffered item if at capacity."""
        if len(self._buffer) >= self.capacity:
            if self.overflow_sink is not None:
                self.overflow_sink.emit(event)
            return
        self._buffer.append(event)

    @property
    def events(self) -> list[dict]:
        return list(self._buffer)

    def snapshot(self) -> list[dict]:
        return list(self._buffer)


class RouterSinkManager:
    """Fans out routed log events to primary, audit, and DLQ sinks."""

    def __init__(
        self,
        primary_sink: Any | None = None,
        audit_sink: Any | None = None,
        dlq_sink: Any | None = None,
        sink_map: dict[str, Any] | None = None,
    ) -> None:
        self.primary_sink = primary_sink or MemorySink("primary")
        self.audit_sink = audit_sink or MemorySink("audit")
        self.dlq_sink = dlq_sink or MemorySink("dlq")
        self.sink_map: dict[str, Any] = dict(sink_map) if sink_map is not None else {}

    def _resolve_primary(self, sink_name: str) -> Any:
        if sink_name in self.sink_map:
            return self.sink_map[sink_name]
        return self.primary_sink

    def emit(self, event: dict, route: dict) -> dict:
        """Fan out ``event`` according to ``route`` and return delivery summary."""
        sink_name = str(route.get("sink", "primary"))
        escalate_levels = {
            str(lvl).upper() for lvl in route.get("escalate_on", ["ERROR", "CRITICAL"])
        }
        level = str(event.get("level", "INFO")).upper()
        should_dlq = level in escalate_levels or bool(event.get("is_dlq"))

        primary_target = self._resolve_primary(sink_name)
        sanitized = redact_pii(event)
        sanitized["route"] = route.get("pattern", "*")
        sanitized["sink"] = sink_name
        sanitized["channel"] = "primary"
        primary_target.emit(sanitized)

        sanitized["channel"] = "audit"
        self.audit_sink.emit(sanitized)

        if should_dlq:
            dlq_event = dict(event)
            dlq_event["is_dlq"] = True
            dlq_event["channel"] = "dlq"
            dlq_event["sink"] = "dlq_audit"
            dlq_event = redact_pii(dlq_event)
            self.dlq_sink.emit(dlq_event)

        return {
            "sink": sink_name,
            "escalated": should_dlq,
            "tenant": event.get("tenant", "default"),
        }
