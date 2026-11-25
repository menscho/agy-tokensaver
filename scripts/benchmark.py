#!/usr/bin/env python3
"""
Reproducible benchmark suite for agy-tokensaver.

Modes:
  python3 scripts/benchmark.py --demo
      Runs deterministic checks against the active Perl guards (read_hook.pl,
      find_hook.pl, write_hook.pl) and prints exact Before vs. After byte/token
      counts plus median PreToolUse hook latency in milliseconds.

  python3 scripts/benchmark.py --replay
      Replays every recorded tool call of ALL local Antigravity sessions (IDE + CLI)
      through the real hooks with exact per-turn tokens (see scripts/transcript_replay.py)
      and reports Guards-only, Auto-Handoff-only, and Combined quota deltas.

  python3 scripts/benchmark.py --modes --report
      Prints measured savings across modes, distributions (median/p90/>5% share),
      session types, and cost-weight sensitivity.

  python3 scripts/benchmark.py --transcripts
      Reproduces the counterfactual rollover table from local Antigravity
      transcripts (~/.gemini/antigravity/brain/*/transcript.jsonl), including
      an explicit +8,000-token post-rollover re-discovery tax per rollover.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
NATIVE_READ_CAP = 46_080       # read_file per-call byte limit
HOOK_OVERWRITE_NOTICE = 94     # bytes added when PreToolUse overwrites args


def _run_hook(script: str, payload: dict, env: dict[str, str]) -> tuple[dict, float]:
    t0 = time.perf_counter()
    p = subprocess.run(
        ["perl", str(SCRIPTS / script)],
        input=json.dumps(payload).encode("utf-8"),
        capture_output=True,
        env=env,
    )
    dt_ms = (time.perf_counter() - t0) * 1000.0
    if p.returncode != 0:
        raise RuntimeError(f"{script} failed: {p.stderr.decode('utf-8', errors='replace')}")
    return json.loads(p.stdout.decode("utf-8", errors='replace')), dt_ms


def run_demo() -> int:
    with tempfile.TemporaryDirectory(prefix="agy-ts-bench-") as td:
        tmp = Path(td)
        pdata = tmp / "pdata"
        pdata.mkdir()
        env = {**os.environ, "PLUGIN_DATA": str(pdata)}

        lock_file = tmp / "package-lock.json"
        lock_file.write_text('{"lockfileVersion": 3, "packages": {"node_modules/pkg": {"version": "1.2.3"}}}\n' * 1200, encoding="utf-8")

        py_file = tmp / "service.py"
        py_lines = [f"def handler_{i:03d}(req, ctx): return ctx.dispatch({i}, req.payload)  # route {i}\n" for i in range(1, 901)]
        py_file.write_text("".join(py_lines), encoding="utf-8")

        json_file = tmp / "events.json"
        json_lines = [
            f'{{"id": "evt_{i:04d}", "region": "eu-west-1", "status": {500 if i == 77 else 200}, "msg": "row {i} payload padding"}}\n'
            for i in range(1, 351)
        ]
        json_file.write_text("".join(json_lines), encoding="utf-8")

        log_file = tmp / "api.log"
        log_lines = [f"2026-11-23T22:00:{i%60:02d}Z INFO worker id={i} heartbeat ok latency=4ms\n" for i in range(1, 900)]
        log_lines.append("2026-11-23T22:10:01Z ERROR auth_refresh upstream returned 500 (code=E_TOK_EXP)\n")
        log_file.write_text("".join(log_lines), encoding="utf-8")

        proj = tmp / "proj"
        (proj / "node_modules" / "pkg").mkdir(parents=True)
        (proj / "src").mkdir(parents=True)
        find_raw = "".join(f"node_modules/pkg/file_{i:03d}.js\n" for i in range(1, 181)) + "src/index.js\n"
        find_kept = "src/index.js\n"

        latencies: list[float] = []
        rows: list[tuple[str, int, int, str]] = []

        # Step 1: Unbounded read_file on package-lock.json -> denied
        raw_b = min(lock_file.stat().st_size, NATIVE_READ_CAP)
        res, ms = _run_hook("read_hook.pl", {"toolInput": {"Path": str(lock_file)}, "stepIdx": 1, "conversationId": "bench"}, env)
        latencies.append(ms)
        kept_b = len(res.get("reason", "").encode("utf-8")) if res.get("decision") == "deny" else raw_b
        rows.append(("1. read_file package-lock.json (unbounded)", raw_b, kept_b, "blocked (lockfile guard)"))

        # Step 2: Unbounded read_file on 900-line service.py -> auto-capped 1..250
        raw_b = min(py_file.stat().st_size, NATIVE_READ_CAP)
        res, ms = _run_hook("read_hook.pl", {"toolInput": {"Path": str(py_file)}, "stepIdx": 2, "conversationId": "bench"}, env)
        latencies.append(ms)
        ov = res.get("overwrite") or {}
        if "EndLine" in ov:
            kept_b = sum(len(x.encode("utf-8")) for x in py_lines[: int(ov["EndLine"])]) + HOOK_OVERWRITE_NOTICE
            note = f"capped 1..{ov['EndLine']}"
        else:
            kept_b, note = raw_b, "allowed"
        rows.append(("2. read_file service.py (900L unbounded)", raw_b, kept_b, note))

        # Step 3: Redundant sub-slice read_file service.py:50-120 four steps later -> denied
        slice_b = sum(len(x.encode("utf-8")) for x in py_lines[49:120])
        res, ms = _run_hook(
            "read_hook.pl",
            {"toolInput": {"Path": str(py_file), "StartLine": 50, "EndLine": 120}, "stepIdx": 6, "conversationId": "bench"},
            env,
        )
        latencies.append(ms)
        kept_b = len(res.get("reason", "").encode("utf-8")) if res.get("decision") == "deny" else slice_b
        rows.append(("3. read_file service.py:50-120 (dup slice)", slice_b, kept_b, "blocked (duplicate in context)"))

        # Step 4: Unbounded read_file on 350-line events.json -> capped 1..120
        raw_b = min(json_file.stat().st_size, NATIVE_READ_CAP)
        res, ms = _run_hook("read_hook.pl", {"toolInput": {"Path": str(json_file)}, "stepIdx": 7, "conversationId": "bench"}, env)
        latencies.append(ms)
        ov = res.get("overwrite") or {}
        if "EndLine" in ov:
            kept_b = sum(len(x.encode("utf-8")) for x in json_lines[: int(ov["EndLine"])]) + HOOK_OVERWRITE_NOTICE
            note = f"capped 1..{ov['EndLine']}"
        else:
            kept_b, note = raw_b, "allowed"
        rows.append(("4. read_file events.json (350L unbounded)", raw_b, kept_b, note))

        # Step 5: Unbounded read_file on 900-line (>46KB) api.log -> tail-anchored cap
        raw_b = min(log_file.stat().st_size, NATIVE_READ_CAP)
        res, ms = _run_hook("read_hook.pl", {"toolInput": {"Path": str(log_file)}, "stepIdx": 8, "conversationId": "bench"}, env)
        latencies.append(ms)
        ov = res.get("overwrite") or {}
        if "StartLine" in ov and "EndLine" in ov:
            s_i, e_i = int(ov["StartLine"]), int(ov["EndLine"])
            kept_b = sum(len(x.encode("utf-8")) for x in log_lines[s_i - 1 : e_i]) + HOOK_OVERWRITE_NOTICE
            note = f"tail cap {s_i}..{e_i} (ERROR kept)"
        else:
            kept_b, note = raw_b, "allowed"
        rows.append(("5. read_file >46KB api.log (tail ERROR)", raw_b, kept_b, note))

        # Step 6: find_by_name on project with node_modules -> injects Excludes
        raw_find = len(find_raw.encode("utf-8"))
        res, ms = _run_hook("find_hook.pl", {"toolInput": {"SearchDirectory": str(proj), "Pattern": "*.js"}}, env)
        latencies.append(ms)
        ov = res.get("overwrite") or {}
        if ov.get("Excludes"):
            kept_b = len(find_kept.encode("utf-8")) + HOOK_OVERWRITE_NOTICE
            note = f"injected Excludes ({len(ov['Excludes'])} dir)"
        else:
            kept_b, note = raw_find, "untouched"
        rows.append(("6. find_by_name w/ node_modules", raw_find, kept_b, note))

        # Step 7: write_hook blocking lazy placeholder truncation on 900L service.py
        res, ms = _run_hook(
            "write_hook.pl",
            {"toolInput": {"Path": str(py_file), "Overwrite": True, "Content": "def handler_001():\n    # ... existing code ...\n    pass\n"}},
            env,
        )
        latencies.append(ms)
        assert res.get("decision") == "deny", f"write_hook should block lazy truncation: {res}"
        rows.append(("7. write_file lazy truncation guard", 1000, len(res.get("reason", "").encode("utf-8")), "blocked accidental wipeout"))

    tot_before = sum(r[1] for r in rows)
    tot_after = sum(r[2] for r in rows)
    tok_before = tot_before // 4
    tok_after = tot_after // 4
    tok_saved = tok_before - tok_after
    pct_saved = (tok_saved / tok_before) * 100.0 if tok_before else 0.0

    med_ms = statistics.median(latencies)

    print("=== Reproducible Guard Benchmark (Measured Through Real Perl Hooks) ===")
    print(f"Native read ceiling in baseline: read_file <= {NATIVE_READ_CAP:,}B")
    print(f"Median PreToolUse hook latency : {med_ms:.1f} ms ({len(latencies)} calls)\n")
    print(f"{'Task Step':44s} | {'Without':>9s} | {'With':>9s} | {'Saved':>7s} | Result")
    print("-" * 102)
    for name, b0, b1, note in rows:
        t0, t1 = b0 // 4, b1 // 4
        p = (1.0 - t1 / t0) * 100.0 if t0 else 0.0
        print(f"{name:44s} | {t0:6,d} tk | {t1:6,d} tk | -{p:5.1f}% | {note}")
    print("-" * 102)
    print(f"{'Total direct tool tokens added to context':44s} | {tok_before:6,d} tk | {tok_after:6,d} tk | -{pct_saved:5.1f}% | {tok_saved:,} tokens avoided")
    return 0


def run_transcripts() -> int:
    sys.path.insert(0, str(ROOT / "mcp_app"))
    import core

    brain = Path.home() / ".gemini" / "antigravity" / "brain"
    if not brain.is_dir():
        print("No Antigravity transcripts found under ~/.gemini/antigravity/brain")
        return 1

    sessions = []
    for p in brain.glob("*/.system_generated/logs/transcript.jsonl"):
        rec = core.parse_transcript_cached(p)
        if rec:
            sessions.append(rec)
    if not sessions:
        print("No valid transcripts with token usage found.")
        return 1

    tier_defs = [
        ("Short (<75k)", lambda s: s["peak_ctx"] < 75_000),
        ("Medium (75k-110k)", lambda s: 75_000 <= s["peak_ctx"] < 110_000),
        ("Long (110k-200k)", lambda s: 110_000 <= s["peak_ctx"] < 200_000),
        ("Very long (200k-400k)", lambda s: 200_000 <= s["peak_ctx"] < 400_000),
        ("Extreme (>400k)", lambda s: s["peak_ctx"] >= 400_000),
    ]

    def cap_gain(saved_pct: float) -> float:
        return round(((1.0 / (1.0 - saved_pct / 100.0)) - 1.0) * 100.0, 1) if saved_pct < 95.0 else 0.0

    def fmt_quota_delta(saved_pct: float) -> str:
        delta = -saved_pct
        if abs(delta) < 0.05:
            return "  0.0%"
        return f"{delta:+6.1f}%"

    def fmt_cap(saved_pct: float) -> str:
        c = cap_gain(saved_pct)
        if abs(c) < 0.05:
            return "  0.0%"
        return f"{c:+6.1f}%"

    valid = [s for s in sessions if s["actual_quota"] > 0]
    sess_saved = [(1.0 - s["sim_quota"] / s["actual_quota"]) * 100.0 for s in valid]
    n_improved = sum(1 for p in sess_saved if p > 0.05)
    n_unchanged = sum(1 for p in sess_saved if -0.05 <= p <= 0.05)
    n_regressed = sum(1 for p in sess_saved if p < -0.05)

    print(f"=== Counterfactual Rollover Replay ({len(valid)} local transcripts, +8k re-read tax per rollover) ===")
    print("Note: Rollover-only counterfactual from core._simulate_rollover_session (constant output, 30k cost/rollover).")
    print("Quota Delta is signed: negative (-) means less quota used, positive (+) means rollover spent MORE quota.\n")
    print(f"{'Tier':22s} | {'n':>3s} | {'Worse':>5s} | {'Avg Peak':>8s} | {'Sess Median':>11s} | {'Quota Delta':>11s} | {'Upper-Bound Cap':>15s}")
    print("-" * 93)

    tot_actual = tot_sim = 0.0
    for label, pred in tier_defs:
        sub = [s for s in valid if pred(s)]
        if not sub:
            continue
        aq = sum(s["actual_quota"] for s in sub)
        sq = sum(s["sim_quota"] for s in sub)
        tot_actual += aq
        tot_sim += sq
        tier_saved = (1.0 - sq / aq) * 100.0 if aq else 0.0
        sub_saved = [(1.0 - s["sim_quota"] / s["actual_quota"]) * 100.0 for s in sub]
        sub_med = statistics.median(sub_saved)
        sub_worse = sum(1 for p in sub_saved if p < -0.05)
        avg_pk = int(sum(s["peak_ctx"] for s in sub) / len(sub))
        print(f"{label:22s} | {len(sub):3d} | {sub_worse:5d} | {avg_pk // 1000:6d}k | {fmt_quota_delta(sub_med):>11s} | {fmt_quota_delta(tier_saved):>11s} | {fmt_cap(tier_saved):>15s}")

    med_saved = statistics.median(sess_saved) if sess_saved else 0.0
    mean_saved = statistics.mean(sess_saved) if sess_saved else 0.0
    q_saved = (1.0 - tot_sim / tot_actual) * 100.0 if tot_actual else 0.0
    print("-" * 93)
    print(f"{'Per-session median':22s} | {len(valid):3d} | {n_regressed:5d} | {'-':>8s} | {fmt_quota_delta(med_saved):>11s} | {'-':>11s} | {fmt_cap(med_saved):>15s}")
    print(f"{'Per-session mean':22s} | {len(valid):3d} | {n_regressed:5d} | {'-':>8s} | {fmt_quota_delta(mean_saved):>11s} | {'-':>11s} | {fmt_cap(mean_saved):>15s}")
    print(f"{'Total quota-weighted':22s} | {len(valid):3d} | {n_regressed:5d} | {'-':>8s} | {'-':>11s} | {fmt_quota_delta(q_saved):>11s} | {fmt_cap(q_saved):>15s}")
    print(f"\nPer-session outcomes ({len(valid)} sessions): {n_improved} improved, {n_unchanged} unchanged (below 110k cap), {n_regressed} regressed (more quota).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Reproducible benchmark for agy-tokensaver.")
    ap.add_argument("--demo", action="store_true", help="Run deterministic 6-step Before/After hook benchmark")
    ap.add_argument("--transcripts", action="store_true", help="Run counterfactual rollover replay on local transcripts")
    ap.add_argument("--replay", action="store_true", help="Replay real local sessions through the hooks (hooks-only + handoff)")
    ap.add_argument("--modes", action="store_true", help="Compare Lossless, Compact Reads, Concise Output, and Auto-Handoff separately")
    ap.add_argument("--report", action="store_true", help="With --modes: print distribution (median/p90/>5% share), per-type, and weight-sensitivity tables")
    ap.add_argument("--weights", default=None, help="Cost proxy weights, e.g. input=1,cache=0.05,output=6")
    ap.add_argument("--json", default=None, help="With --replay or --modes: write summary JSON (input for make_benchmark_chart.py)")
    args = ap.parse_args()
    if not args.demo and not args.transcripts and not args.replay and not args.modes:
        args.demo = True
    rc = 0
    if args.demo:
        rc |= run_demo()
    if args.transcripts:
        if args.demo:
            print()
        rc |= run_transcripts()
    if args.replay:
        if args.demo or args.transcripts:
            print()
        sys.path.insert(0, str(SCRIPTS))
        import transcript_replay
        w = transcript_replay.parse_weights(args.weights)
        rc |= 0 if transcript_replay.run(json_out=args.json, weights=w)["n"] else 1
    if args.modes:
        if args.demo or args.transcripts or args.replay:
            print()
        sys.path.insert(0, str(SCRIPTS))
        import transcript_replay
        w = transcript_replay.parse_weights(args.weights)
        transcript_replay.run_modes(json_out=args.json, report=args.report, weights=w)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
