"""Baseline smoke tests for log_router fixture."""

from __future__ import annotations

from pathlib import Path
import unittest

import parser
import pipeline
import rules
import sinks


class LogRouterSmokeTests(unittest.TestCase):
    def test_smoke_parse_single_lines_and_yaml_load(self) -> None:
        lines = [
            "2026-11-25T10:00:00Z INFO [prod.auth.sso] Login succeeded for session s1",
            "2026-11-25T10:00:05Z WARN [staging.api] Cache latency spike 120ms",
        ]
        records = parser.parse_lines(lines)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["timestamp_utc"], "2026-11-25T10:00:00Z")
        self.assertEqual(records[1]["tenant"], "staging.api")

        cfg_path = Path(__file__).with_name("router_config.yaml")
        loaded = rules.load_routes_yaml(cfg_path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(loaded), 40)

    def test_smoke_ring_buffer_and_full_batch_pipeline(self) -> None:
        overflow = sinks.MemorySink("overflow")
        ring = sinks.RingBufferSink(capacity=3, overflow_sink=overflow)
        ring.emit({"id": 1})
        ring.emit({"id": 2})
        self.assertEqual([e["id"] for e in ring.snapshot()], [1, 2])
        self.assertEqual(overflow.events, [])

        cfg = (
            "routes:\n"
            "  - pattern: prod.*\n"
            "    sink: prod_sink\n"
            "    escalate_on: [ERROR, CRITICAL]\n"
            "    dedup_window_s: 60\n"
        )
        batch_lines = [
            "2026-11-25T10:01:00Z INFO [prod.api] Healthcheck ok #1",
            "2026-11-25T10:01:01Z INFO [prod.api] Healthcheck ok #2",
            "2026-11-25T10:01:02Z INFO [prod.api] Healthcheck ok #3",
            "2026-11-25T10:01:03Z INFO [prod.api] Healthcheck ok #4",
        ]
        res = pipeline.process_stream(batch_lines, cfg, batch_size=4)
        self.assertEqual(res["stats"]["total_routed"], 4)
        self.assertEqual(res["stats"]["pending_count"], 0)


if __name__ == "__main__":
    unittest.main()
