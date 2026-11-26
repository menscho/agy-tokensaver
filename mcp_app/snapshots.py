"""Handoff packet generation, archive/rollover execution, HUD state builder, and background tick."""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from config import (
    APP_DIRS, COLD_MIN_CTX, COLD_START_TOK, DATA_DIR, PLUGIN_ROOT,
    _load_rollover_state, _save_rollover_state, _state_lock, append_rollover_history,
    has_direct_handoff_cap, load_config, load_rollover_history,
)
from rollover import compute_adaptive_threshold, continuation_probability
from rules_sync import flush_pending_concise_sync
from telemetry import compute_savings_analytics, load_events_summary
from transcript_io import _session_paths, discover_session_sources, parse_session_cached
from updater import get_update_snapshot

_last_auto_event: dict[str, Any] = {}


def _conv_rpc(ls_addr: str, csrf: str, method: str, body: dict[str, Any], timeout: float = 12.0) -> dict[str, Any]:
    req = urllib.request.Request(
        f"http://{ls_addr}/product.v1.ConversationService/{method}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "x-antigravity-csrf-token": csrf,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw) if raw else {}
    return parsed if isinstance(parsed, dict) else {}


def _find_agentapi_cmd() -> list[str] | None:
    exe = os.environ.get("ANTIGRAVITY_AGENTAPI_EXE")
    if exe and Path(exe).is_file():
        return [exe, "agentapi"]
    shim = Path.home() / ".gemini" / "antigravity" / "bin" / "agentapi"
    if shim.is_file():
        return [str(shim)]
    return None


def _can_spawn_chat() -> bool:
    ls_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
    if not ls_addr:
        return False
    return bool(os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "").strip()) or bool(_find_agentapi_cmd())


def _read_last_auto_event() -> dict[str, Any]:
    ev = dict(_last_auto_event)
    ev_file = DATA_DIR / "last_auto_event.json"
    if ev_file.is_file():
        try:
            disk = json.loads(ev_file.read_text(encoding="utf-8", errors="replace"))
            if isinstance(disk, dict) and int(disk.get("ts") or 0) >= int(ev.get("ts") or 0):
                ev = disk
        except Exception:
            pass
    return ev


