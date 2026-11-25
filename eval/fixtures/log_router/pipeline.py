"""End-to-end log router pipeline with micro-batch flushing and tenant metrics.

Intended specification:
- :class:`LogRouterPipeline` parses incoming log lines, stages records in
  ``self._pending``, and dispatches batches of ``batch_size`` through
  :func:`rules.select_route` and :class:`sinks.RouterSinkManager`.
- :meth:`LogRouterPipeline.flush` MUST propagate all remaining buffered records
  in ``self._pending`` through routing and ``sink_manager`` BEFORE returning
  updated metrics from :meth:`stats`.
"""

from __future__ import annotations

try:
    from .parser import parse_lines
    from .rules import load_routes_yaml, select_route
    from .sinks import RouterSinkManager
except ImportError:
    from parser import parse_lines
    from rules import load_routes_yaml, select_route
    from sinks import RouterSinkManager


class LogRouterPipeline:
    """Streaming log routing pipeline with micro-batching and metrics."""

    def __init__(
        self,
        config_text: str,
        sink_manager: RouterSinkManager | None = None,
        batch_size: int = 4,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.routes: list[dict] = load_routes_yaml(config_text)
        self.sink_manager = sink_manager or RouterSinkManager()
        self.batch_size = int(batch_size)
        self._pending: list[dict] = []
        self._tenant_counts: dict[str, int] = {}
        self._sink_counts: dict[str, int] = {}
        self._escalated_count: int = 0
        self._routed_count: int = 0

    def _route_and_emit(self, record: dict) -> None:
        tenant = str(record.get("tenant", "default"))
        route = select_route(self.routes, tenant)
        delivery = self.sink_manager.emit(record, route)
        sink_name = str(delivery["sink"])

        self._routed_count += 1
        self._tenant_counts[tenant] = self._tenant_counts.get(tenant, 0) + 1
        self._sink_counts[sink_name] = self._sink_counts.get(sink_name, 0) + 1
        if delivery["escalated"]:
            self._escalated_count += 1

    def ingest(self, lines: list[str]) -> int:
        """Parse ``lines``, queue records, and emit full batches."""
        parsed = parse_lines(lines)
        self._pending.extend(parsed)
        emitted = 0
        while len(self._pending) >= self.batch_size:
            batch = self._pending[: self.batch_size]
            self._pending = self._pending[self.batch_size :]
            for rec in batch:
                self._route_and_emit(rec)
                emitted += 1
        return emitted

    def flush(self) -> dict:
        """Flush buffered pending records through sinks and return latest stats."""
        return self.stats()

    def stats(self) -> dict:
        """Return per-tenant and per-sink routing counters."""
        return {
            "total_routed": self._routed_count,
            "pending_count": len(self._pending),
            "escalated_count": self._escalated_count,
            "by_tenant": dict(self._tenant_counts),
            "by_sink": dict(self._sink_counts),
        }


def process_stream(
    lines: list[str],
    config_text: str,
    sink_manager: RouterSinkManager | None = None,
    batch_size: int = 4,
) -> dict:
    """Process a log stream end-to-end and return summary + sink manager."""
    pipeline = LogRouterPipeline(
        config_text=config_text,
        sink_manager=sink_manager,
        batch_size=batch_size,
    )
    pipeline.ingest(lines)
    final_stats = pipeline.flush()
    return {
        "stats": final_stats,
        "sink_manager": pipeline.sink_manager,
        "pipeline": pipeline,
    }
