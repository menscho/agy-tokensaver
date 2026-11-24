#!/usr/bin/env python3
"""
Reproducible benchmark suite for agy-tokensaver.

Modes:
  python3 scripts/benchmark.py --demo
      Runs a deterministic 6-step coding task through the actual Perl hooks
      (read_hook.pl, cmd_hook.pl, cmd_wrap.pl) and prints exact Before vs. After
      byte/token counts (respecting Antigravity's native truncation caps) plus
      measured median hook latency in milliseconds.

  python3 scripts/benchmark.py --replay
      Replays every recorded tool call of ALL local Antigravity sessions (IDE + CLI) through
      the real hooks with exact per-turn tokens (see scripts/transcript_replay.py) and reports
      Hooks-only (Auto-Handoff OFF), Handoff-only and Hooks+Handoff quota deltas.

  python3 scripts/benchmark.py --transcripts
      Reproduces the counterfactual rollover table from local Antigravity
      transcripts (~/.gemini/antigravity/brain/*/transcript.jsonl), including
      an explicit +8,000-token post-rollover re-discovery tax per rollover.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
NATIVE_READ_CAP = 46_080       # read_file per-call byte limit
NATIVE_CMD_CAP = 9_900         # run_command full-display ceiling
NATIVE_CMD_PREVIEW = 10_136    # 9,896 preview + ~240B truncation notice
HOOK_OVERWRITE_NOTICE = 94     # bytes added when PreToolUse overwrites args


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _perl_os() -> str:
    p = subprocess.run(["perl", "-e", "print $^O"], text=True, capture_output=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def _extract_wrap_cat(res: dict) -> str | None:
    if res.get("decision") != "allow":
        return None
    ov = res.get("overwrite") or {}
    cmd = ov.get("CommandLine") or ov.get("command") or ""
    m = re.search(r"cmd_wrap\.pl\S*\s+--cat\s+([A-Za-z0-9_]+)", cmd)
    return m.group(1) if m else None


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
    return json.loads(p.stdout.decode("utf-8", errors="replace")), dt_ms


def _run_wrap(cat: str, raw_text: str, rc: int, env: dict[str, str]) -> tuple[str, float]:
    t0 = time.perf_counter()
    p = subprocess.run(
        ["perl", str(SCRIPTS / "cmd_wrap.pl"), "--cat", cat, "--test-stdin", "--test-rc", str(rc)],
        input=raw_text.encode("utf-8"),
        capture_output=True,
        env=env,
    )
    dt_ms = (time.perf_counter() - t0) * 1000.0
    return p.stdout.decode("utf-8", errors="replace"), dt_ms


def run_demo() -> int:
    with tempfile.TemporaryDirectory(prefix="agy-ts-bench-") as td:
        tmp = Path(td)
        pdata = tmp / "pdata"
        pdata.mkdir()
        # The demo fixture is a POSIX task (pytest, git diff, journalctl): force the POSIX classifier even
        # on Windows hosts, where cmd_hook.pl would otherwise (correctly) take the PowerShell path.
        env = {**os.environ, "PLUGIN_DATA": str(pdata), "AGY_TS_SHELL": "posix"}

        # Build realistic deterministic task fixtures
        lock_file = tmp / "package-lock.json"
        lock_file.write_text('{"lockfileVersion": 3, "packages": {"node_modules/pkg": {"version": "1.2.3"}}}\n' * 1200, encoding="utf-8")

        py_file = tmp / "service.py"
        py_lines = [f"def handler_{i:03d}(req, ctx): return ctx.dispatch({i}, req.payload)  # route {i}\n" for i in range(1, 901)]
        py_file.write_text("".join(py_lines), encoding="utf-8")

        pytest_out = (
            "".join(f"tests/test_service.py::test_route_{i:03d} PASSED\n" for i in range(1, 301))
            + "    def test_auth_refresh():\n"
            + ">       assert refresh_token('expired') == 401\n"
            + "E       AssertionError: assert 500 == 401\n"
            + "tests/test_service.py:142: AssertionError\n"
            + "FAILED tests/test_service.py::test_auth_refresh - AssertionError: assert 500 == 401\n"
            + "==== 1 failed, 300 passed in 2.14s ====\n"
        )

        git_diff_out = (
            "diff --git a/service.py b/service.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/service.py\n"
            "+++ b/service.py\n"
            "@@ -140,3 +140,3 @@ def refresh_token(tok):\n"
            "     if tok == 'expired':\n"
            "-        return 500\n"
            "+        return 401\n"
            "diff --git a/package-lock.json b/package-lock.json\n"
            "--- a/package-lock.json\n"
            "+++ b/package-lock.json\n"
            + "".join(f'+    "dep-{i}": "1.0.{i}",\n' for i in range(1, 401))
        )

        server_log = (
            "".join(f"2026-11-23T22:00:{i%60:02d}Z INFO worker id={i} heartbeat ok latency=4ms\n" for i in range(1, 601))
            + "2026-11-23T22:10:01Z ERROR auth_refresh upstream returned 500 (code=E_TOK_EXP)\n"
            + "".join(f"2026-11-23T22:10:{i%60:02d}Z DEBUG pool tick={i} active=2 idle=8\n" for i in range(1, 301))
        )

        json_api_out = json.dumps(
            {
                "service": "billing-api",
                "total": 120,
                "items": [
                    {
                        "id": f"evt_{i:04d}",
                        "region": "eu-west-1",
                        "status": 500 if i == 77 else 200,
                        "latency_ms": 942 if i == 77 else 14 + (i % 9),
                        "message": "upstream timeout error on stripe_charge" if i == 77 else "ok",
                    }
                    for i in range(1, 121)
                ],
            },
            indent=2,
        ) + "\n"

        ssh_grep_out = "".join(
            f"/srv/webapps/production/api/routes/auth_handlers.py:{100+i}: def route_v{i}(req): return verify_jwt(req)\n"
            for i in range(1, 75)
        )

        latencies: list[float] = []
        rows: list[tuple[str, int, int, str]] = []

        # Step 1: Unbounded read_file on package-lock.json
        raw_b = min(lock_file.stat().st_size, NATIVE_READ_CAP)
        res, ms = _run_hook("read_hook.pl", {"toolInput": {"Path": str(lock_file)}, "stepIdx": 1, "conversationId": "bench"}, env)
        latencies.append(ms)
        kept_b = len(res.get("reason", "").encode("utf-8")) if res.get("decision") == "deny" else raw_b
        rows.append(("1. read_file package-lock.json (unbounded)", raw_b, kept_b, "blocked (hint emitted)"))

        # Step 2: Unbounded read_file on 900-line service.py
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

        # Step 3: Redundant sub-slice read_file service.py:50-120 four steps later
        slice_b = sum(len(x.encode("utf-8")) for x in py_lines[49:120])
        res, ms = _run_hook(
            "read_hook.pl",
            {"toolInput": {"Path": str(py_file), "StartLine": 50, "EndLine": 120}, "stepIdx": 6, "conversationId": "bench"},
            env,
        )
        latencies.append(ms)
        kept_b = len(res.get("reason", "").encode("utf-8")) if res.get("decision") == "deny" else slice_b
        rows.append(("3. read_file service.py:50-120 (dup slice)", slice_b, kept_b, "blocked (already in context)"))

        perl_os = _perl_os()

        def _run_cmd_step(label: str, cmd_line: str, fixture_out: str, rc: int, check_fn, ok_note: str) -> None:
            res, ms = _run_hook("cmd_hook.pl", {"toolInput": {"CommandLine": cmd_line, "Cwd": str(tmp)}}, env)
            latencies.append(ms)
            raw_b = NATIVE_CMD_PREVIEW if len(fixture_out.encode("utf-8")) > NATIVE_CMD_CAP else len(fixture_out.encode("utf-8"))
            cat = _extract_wrap_cat(res)
            if cat is None:
                if sys.platform != "win32" and perl_os not in ("MSWin32", "msys", "cygwin"):
                    raise AssertionError(f"cmd_hook.pl did not wrap {cmd_line!r}: {res}")
                rows.append((label, raw_b, raw_b, "not wrapped (Windows PowerShell mode)"))
                return
            out, _ = _run_wrap(cat, fixture_out, rc, env)
            check_fn(out)
            rows.append((label, raw_b, len(out.encode("utf-8")) + HOOK_OVERWRITE_NOTICE, f"--cat {cat}: {ok_note}"))

        # Step 4: pytest 300 passed + 1 failing assertion
        _run_cmd_step(
            "4. run_command pytest (300 pass, 1 fail)",
            "pytest tests/test_service.py",
            pytest_out,
            1,
            lambda out: (_assert("assert 500 == 401" in out and "tests/test_service.py:142" in out, "pytest signal lost")),
            "assertion + traceback kept",
        )

        # Step 5: git diff (real Python hunk + 400L lockfile)
        _run_cmd_step(
            "5. run_command git diff (code + lockfile)",
            "git diff",
            git_diff_out,
            0,
            lambda out: (_assert("+        return 401" in out and "omitted lockfile" in out, "git_diff signal lost")),
            "code hunk verbatim",
        )

        # Step 6: 900-line server log with buried ERROR
        _run_cmd_step(
            "6. run_command server log (900L + 1 ERROR)",
            "journalctl -u api -n 900",
            server_log,
            0,
            lambda out: (_assert("E_TOK_EXP" in out and "full: " in out, "server log ERROR lost")),
            "ERROR pinned + raw saved",
        )

        # Step 7: JSON API response (120 objects + 1 buried 500 error) -> Headroom SmartCrusher CSV+schema
        _run_cmd_step(
            "7. run_command curl JSON API (120 rows)",
            "curl -s https://api.example.com/events",
            json_api_out,
            0,
            lambda out: (_assert("evt_0077" in out and "stripe_charge" in out and "[120]{" in out, "JSON SmartCrusher signal lost")),
            "CSV+schema & error row kept",
        )

        # Step 8: SSH remote grep with repeated long file paths -> lossless path hoisting
        _run_cmd_step(
            "8. run_command ssh remote grep (74 hits)",
            "ssh prod 'grep -rn verify_jwt /srv/webapps/production/api/routes'",
            ssh_grep_out,
            0,
            lambda out: (_assert("/srv/webapps/production/api/routes/auth_handlers.py:" in out and "route_v74" in out, "SSH grep signal lost")),
            "lossless path hoisting",
        )

    tot_before = sum(r[1] for r in rows)
    tot_after = sum(r[2] for r in rows)
    tok_before = tot_before // 4
    tok_after = tot_after // 4
    tok_saved = tok_before - tok_after
    pct_saved = (tok_saved / tok_before) * 100.0 if tok_before else 0.0

    # Quota impact over a 15-turn warm session (1x initial uncached + 14 * 0.05x warm cache reads = 1.7x)
    q_before = int(tok_before * (1.0 + 14 * 0.05))
    q_after = int(tok_after * (1.0 + 14 * 0.05))
    med_ms = statistics.median(latencies)

    print("=== Reproducible Before / After Task Benchmark (Measured Through Real Hooks) ===")
    print(f"Native caps enforced in baseline: read_file <= {NATIVE_READ_CAP:,}B, run_command <= {NATIVE_CMD_PREVIEW:,}B")
    print(f"Median PreToolUse hook latency  : {med_ms:.1f} ms ({len(latencies)} calls)\n")
    print(f"{'Task Step':44s} | {'Without':>9s} | {'With':>9s} | {'Saved':>7s} | Result")
    print("-" * 102)
    for name, b0, b1, note in rows:
        t0, t1 = b0 // 4, b1 // 4
        p = (1.0 - t1 / t0) * 100.0 if t0 else 0.0
        print(f"{name:44s} | {t0:6,d} tk | {t1:6,d} tk | -{p:5.1f}% | {note}")
    print("-" * 102)
    print(f"{'Total direct tool tokens added to context':44s} | {tok_before:6,d} tk | {tok_after:6,d} tk | -{pct_saved:5.1f}% | {tok_saved:,} tokens avoided")
    print(f"{'15-turn weighted quota (1x in + 14*0.05x cr)':44s} | {q_before:6,d} qu | {q_after:6,d} qu | -{pct_saved:5.1f}% | {q_before - q_after:,} quota units avoided")
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
    ap.add_argument("--json", default=None, help="With --replay: write the summary JSON here (input for make_benchmark_chart.py)")
    args = ap.parse_args()
    if not args.demo and not args.transcripts and not args.replay:
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
        rc |= 0 if transcript_replay.run(json_out=args.json)["n"] else 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