def build_handoff_packet(ws: str, conv_id: str = "") -> dict[str, Any]:
    if not ws or not Path(ws).is_dir():
        ws = str(Path.home())
    cmd = [sys.executable, str(PLUGIN_ROOT / "scripts" / "handoff_pack.py"), ws, "--no-save"]
    if conv_id:
        cmd.extend(["--conversation-id", conv_id])
    p = subprocess.run(cmd, text=True, capture_output=True, timeout=12)
    txt = p.stdout.strip()
    return {"packet": txt, "est_tokens": len(txt) // 4, "rc": p.returncode}


def build_repo_digest(ws: str) -> dict[str, Any]:
    if not ws or not Path(ws).is_dir():
        ws = str(Path.home())
    p = subprocess.run(
        [sys.executable, str(PLUGIN_ROOT / "scripts" / "repo_digest.py"), ws],
        text=True, capture_output=True, timeout=12,
    )
    txt = p.stdout.strip()
    return {"digest": txt, "est_tokens": len(txt) // 4, "rc": p.returncode}


def perform_rollover(
    ws: str,
    active: dict[str, Any] | None,
    kind: str = "manual",
    model: str = "inherit",
    spawn_chat: bool = True,
    cap_k: int = 110,
) -> dict[str, Any]:
    target_cid = str(active["conversation_id"]) if active and active.get("conversation_id") else ""
    ho = build_handoff_packet(ws, target_cid)
    msg = ho.get("packet", "")
    if not msg:
        return {"ok": False, "error": "Could not build handoff packet (empty output)."}

    to_conv = ""
    raw_out = ""
    if spawn_chat:
        flush_pending_concise_sync(force=True)
        ls_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
        csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "").strip()
        if ls_addr and csrf:
            body: dict[str, Any] = {"prompt": [{"text": msg}], "tags": ["token-saver-handoff"]}
            proj_id = os.environ.get("ANTIGRAVITY_PROJECT_ID", "").strip()
            if target_cid:
                try:
                    meta_resp = _conv_rpc(ls_addr, csrf, "GetConversation", {"conversationId": target_cid}, timeout=5.0)
                    meta_raw = meta_resp.get("metadata")
                    meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}
                    if meta.get("projectId"):
                        proj_id = str(meta["projectId"])
                    if proj_id and meta.get("environmentId"):
                        body["environmentId"] = str(meta["environmentId"])
                except Exception:
                    pass
            if proj_id:
                body["projectId"] = proj_id
            if model and model != "inherit":
                body["model"] = model
            try:
                created = _conv_rpc(ls_addr, csrf, "CreateConversation", body, timeout=12.0)
                conv_raw = created.get("conversation")
                conv_obj: dict[str, Any] = conv_raw if isinstance(conv_raw, dict) else {}
                to_conv = str(conv_obj.get("id") or "")
                raw_out = json.dumps(created)
                if to_conv and target_cid:
                    try:
                        _conv_rpc(
                            ls_addr,
                            csrf,
                            "UpdateConversation",
                            {"conversationId": target_cid, "annotations": {"archived": True}},
                            timeout=4.0,
                        )
                    except Exception:
                        pass
            except Exception as exc:
                return {"ok": False, "error": f"ConversationService error: {exc}"}
        else:
            base_cmd = _find_agentapi_cmd()
            if not base_cmd:
                return {"ok": False, "error": "Direct chat spawn unavailable from global sidebar; use side pane (+) or Copy Handoff."}
            cmd = [*base_cmd, "new-conversation"]
            if model and model != "inherit":
                cmd.append(f"--model={model}")
            cmd.append(msg)
            p = subprocess.run(cmd, text=True, capture_output=True, timeout=18)
            if p.returncode != 0:
                err_txt = (p.stderr or p.stdout or "").strip()
                if "ANTIGRAVITY_LS_ADDRESS" in err_txt:
                    err_txt = "Global sidebar has no LS session address; open agy-tokensaver in a chat side pane (+) or let Auto-Handoff run on your next message."
                return {"ok": False, "error": err_txt or f"agentapi exit {p.returncode}"}
            raw_out = p.stdout.strip()
            try:
                parsed = json.loads(raw_out) if raw_out else {}
                resp_conv = (parsed.get("response") or {}).get("newConversation") or {}
                to_conv = str(
                    resp_conv.get("conversationId")
                    or resp_conv.get("conversation_id")
                    or parsed.get("conversationId")
                    or parsed.get("conversation_id")
                    or parsed.get("id")
                    or ""
                )
            except Exception:
                pass

    now_ts = int(time.time())
    peak = int(active["cur_tot"]) if active else 0
    # Conservative estimate for next ~20 turns saved by resetting peak -> 22k
    saved_per_turn_cr = max(0, peak - 22_000)
    est_cache_saved = saved_per_turn_cr * 15
    est_input_saved = max(0, peak - 22_000)  # 1 avoided cache drop
    entry = {
        "id": f"ro-{now_ts}",
        "ts": now_ts,
        "iso_time": datetime.datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d %H:%M"),
        "kind": kind,
        "from_conv": (active["conversation_id"][:8] if active else "manual"),
        "to_conv": to_conv[:8] if to_conv else "",
        "workspace": ws,
        "goal": (active.get("goal_preview") if active else "") or "Session Continuation",
        "peak_tokens": peak,
        "turns": int(active["turns"]) if active else 0,
        "cap_k": cap_k,
        "est_input_saved": est_input_saved,
        "est_cache_saved": est_cache_saved,
        "packet": msg,
        "est_packet_tokens": ho["est_tokens"],
    }
    append_rollover_history(entry)
    return {"ok": True, "entry": entry, "to_conv": to_conv, "raw": raw_out}


def build_state(conv_id: str = "") -> dict[str, Any]:
    now = time.time()
    all_sessions: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None

    # IDE + CLI sessions; token usage falls back to conversations/<id>.db when the transcript has none
    cands = discover_session_sources()
    if conv_id:
        for app in APP_DIRS:
            tx, db = _session_paths(app, conv_id)
            if tx is not None or db is not None:
                active = parse_session_cached(conv_id, tx, db, app.name)
                if active:
                    break

    for _mt, sid, tx, db in cands[:120]:
        rec = parse_session_cached(sid, tx, db)
        if not rec:
            continue
        if active is None:
            active = rec
        all_sessions.append(rec)

    cfg = load_config()
    if active:
        active = {**active, "p_continue": continuation_probability(all_sessions, active["conversation_id"])}
    adaptive = compute_adaptive_threshold(active, cfg)

    if active:
        age = max(0, int(now - active["last_ts"]))
        ttl_cliff = int(active.get("ttl_cliff_sec") or 150)
        is_cold = age >= ttl_cliff
        recent_10m = sum(1 for t in active.get("turn_timestamps", []) if (now - t) <= 600)
        active = {
            **{k: v for k, v in active.items() if k != "turn_timestamps"},
            "idle_sec": age,
            "ttl_remaining_sec": max(0, ttl_cliff - age),
            "is_cold": is_cold,
            "cold_rebuild_save_est": max(0, int(active["cur_tot"]) - COLD_START_TOK) if is_cold else 0,
            "recent_turns_10m": recent_10m,
        }

    hooks = load_events_summary()
    history = load_rollover_history()
    savings = compute_savings_analytics(all_sessions, hooks, history, cfg)

    recent_clean = [
        {k: v for k, v in s.items() if k != "turn_timestamps"}
        for s in all_sessions[:12]
    ]

    return {
        "now": int(now),
        "active": active,
        "adaptive": adaptive,
        "recent": recent_clean,
        "hooks": hooks,
        "savings": savings,
        "history": history[:20],
        "config": cfg,
        "last_auto_event": _read_last_auto_event(),
        "can_new_chat": _can_spawn_chat(),
        "auto_handoff_hook": True,
        "direct_handoff_hook": has_direct_handoff_cap(),
        "workspace": os.environ.get("ANTIGRAVITY_WORKSPACE_PATH", str(Path.cwd())),
        "updates": get_update_snapshot(),
    }



