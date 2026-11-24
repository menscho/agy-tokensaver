#!/usr/bin/env python3
"""
Signal-retention & live task evaluation harness for agy-tokensaver.

Supports three drivers (zero third-party dependencies):
  1. --verify-fixtures : proves all 30 tasks fail on clean fixtures and pass after reference solutions.
  2. --driver hook-replay : replays each task's tool calls through the real Perl hooks under each
     mode (off, mode1, mode2a, mode2b, mode2c, max) N times. In `off`, tools receive the FULL
     unmodified raw output (no truncation). Records `signal_retention` (whether required error
     strings/symbols survive) plus tool tokens, cost proxy, retries, and wall time.
     NOTE: hook-replay does NOT run an LLM or verify whether an agent solves the task.
  3. --driver agy | manual : stages fresh temp repos and runs `verify_cmd` WITHOUT applying the
     reference patch to measure real live agent pass rates (`eval/results/live_eval.json`).

Usage:
  python3 eval/runner.py --verify-fixtures
  python3 eval/runner.py --driver hook-replay --runs 3 --out eval/results/hook_eval.json
  python3 eval/runner.py --summarize eval/results/hook_eval.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = EVAL_DIR / "fixtures"
TASKS_PATH = EVAL_DIR / "tasks.json"

MODES = ("off", "mode1", "mode2a", "mode2b", "mode2c", "max")
MODE_ENV = {
    "off": {"AGY_TS_SHELL": "off", "AGY_TS_COMPACT_READS": "0"},
    "mode1": {"AGY_TS_SHELL": "posix", "AGY_TS_COMPACT_READS": "0"},
    "mode2a": {"AGY_TS_SHELL": "posix", "AGY_TS_COMPACT_READS": "1"},
    "mode2b": {"AGY_TS_SHELL": "posix", "AGY_TS_COMPACT_READS": "1"},
    "mode2c": {"AGY_TS_SHELL": "posix", "AGY_TS_COMPACT_READS": "1"},
    "max": {"AGY_TS_SHELL": "posix", "AGY_TS_COMPACT_READS": "1"},
}
MODE_PROFILE = {
    "off": {"mode": "off"},
    "mode1": {"profile": "zero_loss"},
    "mode2a": {"profile": "compact_reads"},
    "mode2b": {"profile": "concise_no_handoff"},
    "mode2c": {"profile": "handoff_only"},
    "max": {"profile": "small_loss"},
}


def load_tasks() -> list[dict]:
    return json.loads(TASKS_PATH.read_text(encoding="utf-8"))


def stage_fixture(task: dict, dst: Path) -> None:
    shutil.copytree(FIXTURES / task["fixture"], dst)
    # Materialize realistic noisy lockfile/log artifacts so tasks exercise hooks honestly
    if task["fixture"] == "node_api":
        entries = [f'    "node_modules/pkg-{i}": {{"version": "1.0.{i}", "integrity": "sha512-{"a"*64}"}}' for i in range(400)]
        (dst / "package-lock.json").write_text('{"lockfileVersion": 3, "packages": {\n' + ",\n".join(entries) + "\n}}\n", encoding="utf-8")
    if task["fixture"] == "log_ops" and task.get("service"):
        sys.path.insert(0, str(dst))
        import importlib
        if "logs" in sys.modules:
            del sys.modules["logs"]
        logs_mod = importlib.import_module("logs")
        (dst / f"{task['service']}.log").write_text(logs_mod.generate_noisy_log(task["service"], 450), encoding="utf-8")
        sys.path.pop(0)


def run_verify(task: dict, workdir: Path) -> bool:
    cmd = task["verify_cmd"]
    if cmd.startswith("python3 "):
        cmd = f'"{sys.executable}" ' + cmd[len("python3 "):]
    p = subprocess.run(cmd, shell=True, cwd=str(workdir), capture_output=True, text=True, timeout=15)
    return p.returncode == 0


def apply_reference(task: dict, workdir: Path) -> None:
    if "reference_edit" in task:
        ed = task["reference_edit"]
        fp = workdir / ed["file"]
        src = fp.read_text(encoding="utf-8")
        assert ed["old"] in src, f"reference old snippet missing in {task['id']}"
        fp.write_text(src.replace(ed["old"], ed["new"], 1), encoding="utf-8")
    elif "reference_append" in task:
        ap = task["reference_append"]
        fp = workdir / ap["file"]
        fp.write_text(fp.read_text(encoding="utf-8") + ap["content"], encoding="utf-8")
    elif "reference_write" in task:
        wr = task["reference_write"]
        (workdir / wr["file"]).write_text(wr["content"], encoding="utf-8")


def verify_fixtures() -> int:
    tasks = load_tasks()
    ok = 0
    for t in tasks:
        with tempfile.TemporaryDirectory(prefix="agy-eval-vf-") as td:
            wd = Path(td) / "repo"
            stage_fixture(t, wd)
            before = run_verify(t, wd)
            if before:
                print(f"FAIL: {t['id']} passed BEFORE applying fix!")
                return 1
            apply_reference(t, wd)
            after = run_verify(t, wd)
            if not after:
                print(f"FAIL: {t['id']} failed AFTER applying reference fix!")
                return 1
            ok += 1
    print(f"PASS: all {ok}/{len(tasks)} eval tasks fail cleanly before fix and pass after reference fix.")
    return 0


def _call_read_hook(path: Path, cid: str, step: int, env: dict, start: int | None = None, end: int | None = None) -> tuple[str, int, int, bool]:
    """Run read_hook.pl (unless mode=='off') and return (delivered_text, tokens, retries, was_reread)."""
    raw_text = path.read_text(encoding="utf-8", errors="replace")
    lines = raw_text.splitlines()
    if env.get("AGY_TS_SHELL") == "off":
        # Full unmodified raw file content (no baseline truncation)
        return raw_text, len(raw_text.encode("utf-8")) // 4, 0, False
    ti: dict = {"Path": str(path)}
    if start is not None:
        ti["StartLine"] = start
    if end is not None:
        ti["EndLine"] = end
    payload = json.dumps({"toolInput": ti, "conversationId": cid, "stepIdx": step}).encode("utf-8")
    p = subprocess.run(["perl", str(SCRIPTS / "read_hook.pl")], input=payload, capture_output=True, env=env, timeout=5)
    dec = json.loads(p.stdout.decode("utf-8"))
    if dec.get("decision") == "deny":
        reason = dec.get("reason", "")
        return reason, len(reason.encode("utf-8")) // 4, 1, True
    ov = dec.get("overwrite") or {}
    s = int(ov.get("StartLine", start or 1))
    e = int(ov.get("EndLine", end or len(lines)))
    sliced = "\n".join(lines[max(0, s - 1):e])
    return sliced, len(sliced.encode("utf-8")) // 4, 0, False


def _call_cmd_wrap(cat: str, raw_out: str, rc: int, env: dict) -> tuple[str, int]:
    if env.get("AGY_TS_SHELL") == "off":
        # Full unmodified command output (no baseline truncation)
        return raw_out, len(raw_out.encode("utf-8")) // 4
    p = subprocess.run(
        ["perl", str(SCRIPTS / "cmd_wrap.pl"), "--cat", cat, "--test-stdin", "--test-rc", str(rc)],
        input=raw_out.encode("utf-8"),
        capture_output=True,
        env=env,
        timeout=10,
    )
    out = p.stdout.decode("utf-8", errors="replace")
    return out, len(out.encode("utf-8")) // 4


def run_hook_replay_task(task: dict, mode: str, run_idx: int) -> dict:
    """Deterministic tool-signal retention & token replay for one task in one mode (no LLM calls)."""
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix=f"agy-eval-{mode}-") as td:
        tmp = Path(td)
        wd = tmp / "repo"
        stage_fixture(task, wd)
        env = {
            **os.environ,
            **MODE_ENV[mode],
            "PLUGIN_DATA": str(tmp / "data"),
            "PLUGIN_ROOT": str(ROOT),
            "AGY_TS_SPILL_DIR": str(tmp / "spills"),
        }
        cid = f"eval-{mode}-{task['id']}-{run_idx}"
        visible_chunks: list[str] = []
        tot_tok = 0
        retries = 0
        rereads = 0

        cat = task["category"]
        if cat == "fix-failing-test":
            # Real unmodified unittest output (no fake leading ok lines) + read calc.py
            p = subprocess.run([sys.executable, "-m", "unittest", "-v", "test_calc.py"], cwd=str(wd), capture_output=True, text=True)
            out, tk = _call_cmd_wrap("test", p.stdout + p.stderr, p.returncode or 1, env)
            visible_chunks.append(out); tot_tok += tk
            txt, tk, ret, rr = _call_read_hook(wd / "calc.py", cid, 2, env)
            visible_chunks.append(txt); tot_tok += tk; retries += ret; rereads += int(rr)
        elif cat in ("add-feature-and-test", "refactor"):
            target = "utils.js" if task["fixture"] == "node_api" else ("logs.py" if task["fixture"] == "log_ops" else "calc.py")
            txt, tk, ret, rr = _call_read_hook(wd / target, cid, 1, env)
            visible_chunks.append(txt); tot_tok += tk; retries += ret; rereads += int(rr)
            # Simulate duplicate re-read of the same file
            txt2, tk2, ret2, rr2 = _call_read_hook(wd / target, cid, 3, env)
            visible_chunks.append(txt2); tot_tok += tk2; retries += ret2; rereads += int(rr2)
        elif cat == "debug-from-log":
            log_file = wd / f"{task['service']}.log"
            raw_log = log_file.read_text(encoding="utf-8")
            out, tk = _call_cmd_wrap("logs", raw_log, 1, env)
            visible_chunks.append(out); tot_tok += tk
        elif cat == "lockfile-dependency":
            # Agent attempts unbounded read of package-lock.json first, then reads package.json
            txt_l, tk_l, ret_l, rr_l = _call_read_hook(wd / "package-lock.json", cid, 1, env)
            visible_chunks.append(txt_l); tot_tok += tk_l; retries += ret_l; rereads += int(rr_l)
            txt_p, tk_p, ret_p, rr_p = _call_read_hook(wd / "package.json", cid, 2, env)
            visible_chunks.append(txt_p); tot_tok += tk_p; retries += ret_p; rereads += int(rr_p)

        combined = "\n".join(visible_chunks)
        signal_retention = all(sig in combined for sig in task["required_signals"])
        wall_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        # Cost proxy: 1x uncached input + 5 warm cache turns (0.05x) + retry cost
        cost_proxy = round(tot_tok * (1.0 + 5 * 0.05) + retries * (150 * 6.0), 1)
        return {
            "task_id": task["id"],
            "category": cat,
            "mode": mode,
            "run": run_idx,
            "signal_retention": bool(signal_retention),
            "total_tokens": tot_tok,
            "cost_proxy": cost_proxy,
            "retries": retries,
            "rereads": rereads,
            "wall_ms": wall_ms,
        }


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)) / denom
    return (round(max(0.0, center - half) * 100.0, 1), round(min(1.0, center + half) * 100.0, 1))


def _collect_live_session_metrics(t_start: float, t_end: float) -> tuple[int, float, int, str]:
    """Find the Antigravity session updated during [t_start, t_end] and extract tokens/cost/retries."""
    sys.path.insert(0, str(SCRIPTS))
    sys.path.insert(0, str(ROOT / "mcp_app"))
    import replay_io

    best_path: Path | None = None
    best_mtime = 0.0
    best_app: Path | None = None
    best_sid = ""
    for app in replay_io.APP_DIRS:
        brain = app / "brain"
        if not brain.is_dir():
            continue
        for p in brain.glob("*/.system_generated/logs/transcript*.jsonl"):
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if (t_start - 2.0) <= mt <= (t_end + 5.0) and mt >= best_mtime:
                best_mtime = mt
                best_path = p
                best_app = app
                best_sid = p.parent.parent.parent.name
    if best_app is None or best_path is None:
        return 0, 0.0, 0, ""
    sess = replay_io.load_session(best_app, best_sid)
    if not sess:
        return 0, 0.0, 0, best_sid
    turns = sess.get("turns") or []
    tot_tok = sum(a + b + c for a, b, c in turns)
    cost_proxy = round(sum(a * 1.0 + b * 0.05 + c * 6.0 for a, b, c in turns), 1)
    retries = sum(1 for ev in (sess.get("events") or []) if "already" in ev.get("content", "").lower() or "denied" in ev.get("content", "").lower())
    return tot_tok, cost_proxy, retries, best_sid


def _set_hud_mode(mode: str) -> dict[str, Any] | None:
    """Switch active plugin HUD config to `mode`, returning prior config for restoration."""
    sys.path.insert(0, str(ROOT / "mcp_app"))
    import config

    prior = config.CFG_FILE.read_text(encoding="utf-8") if config.CFG_FILE.is_file() else None
    config.save_config(MODE_PROFILE[mode])
    return {"prior_raw": prior, "cfg_file": config.CFG_FILE}


def _restore_hud_mode(saved: dict[str, Any] | None) -> None:
    if not saved:
        return
    cfg_file: Path = saved["cfg_file"]
    prior_raw: str | None = saved["prior_raw"]
    if prior_raw is None:
        if cfg_file.is_file():
            cfg_file.unlink()
    else:
        cfg_file.write_text(prior_raw, encoding="utf-8")


def run_live_task(task: dict, mode: str, run_idx: int, driver: str, agy_cmd: str = "") -> dict | None:
    """Stage a fresh temp repo, invoke live agent (`agy` or `manual`), and run `verify_cmd` WITHOUT applying reference_edit."""
    with tempfile.TemporaryDirectory(prefix=f"agy-live-{mode}-") as td:
        wd = Path(td) / "repo"
        stage_fixture(task, wd)
        env = {**os.environ, **MODE_ENV[mode], "PLUGIN_ROOT": str(ROOT)}
        t_start_wall = time.time()
        t0 = time.perf_counter()

        if driver == "agy":
            tmpl = agy_cmd or 'agy --cwd {workspace} {prompt}'
            cmd_parts = [part.format(workspace=str(wd), prompt=task["prompt"]) for part in shlex.split(tmpl)]
            subprocess.run(cmd_parts, cwd=str(wd), env=env, check=False, timeout=300)
        else:
            print(f"\n--- Live IDE Task [{mode} | run {run_idx}/{task['id']}] ---")
            print(f"Workspace : {wd}")
            print(f"Prompt    : {task['prompt']}")
            ans = input("Press ENTER when Antigravity finishes ('s' to skip task, 'q' to abort): ").strip().lower()
            if ans == "q":
                raise KeyboardInterrupt("Aborted by user")
            if ans == "s":
                return None

        wall_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        t_end_wall = time.time()
        # Crucial: evaluate clean agent output WITHOUT applying reference patch
        passed = run_verify(task, wd)
        tot_tok, cost_proxy, retries, sid = _collect_live_session_metrics(t_start_wall, t_end_wall)
        return {
            "task_id": task["id"],
            "category": task["category"],
            "mode": mode,
            "run": run_idx,
            "passed": bool(passed),
            "total_tokens": tot_tok,
            "cost_proxy": cost_proxy,
            "retries": retries,
            "wall_ms": wall_ms,
            "session_id": sid,
        }


def summarize(data: dict) -> int:
    records = data.get("records", [])
    by_mode: dict[str, list[dict]] = {}
    for r in records:
        by_mode.setdefault(r["mode"], []).append(r)

    is_signal = data.get("driver") == "hook-replay"
    metric_key = "signal_retention" if is_signal else "passed"
    metric_hdr = "Signal Ret" if is_signal else "Pass Rate"

    def _hit(rec: dict) -> bool:
        if metric_key in rec:
            return bool(rec[metric_key])
        return bool(rec.get("passed", rec.get("signal_retention", False)))

    off_recs = by_mode.get("off", [])
    off_rate = (sum(1 for r in off_recs if _hit(r)) / len(off_recs) * 100.0) if off_recs else 100.0
    off_tok = sum(r.get("total_tokens", 0) for r in off_recs)

    print(f"=== Eval Summary (driver={data.get('driver')}, tasks={data.get('n_tasks')}, runs={data.get('runs')}) ===\n")
    print(f"| {'Mode':8s} | {metric_hdr:10s} | {'95% CI':13s} | {'Min..Max':11s} | {'Tokens':>9s} | {'Tok Delta':>9s} | {'CostProxy':>9s} | {'Retries':>7s} | {'Flag':12s} |")
    print(f"| {'-'*8} | {'-'*10} | {'-'*13} | {'-'*11} | {'-'*9} | {'-'*9} | {'-'*9} | {'-'*7} | {'-'*12} |")
    for m in MODES:
        recs = by_mode.get(m, [])
        if not recs:
            continue
        k = sum(1 for r in recs if _hit(r))
        n = len(recs)
        pr = k / n * 100.0
        lo, hi = wilson_ci(k, n)
        runs_map: dict[int, list[bool]] = {}
        for r in recs:
            runs_map.setdefault(int(r.get("run", 1)), []).append(_hit(r))
        run_prs = [sum(v) / len(v) * 100.0 for v in runs_map.values()]
        rmin, rmax = min(run_prs), max(run_prs)
        toks = sum(r.get("total_tokens", 0) for r in recs)
        tdelta = ((toks / off_tok) - 1.0) * 100.0 if off_tok else 0.0
        cproxy = sum(float(r.get("cost_proxy", 0.0)) for r in recs)
        rets = sum(r.get("retries", 0) for r in recs)
        flag = "LOSS >2pp" if (m != "off" and (off_rate - pr) > 2.0) else "OK"
        print(f"| {m:8s} | {pr:8.1f}%  | {lo:5.1f}..{hi:5.1f}% | {rmin:4.0f}..{rmax:4.0f}%  | {toks:9,d} | {tdelta:+8.1f}% | {cproxy:9,.0f} | {rets:7d} | {flag:12s} |")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verify-fixtures", action="store_true", help="Verify all 30 tasks fail before fix and pass after reference fix")
    ap.add_argument("--driver", choices=("hook-replay", "agy", "manual"), default="hook-replay")
    ap.add_argument("--modes", default=",".join(MODES), help="Comma-separated modes to run")
    ap.add_argument("--tasks", default=None, help="Optional comma-separated task ID prefixes, e.g. T01,T02")
    ap.add_argument("--runs", type=int, default=3, help="Repetitions per (task, mode), default 3")
    ap.add_argument("--agy-cmd", default="", help="Override headless agy command template with {workspace} and {prompt}")
    ap.add_argument("--out", default=None, help="Output JSON path (defaults to eval/results/hook_eval.json or live_eval.json)")
    ap.add_argument("--summarize", default=None, help="Summarize an existing results JSON file")
    a = ap.parse_args()

    if a.verify_fixtures:
        return verify_fixtures()
    if a.summarize:
        return summarize(json.loads(Path(a.summarize).read_text(encoding="utf-8")))

    tasks = load_tasks()
    if a.tasks:
        prefixes = [p.strip() for p in a.tasks.split(",") if p.strip()]
        tasks = [t for t in tasks if any(t["id"].startswith(p) for p in prefixes)]
    sel_modes = [m.strip() for m in a.modes.split(",") if m.strip() in MODES]
    out_path = Path(a.out) if a.out else (EVAL_DIR / "results" / ("hook_eval.json" if a.driver == "hook-replay" else "live_eval.json"))

    if a.driver == "agy" and not a.agy_cmd and not shutil.which("agy"):
        print("ERROR: `agy` CLI not found on PATH. Use `--driver manual` for IDE evaluation or `--driver hook-replay` for signal-retention checks.", file=sys.stderr)
        return 2

    records: list[dict] = []
    if a.driver in ("agy", "manual"):
        saved_cfg = None
        try:
            for mode in sel_modes:
                if a.driver == "agy":
                    saved_cfg = _set_hud_mode(mode)
                else:
                    input(f"\nSwitch Token Saver HUD to '{mode}' (or disable plugin for 'off'), then press ENTER to begin {mode}...")
                for run_idx in range(1, a.runs + 1):
                    for task in tasks:
                        rec = run_live_task(task, mode, run_idx, a.driver, agy_cmd=a.agy_cmd)
                        if rec is not None:
                            records.append(rec)
        except KeyboardInterrupt:
            print("\nStopped early; saving completed records.")
        finally:
            if saved_cfg is not None:
                _restore_hud_mode(saved_cfg)
    else:
        for mode in sel_modes:
            for run_idx in range(1, a.runs + 1):
                for task in tasks:
                    records.append(run_hook_replay_task(task, mode, run_idx))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "driver": a.driver,
        "n_tasks": len(tasks),
        "runs": a.runs,
        "modes": sel_modes,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "records": records,
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(records)} records to {out_path}")
    return summarize(payload)


if __name__ == "__main__":
    raise SystemExit(main())
