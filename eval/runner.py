#!/usr/bin/env python3
"""
Signal-retention & live task evaluation harness for agy-tokensaver.

Supports four drivers (zero third-party dependencies):
  1. --verify-fixtures : proves every task fails cleanly on unpatched fixtures and passes after reference edits.
  2. --driver hook-replay : deterministic replay of read_file calls through `read_hook.py` across modes
     (in `off`, unmodified full file content is delivered with zero truncation).
  3. --driver antigravity : live end-to-end execution via Antigravity's local `ConversationService` HTTP+JSON
     API (requires `ANTIGRAVITY_LS_ADDRESS` and `ANTIGRAVITY_CSRF_TOKEN`), testing BOTH:
       - standard tasks (`T*`) under `off`, `default` (Auto-Handoff + Read Guards), `compact`, `max`
       - multi-step handoff continuity tasks (`H*`) comparing single-session `off` vs Phase 1 -> `handoff_pack.py`
         -> fresh Phase 2 conversation with zero Phase 1 history.
  4. --driver agy | manual : CLI or interactive IDE walkthrough recording `eval/results/live_eval.json`.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
ROOT = EVAL_DIR.parent
SCRIPTS = ROOT / "scripts"
FIXTURES = EVAL_DIR / "fixtures"
TASKS_PATH = EVAL_DIR / "tasks.json"
SCRATCH_EVAL_BASE = Path.home() / ".gemini" / "antigravity" / "scratch"

MODES = ("off", "guards", "default", "compact", "max")
MODE_ENV = {
    "off": {"AGY_TS_HOOKS": "off", "AGY_TS_COMPACT_READS": "0"},
    "guards": {"AGY_TS_HOOKS": "on", "AGY_TS_COMPACT_READS": "0"},
    "default": {"AGY_TS_HOOKS": "on", "AGY_TS_COMPACT_READS": "0"},
    "compact": {"AGY_TS_HOOKS": "on", "AGY_TS_COMPACT_READS": "1"},
    "max": {"AGY_TS_HOOKS": "on", "AGY_TS_COMPACT_READS": "1"},
    # Legacy aliases kept for CLI compatibility
    "mode1": {"AGY_TS_HOOKS": "on", "AGY_TS_COMPACT_READS": "0"},
    "mode2c": {"AGY_TS_HOOKS": "on", "AGY_TS_COMPACT_READS": "1"},
}
MODE_PROFILE: dict[str, dict[str, Any]] = {
    "off": {"mode": "off", "auto_rollover": False, "compact_reads": False, "concise_output": False},
    "guards": {"profile": "zero_loss"},
    "default": {"profile": "auto_handoff"},
    "compact": {"profile": "handoff_only"},
    "max": {"profile": "small_loss"},
    "mode1": {"profile": "zero_loss"},
    "mode2c": {"profile": "handoff_only"},
}


def load_tasks() -> list[dict[str, Any]]:
    return json.loads(TASKS_PATH.read_text(encoding="utf-8"))


def stage_fixture(task: dict[str, Any], dst: Path, init_git: bool = False) -> None:
    shutil.copytree(FIXTURES / task["fixture"], dst)
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
    if init_git:
        subprocess.run(["git", "init", "-q"], cwd=str(dst), check=False)
        subprocess.run(["git", "config", "user.email", "eval@local"], cwd=str(dst), check=False)
        subprocess.run(["git", "config", "user.name", "Eval"], cwd=str(dst), check=False)
        subprocess.run(["git", "add", "-A"], cwd=str(dst), check=False)
        subprocess.run(["git", "commit", "-q", "-m", "initial fixture"], cwd=str(dst), check=False)


def run_verify(task: dict[str, Any], workdir: Path) -> bool:
    cmd = str(task["verify_cmd"])
    if cmd.startswith("python3 "):
        cmd = f'"{sys.executable}" ' + cmd[len("python3 "):]
    p = subprocess.run(cmd, shell=True, cwd=str(workdir), capture_output=True, text=True, timeout=20)
    return p.returncode == 0


def apply_reference(task: dict[str, Any], workdir: Path) -> None:
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
    if "extra_reference_write" in task:
        ewr = task["extra_reference_write"]
        (workdir / ewr["file"]).write_text(ewr["content"], encoding="utf-8")


def verify_fixtures() -> int:
    tasks = load_tasks()
    ok = 0
    for t in tasks:
        with tempfile.TemporaryDirectory(prefix="agy-eval-vf-") as td:
            wd = Path(td) / "repo"
            stage_fixture(t, wd)
            if run_verify(t, wd):
                print(f"FAIL: {t['id']} passed BEFORE applying fix!")
                return 1
            apply_reference(t, wd)
            if not run_verify(t, wd):
                print(f"FAIL: {t['id']} failed AFTER applying reference fix!")
                return 1
            ok += 1
    print(f"PASS: all {ok}/{len(tasks)} eval tasks fail cleanly before fix and pass after reference fix.")
    return 0


def _call_read_hook(path: Path, cid: str, step: int, env: dict[str, str], start: int | None = None, end: int | None = None) -> tuple[str, int, int, bool]:
    """Run read_hook.py (unless mode=='off') and return (delivered_text, tokens, retries, was_reread)."""
    raw_text = path.read_text(encoding="utf-8", errors="replace")
    lines = raw_text.splitlines()
    if env.get("AGY_TS_HOOKS") == "off":
        return raw_text, len(raw_text.encode("utf-8")) // 4, 0, False
    ti: dict[str, Any] = {"Path": str(path)}
    if start is not None:
        ti["StartLine"] = start
    if end is not None:
        ti["EndLine"] = end
    payload = json.dumps({"toolInput": ti, "conversationId": cid, "stepIdx": step}).encode("utf-8")
    p = subprocess.run([sys.executable, "-S", str(SCRIPTS / "read_hook.py")], input=payload, capture_output=True, env=env, timeout=5)
    dec = json.loads(p.stdout.decode("utf-8"))
    if dec.get("decision") == "deny":
        reason = dec.get("reason", "")
        return reason, len(reason.encode("utf-8")) // 4, 1, True
    ov = dec.get("overwrite") or {}
    s = int(ov.get("StartLine", start or 1))
    e = int(ov.get("EndLine", end or len(lines)))
    sliced = "\n".join(lines[max(0, s - 1):e])
    return sliced, len(sliced.encode("utf-8")) // 4, 0, False


def run_hook_replay_task(task: dict[str, Any], mode: str, run_idx: int) -> dict[str, Any]:
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
        }
        cid = f"eval-{mode}-{task['id']}-{run_idx}"
        visible_chunks: list[str] = []
        tot_tok = retries = rereads = 0

        cat = task["category"]
        if cat in ("fix-failing-test", "handoff-continuity") and task["fixture"] == "py_calc":
            p = subprocess.run([sys.executable, "-m", "unittest", "-v", "test_calc.py"], cwd=str(wd), capture_output=True, text=True)
            cmd_out = p.stdout + p.stderr
            visible_chunks.append(cmd_out); tot_tok += len(cmd_out.encode("utf-8")) // 4
            txt, tk, ret, rr = _call_read_hook(wd / "calc.py", cid, 2, env)
            visible_chunks.append(txt); tot_tok += tk; retries += ret; rereads += int(rr)
        elif cat in ("add-feature-and-test", "refactor", "handoff-continuity"):
            if task["fixture"] == "log_ops" and task.get("service"):
                txt, tk, ret, rr = _call_read_hook(wd / f"{task['service']}.log", cid, 1, env)
                visible_chunks.append(txt); tot_tok += tk; retries += ret; rereads += int(rr)
            elif task["fixture"] == "node_api" and "package.json" in task.get("prompt", ""):
                txt, tk, ret, rr = _call_read_hook(wd / "package.json", cid, 1, env)
                visible_chunks.append(txt); tot_tok += tk; retries += ret; rereads += int(rr)
            target = "utils.js" if task["fixture"] == "node_api" else ("logs.py" if task["fixture"] == "log_ops" else "calc.py")
            txt, tk, ret, rr = _call_read_hook(wd / target, cid, 2, env)
            visible_chunks.append(txt); tot_tok += tk; retries += ret; rereads += int(rr)
            txt2, tk2, ret2, rr2 = _call_read_hook(wd / target, cid, 4, env)
            visible_chunks.append(txt2); tot_tok += tk2; retries += ret2; rereads += int(rr2)
        elif cat == "debug-from-log":
            log_file = wd / f"{task['service']}.log"
            txt, tk, ret, rr = _call_read_hook(log_file, cid, 1, env)
            visible_chunks.append(txt); tot_tok += tk; retries += ret; rereads += int(rr)
        elif cat == "lockfile-dependency":
            txt_l, tk_l, ret_l, rr_l = _call_read_hook(wd / "package-lock.json", cid, 1, env)
            visible_chunks.append(txt_l); tot_tok += tk_l; retries += ret_l; rereads += int(rr_l)
            txt_p, tk_p, ret_p, rr_p = _call_read_hook(wd / "package.json", cid, 2, env)
            visible_chunks.append(txt_p); tot_tok += tk_p; retries += ret_p; rereads += int(rr_p)

        combined = "\n".join(visible_chunks)
        signal_retention = all(sig in combined for sig in task["required_signals"])
        wall_ms = round((time.perf_counter() - t0) * 1000.0, 2)
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


def _session_metrics_by_id(cid: str) -> tuple[int, int, int, int, float, int]:
    """Read (input_tokens, cache_read_tokens, output_tokens, turns, cost_proxy, retries) for exact conversation ID."""
    if not cid:
        return 0, 0, 0, 0, 0.0, 0
    sys.path.insert(0, str(SCRIPTS))
    sys.path.insert(0, str(ROOT / "mcp_app"))
    import replay_io

    for app in replay_io.APP_DIRS:
        sess = replay_io.load_session(app, cid)
        if not sess:
            continue
        turns = sess.get("turns") or []
        inp = sum(a for a, _, _ in turns)
        cr = sum(b for _, b, _ in turns)
        out = sum(c for _, _, c in turns)
        cproxy = round(sum(a * 1.0 + b * 0.05 + c * 6.0 for a, b, c in turns), 1)
        retries = sum(1 for ev in (sess.get("events") or []) if "token-saver" in ev.get("content", "").lower() and ("blocked" in ev.get("content", "").lower() or "unchanged" in ev.get("content", "").lower()))
        return inp, cr, out, len(turns), cproxy, retries
    return 0, 0, 0, 0, 0.0, 0


def _collect_live_session_metrics(t_start: float, t_end: float) -> tuple[int, float, int, str]:
    """Find the Antigravity session updated during [t_start, t_end] and extract total tokens/cost/retries."""
    sys.path.insert(0, str(SCRIPTS))
    sys.path.insert(0, str(ROOT / "mcp_app"))
    import replay_io

    best_mtime = 0.0
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
                best_sid = p.parent.parent.parent.name
    if not best_sid:
        return 0, 0.0, 0, ""
    inp, cr, out, _n, cproxy, retries = _session_metrics_by_id(best_sid)
    return inp + cr + out, cproxy, retries, best_sid


def _snapshot_hud_state() -> dict[str, Any]:
    """Capture existing plugin HUD config, rules/AGENTS.md, and telemetry files once before live eval."""
    sys.path.insert(0, str(ROOT / "mcp_app"))
    import config

    prior = config.CFG_FILE.read_text(encoding="utf-8") if config.CFG_FILE.is_file() else None
    agents_md = ROOT / "rules" / "AGENTS.md"
    prior_rules = agents_md.read_text(encoding="utf-8") if agents_md.is_file() else None
    micro_f = config.DATA_DIR / "microskills.json"
    prior_micro = micro_f.read_bytes() if micro_f.is_file() else None
    events_f = config.EVENTS_FILE
    prior_events = events_f.read_bytes() if events_f.is_file() else None
    return {
        "prior_raw": prior, "cfg_file": config.CFG_FILE,
        "prior_rules": prior_rules, "agents_md": agents_md,
        "micro_f": micro_f, "prior_micro": prior_micro,
        "events_f": events_f, "prior_events": prior_events,
    }


def _set_hud_mode(mode: str) -> dict[str, Any]:
    """Switch active plugin HUD config + concise rule to `mode`, returning prior state."""
    saved = _snapshot_hud_state()
    import config

    os.environ["AGY_TS_SYNC_RULES"] = "1"
    config.save_config(MODE_PROFILE[mode])
    return saved


def _restore_hud_mode(saved: dict[str, Any] | None) -> None:
    if not saved:
        return
    os.environ.pop("AGY_TS_SYNC_RULES", None)
    for key, fkey, is_bytes in (
        ("prior_raw", "cfg_file", False),
        ("prior_rules", "agents_md", False),
        ("prior_micro", "micro_f", True),
        ("prior_events", "events_f", True),
    ):
        fp: Path | None = saved.get(fkey)
        if fp is None:
            continue
        prev = saved.get(key)
        if prev is None:
            if fp.is_file():
                fp.unlink()
        else:
            fp.parent.mkdir(parents=True, exist_ok=True)
            if is_bytes:
                fp.write_bytes(prev)
            else:
                fp.write_text(prev, encoding="utf-8")


def _conv_rpc(method: str, body: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    ls = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "")
    csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "")
    if not ls or not csrf:
        raise RuntimeError("ANTIGRAVITY_LS_ADDRESS and ANTIGRAVITY_CSRF_TOKEN must be set for --driver antigravity")
    req = urllib.request.Request(
        f"http://{ls}/product.v1.ConversationService/{method}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1", "x-antigravity-csrf-token": csrf},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _run_antigravity_conversation(title: str, prompt: str, model: str = "flash", poll_timeout: int = 150) -> tuple[str, dict[str, Any]]:
    """Create a child Antigravity conversation, wait until RUN_STATE_IDLE, return (cid, metrics)."""
    resp = _conv_rpc("CreateConversation", {"title": title, "model": model, "tags": ["ts-eval"], "prompt": [{"text": prompt}]})
    cid = str((resp.get("conversation") or {}).get("id") or "")
    if not cid:
        raise RuntimeError(f"CreateConversation did not return id: {resp}")
    t0 = time.monotonic()
    saw_running = False
    while (time.monotonic() - t0) < poll_timeout:
        st = _conv_rpc("GetConversation", {"conversationId": cid})
        run_state = str((st.get("state") or {}).get("runState") or "")
        if run_state == "RUN_STATE_RUNNING":
            saw_running = True
        elif run_state == "RUN_STATE_IDLE" and (saw_running or (time.monotonic() - t0) >= 2.0):
            break
        elif run_state == "RUN_STATE_CANCELLING":
            break
        time.sleep(1.0)
    # Wait briefly for transcript / DB token usage flush
    inp = cr = out = turns = retries = 0
    cproxy = 0.0
    for _ in range(6):
        inp, cr, out, turns, cproxy, retries = _session_metrics_by_id(cid)
        if inp + cr + out > 0:
            break
        time.sleep(0.5)
    return cid, {
        "cid": cid, "input_tokens": inp, "cache_read_tokens": cr, "output_tokens": out,
        "turns": turns, "total_tokens": inp + cr + out, "cost_proxy": cproxy, "retries": retries,
    }


def _delete_conversation_quiet(cid: str) -> None:
    if not cid:
        return
    try:
        _conv_rpc("DeleteConversation", {"conversationId": cid}, timeout=10)
    except Exception:
        pass
    data_dir = Path.home() / ".gemini" / "antigravity" / "plugin_data" / "token-saver"
    for sub in ("reads", "alerts"):
        d = data_dir / sub
        if d.is_dir():
            for fp in d.glob(f"{cid}*"):
                try:
                    fp.unlink()
                except OSError:
                    pass


def _build_handoff_packet(wd: Path, cid: str) -> str:
    env = {**os.environ, "PLUGIN_DATA": str(wd.parent / "_ho_data")}
    p = subprocess.run(
        [sys.executable, str(SCRIPTS / "handoff_pack.py"), str(wd), "--conversation-id", cid, "--no-save"],
        capture_output=True, text=True, timeout=15, env=env,
    )
    return p.stdout.strip()


def run_live_task(task: dict[str, Any], mode: str, run_idx: int, driver: str, agy_cmd: str = "", model: str = "flash") -> dict[str, Any] | None:
    """Stage a fresh temp repo, invoke live agent, and run `verify_cmd` WITHOUT applying reference_edit."""
    is_handoff = bool(task.get("handoff_phase1_prompt") and task.get("handoff_phase2_prompt"))
    use_scratch = driver == "antigravity"
    if use_scratch:
        SCRATCH_EVAL_BASE.mkdir(parents=True, exist_ok=True)
        td_ctx: Any = tempfile.TemporaryDirectory(prefix=f"agy-eval-{mode}-{task['id'][:8]}-", dir=str(SCRATCH_EVAL_BASE))
    else:
        td_ctx = tempfile.TemporaryDirectory(prefix=f"agy-live-{mode}-")

    created_cids: list[str] = []
    try:
        with td_ctx as td:
            wd = Path(td) / "repo"
            stage_fixture(task, wd, init_git=is_handoff)
            env = {**os.environ, **MODE_ENV.get(mode, {}), "PLUGIN_ROOT": str(ROOT)}
            t_start_wall = time.time()
            t0 = time.perf_counter()
            ws_scope = (
                f"Work ONLY inside the workspace directory `{wd}` using absolute paths under `{wd}` "
                f"(and Cwd `{wd}` for any commands). Stay strictly inside `{wd}`.\n\n"
            )

            extra: dict[str, Any] = {}
            if driver == "antigravity":
                if is_handoff and mode != "off":
                    cid_a, m_a = _run_antigravity_conversation(
                        f"eval-{mode}-{task['id']}-p1",
                        ws_scope + str(task["handoff_phase1_prompt"]),
                        model=model,
                    )
                    created_cids.append(cid_a)
                    packet = _build_handoff_packet(wd, cid_a)
                    cid_b, m_b = _run_antigravity_conversation(
                        f"eval-{mode}-{task['id']}-p2",
                        f"{packet}\n\n---\n{ws_scope}{task['handoff_phase2_prompt']}",
                        model=model,
                    )
                    created_cids.append(cid_b)
                    inp = m_a["input_tokens"] + m_b["input_tokens"]
                    cr = m_a["cache_read_tokens"] + m_b["cache_read_tokens"]
                    out = m_a["output_tokens"] + m_b["output_tokens"]
                    tot_tok = inp + cr + out
                    cost_proxy = round(m_a["cost_proxy"] + m_b["cost_proxy"], 1)
                    retries = m_a["retries"] + m_b["retries"]
                    sid = f"{cid_a[:8]}->{cid_b[:8]}"
                    extra = {
                        "handoff_reset": True,
                        "handoff_packet_tokens": len(packet.encode("utf-8")) // 4,
                        "phase1_tokens": m_a["total_tokens"],
                        "phase2_tokens": m_b["total_tokens"],
                        "input_tokens": inp, "cache_read_tokens": cr, "output_tokens": out,
                        "turns": m_a["turns"] + m_b["turns"],
                    }
                else:
                    cid, m = _run_antigravity_conversation(
                        f"eval-{mode}-{task['id']}",
                        ws_scope + str(task["prompt"]),
                        model=model,
                    )
                    created_cids.append(cid)
                    tot_tok = m["total_tokens"]
                    cost_proxy = m["cost_proxy"]
                    retries = m["retries"]
                    sid = cid[:8]
                    extra = {
                        "handoff_reset": False,
                        "input_tokens": m["input_tokens"],
                        "cache_read_tokens": m["cache_read_tokens"],
                        "output_tokens": m["output_tokens"],
                        "turns": m["turns"],
                    }
            elif driver == "agy":
                tmpl = agy_cmd or 'agy --cwd {workspace} {prompt}'
                cmd_parts = [part.format(workspace=str(wd), prompt=task["prompt"]) for part in shlex.split(tmpl)]
                subprocess.run(cmd_parts, cwd=str(wd), env=env, check=False, timeout=300)
                t_end_wall = time.time()
                tot_tok, cost_proxy, retries, sid = _collect_live_session_metrics(t_start_wall, t_end_wall)
            else:
                print(f"\n--- Live IDE Task [{mode} | run {run_idx}/{task['id']}] ---")
                print(f"Workspace : {wd}")
                print(f"Prompt    : {task['prompt']}")
                ans = input("Press ENTER when Antigravity finishes ('s' to skip task, 'q' to abort): ").strip().lower()
                if ans == "q":
                    raise KeyboardInterrupt("Aborted by user")
                if ans == "s":
                    return None
                t_end_wall = time.time()
                tot_tok, cost_proxy, retries, sid = _collect_live_session_metrics(t_start_wall, t_end_wall)

            wall_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            passed = run_verify(task, wd)
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
                **extra,
            }
    finally:
        for cid in created_cids:
            _delete_conversation_quiet(cid)


def summarize(data: dict[str, Any]) -> int:
    records = data.get("records", [])
    by_mode: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        by_mode.setdefault(r["mode"], []).append(r)

    is_signal = data.get("driver") == "hook-replay"
    metric_key = "signal_retention" if is_signal else "passed"
    metric_hdr = "Signal Ret" if is_signal else "Pass Rate"

    def _hit(rec: dict[str, Any]) -> bool:
        if metric_key in rec:
            return bool(rec[metric_key])
        return bool(rec.get("passed", rec.get("signal_retention", False)))

    off_recs = by_mode.get("off", [])
    off_rate = (sum(1 for r in off_recs if _hit(r)) / len(off_recs) * 100.0) if off_recs else 100.0
    off_tok = sum(r.get("total_tokens", 0) for r in off_recs)
    off_cproxy = sum(float(r.get("cost_proxy", 0.0)) for r in off_recs)

    ordered_modes = list(MODES) + [m for m in by_mode if m not in MODES]
    print(f"=== Eval Summary (driver={data.get('driver')}, model={data.get('model', 'n/a')}, tasks={data.get('n_tasks')}, runs={data.get('runs')}) ===\n")
    print(f"| {'Mode':8s} | {metric_hdr:10s} | {'95% CI':13s} | {'Min..Max':11s} | {'Tokens':>9s} | {'Tok Delta':>9s} | {'CostProxy':>9s} | {'CostDelta':>9s} | {'Retries':>7s} | {'Flag':12s} |")
    print(f"| {'-'*8} | {'-'*10} | {'-'*13} | {'-'*11} | {'-'*9} | {'-'*9} | {'-'*9} | {'-'*9} | {'-'*7} | {'-'*12} |")
    for m in ordered_modes:
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
        cdelta = ((cproxy / off_cproxy) - 1.0) * 100.0 if off_cproxy else 0.0
        rets = sum(r.get("retries", 0) for r in recs)
        flag = "LOSS >2pp" if (m != "off" and (off_rate - pr) > 2.0) else "OK"
        print(f"| {m:8s} | {pr:8.1f}%  | {lo:5.1f}..{hi:5.1f}% | {rmin:4.0f}..{rmax:4.0f}%  | {toks:9,d} | {tdelta:+8.1f}% | {cproxy:9,.0f} | {cdelta:+8.1f}% | {rets:7d} | {flag:12s} |")

    ho_recs = [r for r in records if r.get("category") == "handoff-continuity"]
    std_recs = [r for r in records if r.get("category") != "handoff-continuity"]
    if ho_recs and std_recs and not is_signal:
        for sub_name, sub_list in (("Standard Tasks (T*)", std_recs), ("Auto-Handoff Continuity (H*)", ho_recs)):
            print(f"\n--- {sub_name} ---")
            sub_by: dict[str, list[dict[str, Any]]] = {}
            for r in sub_list:
                sub_by.setdefault(r["mode"], []).append(r)
            for m in ordered_modes:
                sr = sub_by.get(m, [])
                if not sr:
                    continue
                k = sum(1 for r in sr if _hit(r))
                n = len(sr)
                lo, hi = wilson_ci(k, n)
                tk = sum(r.get("total_tokens", 0) for r in sr)
                cp = sum(float(r.get("cost_proxy", 0.0)) for r in sr)
                out_tk = sum(r.get("output_tokens", 0) for r in sr)
                pkt = [int(r["handoff_packet_tokens"]) for r in sr if r.get("handoff_packet_tokens")]
                pkt_note = f" | pkt={sum(pkt)//len(pkt)} tok" if pkt else ""
                print(f"  {m:8s}: {k}/{n} ({k/n*100:.1f}%, CI {lo}..{hi}%) | tok={tk:,} | out={out_tk:,} | cost={cp:,.0f}{pkt_note}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verify-fixtures", action="store_true", help="Verify all tasks fail before fix and pass after reference fix")
    ap.add_argument("--driver", choices=("hook-replay", "antigravity", "agy", "manual"), default="hook-replay")
    ap.add_argument("--modes", default="off,default,compact,max", help="Comma-separated modes to run")
    ap.add_argument("--tasks", default=None, help="Optional comma-separated task ID prefixes, e.g. T01,H01")
    ap.add_argument("--runs", type=int, default=3, help="Repetitions per (task, mode), default 3")
    ap.add_argument("--workers", type=int, default=1, help="Parallel worker threads per mode for --driver antigravity")
    ap.add_argument("--model", default="flash", help="Model for --driver antigravity (default: flash)")
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
    sel_modes = [m.strip() for m in a.modes.split(",") if m.strip() in MODE_PROFILE]
    out_path = Path(a.out) if a.out else (EVAL_DIR / "results" / ("hook_eval.json" if a.driver == "hook-replay" else "live_eval.json"))

    if a.driver == "agy" and not a.agy_cmd and not shutil.which("agy"):
        print("ERROR: `agy` CLI not found on PATH. Use `--driver antigravity`, `--driver manual`, or `--driver hook-replay`.", file=sys.stderr)
        return 2
    if a.driver == "antigravity" and (not os.environ.get("ANTIGRAVITY_LS_ADDRESS") or not os.environ.get("ANTIGRAVITY_CSRF_TOKEN")):
        print("ERROR: ANTIGRAVITY_LS_ADDRESS and ANTIGRAVITY_CSRF_TOKEN must be set for `--driver antigravity`.", file=sys.stderr)
        return 2

    records: list[dict[str, Any]] = []
    rec_lock = threading.Lock()
    if a.driver in ("antigravity", "agy", "manual"):
        saved_cfg = _snapshot_hud_state() if a.driver in ("antigravity", "agy") else None
        try:
            for mode in sel_modes:
                if a.driver in ("antigravity", "agy"):
                    _set_hud_mode(mode)
                else:
                    input(f"\nSwitch Token Saver HUD to '{mode}', then press ENTER to begin {mode}...")
                if a.driver == "antigravity" and a.workers > 1:
                    jobs = [(run_idx, task) for run_idx in range(1, a.runs + 1) for task in tasks]

                    def _exec_job(job: tuple[int, dict[str, Any]]) -> dict[str, Any] | None:
                        r_idx, t_obj = job
                        try:
                            rec = run_live_task(t_obj, mode, r_idx, a.driver, agy_cmd=a.agy_cmd, model=a.model)
                        except Exception as exc:
                            with rec_lock:
                                print(f"[{a.driver}] mode={mode} run={r_idx} task={t_obj['id']} ERROR: {exc}", flush=True)
                            return None
                        if rec is not None:
                            with rec_lock:
                                print(f"[{a.driver}] mode={mode} run={r_idx} task={t_obj['id']} -> passed={rec['passed']} tok={rec['total_tokens']} cost={rec['cost_proxy']} wall={rec['wall_ms']}ms", flush=True)
                        return rec

                    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as pool:
                        futs = []
                        for j in jobs:
                            futs.append(pool.submit(_exec_job, j))
                            time.sleep(0.15)
                        for fut in concurrent.futures.as_completed(futs):
                            rec = fut.result()
                            if rec is not None:
                                records.append(rec)
                else:
                    for run_idx in range(1, a.runs + 1):
                        for task in tasks:
                            print(f"[{a.driver}] mode={mode} run={run_idx}/{a.runs} task={task['id']} ...", flush=True)
                            rec = run_live_task(task, mode, run_idx, a.driver, agy_cmd=a.agy_cmd, model=a.model)
                            if rec is not None:
                                print(f"  -> passed={rec['passed']} tok={rec['total_tokens']} cost={rec['cost_proxy']} wall={rec['wall_ms']}ms", flush=True)
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
        "model": a.model if a.driver == "antigravity" else ("hooks" if a.driver == "hook-replay" else "manual"),
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