def run_smart_rollover_tick() -> None:
    """Single anti-spam adaptive + cold-cache rollover evaluation tick (Persistent Context Graphs arXiv:2609.40118)."""
    global _last_auto_event
    # Apply any deferred rules/AGENTS.md sync as soon as no warm large cache is active
    flush_pending_concise_sync(force=False)
    cfg = load_config()
    mode = cfg.get("mode", "smart_auto")
    if mode == "off" or not cfg.get("auto_rollover", True):
        return

    st = build_state(os.environ.get("ANTIGRAVITY_CONVERSATION_ID", ""))
    act = st.get("active")
    if not act:
        return

    cid = act["conversation_id"]
    cap_k = st["adaptive"]["cap_k"]
    now = int(time.time())

    # Only IDE chats (agentapi spawns IDE conversations); never a CLI session from here.
    if act.get("app") and act.get("app") != "antigravity":
        return
    # Agent run must be finished: spawning a new chat mid-run would NOT stop the old one.
    if not act.get("last_settled"):
        return

    # Cold-cache trigger (Persistent Context Graphs arXiv:2609.40118 / arXiv:2607.19214): once a >=55k chat's KV cache has
    # ALREADY expired (idle >= ttl_cliff, up to 30m), resuming it pays cur_tot uncached vs 17k fresh handoff
    # (instant Turn-1 payback, zero warm cache wasted).
    is_cold_payback = bool(
        act.get("is_cold")
        and act.get("cur_tot", 0) >= COLD_MIN_CTX
        and act.get("turns", 0) >= 4
        and act.get("idle_sec", 0) <= 1800
    )

    if not is_cold_payback:
        # Warm-chat guardrails: shared causal policy + active momentum + settled window
        if not st["adaptive"].get("roll"):
            return
        if act.get("recent_turns_10m", 0) < 3:
            return
        if not (20 <= act["idle_sec"] <= 600):
            return

    spawn = (mode == "smart_auto") and st.get("can_new_chat", False)
    bucket_key = "rolled_convs" if spawn else "snapshotted_convs"

    with _state_lock:
        rstate = _load_rollover_state()
        # Mode 1 ("snapshot") NEVER blocks upgrading the same chat to Mode 2 ("smart_auto") later!
        if cid in rstate[bucket_key]:
            return
        cooldown_sec = cfg.get("cooldown_min", 20) * 60
        if spawn and now - rstate.get("last_rollover_ts", 0) < cooldown_sec:
            return

    kind = "auto" if spawn else "snapshot"
    res = perform_rollover(
        ws=st["workspace"],
        active=act,
        kind=kind,
        model=cfg.get("model", "inherit"),
        spawn_chat=spawn,
        cap_k=cap_k,
    )
    if res.get("ok"):
        with _state_lock:
            rstate = _load_rollover_state()
            rstate[bucket_key][cid] = now
            if spawn:
                rstate["last_rollover_ts"] = now
            _save_rollover_state(rstate)
    _last_auto_event = {
        "ts": now,
        "from_conv": cid[:8],
        "to_conv": res.get("to_conv", "")[:8],
        "at_tokens": act["cur_tot"],
        "cap_k": cap_k,
        "mode": mode,
        "ok": res.get("ok", False),
        "error": res.get("error", ""),
    }


def auto_rollover_loop(stop_ev: threading.Event) -> None:
    while not stop_ev.wait(10.0):
        try:
            run_smart_rollover_tick()
        except Exception:
            pass
