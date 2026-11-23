#!/usr/bin/env python3
"""
Token Saver Core Analytics, Adaptive Auto-Rollover & Session Archive Engine.
Pure Python 3 stdlib — zero external dependencies, zero LLM tokens.
"""

from __future__ import annotations

import collections
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent
GITHUB_REPO = "menscho/agy-tokensaver"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"
BRAIN_DIR = Path.home() / ".gemini" / "antigravity" / "brain"
DATA_DIR = Path(
    os.environ.get("PLUGIN_DATA")
    or (Path.home() / ".gemini" / "antigravity" / "plugin_data" / "token-saver")
)

CFG_FILE = DATA_DIR / "hud_config.json"
ROLLOVER_STATE_FILE = DATA_DIR / "rollover_state.json"
ROLLOVER_HISTORY_FILE = DATA_DIR / "rollover_history.json"
EVENTS_FILE = DATA_DIR / "events.jsonl"

_cfg_lock = threading.Lock()
_state_lock = threading.Lock()
_tx_cache: dict[str, tuple[float, int, dict[str, Any]]] = {}
_ev_cache: tuple[float, int, dict[str, Any]] = (0.0, -1, {})
_last_auto_event: dict[str, Any] = {}
_update_cache: tuple[float, dict[str, Any]] = (0.0, {})

# Quota cost weights (matches stats.py): uncached input 1.0x, cache read 0.05x, output 6.0x
W_IN = 1.0
W_CR = 0.05
W_OUT = 6.0


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_config() -> dict[str, Any]:
    default = {
        "auto_rollover": True,
        "mode": "prepare_only",        # "prepare_only" (safe default) | "smart_auto" | "off"
        "threshold_mode": "auto",      # "auto" | "75" | "90" | "110" | "140" | "180"
        "threshold_k": 110,
        "model": "inherit",
        "min_turns": 14,
        "cooldown_min": 20,
    }
    if not CFG_FILE.is_file():
        return default
    try:
        d = json.loads(CFG_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            auto_on = bool(d.get("auto_rollover", True))
            mode = str(d.get("mode") or ("prepare_only" if auto_on else "off"))
            if mode not in ("smart_auto", "prepare_only", "off"):
                mode = "prepare_only" if auto_on else "off"
            tmode = str(d.get("threshold_mode") or "auto")
            if tmode not in ("auto", "75", "90", "110", "140", "180"):
                tmode = "auto"
            tk = max(60, min(300, int(d.get("threshold_k", 110))))
            return {
                "auto_rollover": mode != "off",
                "mode": mode,
                "threshold_mode": tmode,
                "threshold_k": tk,
                "model": str(d.get("model") or "inherit"),
                "min_turns": max(6, min(40, int(d.get("min_turns", 14)))),
                "cooldown_min": max(5, min(120, int(d.get("cooldown_min", 20)))),
            }
    except Exception:
        pass
    return default


def save_config(patch: dict[str, Any]) -> dict[str, Any]:
    with _cfg_lock:
        cur = load_config()
        merged = {**cur, **patch}
        if "mode" in patch:
            mode = str(patch["mode"])
            if mode not in ("smart_auto", "prepare_only", "off"):
                mode = "smart_auto"
            merged["mode"] = mode
            merged["auto_rollover"] = mode != "off"
        elif "auto_rollover" in patch:
            on = bool(patch["auto_rollover"])
            merged["auto_rollover"] = on
            if not on:
                merged["mode"] = "off"
            elif merged.get("mode") == "off":
                merged["mode"] = "smart_auto"

        tmode = str(merged.get("threshold_mode") or "auto")
        if tmode not in ("auto", "75", "90", "110", "140", "180"):
            tmode = "auto"
        merged["threshold_mode"] = tmode
        if tmode.isdigit():
            merged["threshold_k"] = int(tmode)
        else:
            merged["threshold_k"] = max(60, min(300, int(merged.get("threshold_k", 110))))

        clean = {
            "auto_rollover": bool(merged["auto_rollover"]),
            "mode": merged["mode"],
            "threshold_mode": merged["threshold_mode"],
            "threshold_k": merged["threshold_k"],
            "model": str(merged.get("model") or "inherit"),
            "min_turns": max(6, min(40, int(merged.get("min_turns", 14)))),
            "cooldown_min": max(5, min(120, int(merged.get("cooldown_min", 20)))),
        }
        _atomic_write_json(CFG_FILE, clean)
        return clean


def _load_rollover_state() -> dict[str, Any]:
    if not ROLLOVER_STATE_FILE.is_file():
        return {"rolled_convs": {}, "last_rollover_ts": 0}
    try:
        d = json.loads(ROLLOVER_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            return {
                "rolled_convs": dict(d.get("rolled_convs") or {}),
                "last_rollover_ts": int(d.get("last_rollover_ts") or 0),
            }
    except Exception:
        pass
    return {"rolled_convs": {}, "last_rollover_ts": 0}


def _save_rollover_state(st: dict[str, Any]) -> None:
    # Keep last 200 rolled convs so file never grows unbounded
    rc = st.get("rolled_convs") or {}
    if len(rc) > 200:
        newest = sorted(rc.items(), key=lambda kv: kv[1], reverse=True)[:150]
        st["rolled_convs"] = dict(newest)
    _atomic_write_json(ROLLOVER_STATE_FILE, st)


def load_rollover_history() -> list[dict[str, Any]]:
    if not ROLLOVER_HISTORY_FILE.is_file():
        return []
    try:
        d = json.loads(ROLLOVER_HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(d, list):
            return d[:50]
    except Exception:
        pass
    return []


def append_rollover_history(entry: dict[str, Any]) -> list[dict[str, Any]]:
    with _state_lock:
        hist = load_rollover_history()
        hist.insert(0, entry)
        hist = hist[:50]
        _atomic_write_json(ROLLOVER_HISTORY_FILE, hist)
        return hist


def parse_iso_ts(ts_str: str) -> float | None:
    if not ts_str:
        return None
    try:
        return datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def parse_transcript_cached(tx: Path) -> dict[str, Any] | None:
    try:
        st = tx.stat()
    except OSError:
        return None
    key = str(tx)
    cached = _tx_cache.get(key)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]

    turns = tot_in = tot_cr = tot_out = warm_in = warm_cr = drops = tail_2049 = prev_cr = 0
    cur_in = cur_cr = cur_tot = peak_ctx = 0
    last_model = ""
    first_ts = st.st_mtime
    last_ts = st.st_mtime
    img_reads = 0
    turn_tuples: list[tuple[int, int, int]] = []
    turn_timestamps: list[float] = []
    last_user_goal = ""
    is_handoff_session = False

    try:
        with tx.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"read_file"' in line and any(ext in line.lower() for ext in (".png", ".jpg", ".jpeg", ".webp")):
                    img_reads += 1
                if '"USER_INPUT"' in line:
                    try:
                        uobj = json.loads(line)
                        if uobj.get("type") == "USER_INPUT":
                            uc = str(uobj.get("content") or "")
                            if "# Session Continuation Handoff" in uc:
                                is_handoff_session = True
                            m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", uc, re.S)
                            clean_g = (m.group(1) if m else uc).strip()
                            if clean_g and not clean_g.startswith("# Session Continuation Handoff"):
                                last_user_goal = clean_g[:220]
                    except Exception:
                        pass
                    continue
                if '"PLANNER_RESPONSE"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") != "PLANNER_RESPONSE":
                    continue
                inp = int(obj.get("input_tokens") or 0)
                cr = int(obj.get("cache_read_tokens") or 0)
                out = int(obj.get("output_tokens") or 0)
                if inp == 0 and cr == 0 and out == 0:
                    continue
                turns += 1
                tot_in += inp
                tot_cr += cr
                tot_out += out
                cur_in, cur_cr, cur_tot = inp, cr, inp + cr
                if cur_tot > peak_ctx:
                    peak_ctx = cur_tot
                turn_tuples.append((inp, cr, out))
                if obj.get("model"):
                    last_model = str(obj["model"])
                pts = parse_iso_ts(str(obj.get("created_at") or ""))
                if pts:
                    if turns == 1:
                        first_ts = pts
                    last_ts = pts
                    turn_timestamps.append(pts)
                if turns > 1:
                    warm_in += inp
                    warm_cr += cr
                    if prev_cr >= 4000 and cr < prev_cr * 0.8:
                        drops += 1
                    if 2048 <= inp <= 2060 and cr >= 4000:
                        tail_2049 += 1
                prev_cr = cr
    except OSError:
        return None

    if turns == 0:
        return None

    wden = warm_in + warm_cr
    ttl_cliff = 90 if "hydrogen" in last_model.lower() else 150

    # Simulate counterfactual optimal rollover at 110k (only when >=12 turns remain)
    sim_in, sim_cr, sim_rollovers = _simulate_rollover_session(turn_tuples, 110_000)

    actual_quota = tot_in * W_IN + tot_cr * W_CR + tot_out * W_OUT
    sim_quota = sim_in * W_IN + sim_cr * W_CR + tot_out * W_OUT
    uncached_baseline_quota = (tot_in + tot_cr) * W_IN + tot_out * W_OUT

    res = {
        "conversation_id": tx.parents[2].name,
        "turns": turns,
        "model": last_model or "unknown",
        "cur_tot": cur_tot,
        "peak_ctx": peak_ctx,
        "cur_in": cur_in,
        "cur_cr": cur_cr,
        "tot_in": tot_in,
        "tot_cr": tot_cr,
        "tot_out": tot_out,
        "out_per_turn": tot_out // turns,
        "warm_hit_pct": round((warm_cr / wden) * 100, 1) if wden else 0.0,
        "drops": drops,
        "tail_2049": tail_2049,
        "img_reads": img_reads,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "turn_timestamps": turn_timestamps[-30:],
        "ttl_cliff_sec": ttl_cliff,
        "cache_burn_per_turn": int(cur_tot * W_CR),
        "actual_quota": actual_quota,
        "uncached_baseline_quota": uncached_baseline_quota,
        "sim_in": sim_in,
        "sim_cr": sim_cr,
        "sim_quota": sim_quota,
        "sim_rollovers": sim_rollovers,
        "is_handoff_session": is_handoff_session,
        "goal_preview": last_user_goal,
    }
    _tx_cache[key] = (st.st_mtime, st.st_size, res)
    if len(_tx_cache) > 150:
        oldest = sorted(_tx_cache.items(), key=lambda kv: kv[1][0])[:40]
        for ok, _ in oldest:
            _tx_cache.pop(ok, None)
    return res


def _simulate_rollover_session(
    turns: list[tuple[int, int, int]], cap: int = 110_000, rediscovery_tax: int = 8_000
) -> tuple[float, float, int]:
    """
    Counterfactual input + cache_read if rollover triggers at `cap` with >=12 turns left.
    Explicitly charges `22k cold start + rediscovery_tax (8k)` = 30k uncached input per rollover
    to account for post-rollover file re-reads, and carries that 30k base forward.
    """
    if not turns:
        return 0.0, 0.0, 0
    sim_in = 0.0
    sim_cr = 0.0
    sim_ctx = turns[0][0] + turns[0][1]
    prev_real_ctx = sim_ctx
    rollovers = 0
    n = len(turns)
    reset_base = 22_000 + rediscovery_tax
    for idx, (inp, cr, _out) in enumerate(turns):
        real_ctx = inp + cr
        delta = max(500, real_ctx - prev_real_ctx) if idx > 0 else 0
        was_drop = idx > 0 and cr < prev_real_ctx * 0.7 and prev_real_ctx >= 4000
        if idx == 0:
            sim_in += inp
            sim_cr += cr
            sim_ctx = real_ctx
        elif rollovers == 0:
            if real_ctx >= cap and (n - idx) >= 12:
                rollovers += 1
                sim_ctx = reset_base
                sim_in += reset_base
            else:
                sim_ctx = real_ctx
                sim_in += inp
                sim_cr += cr
        else:
            sim_ctx += delta
            if sim_ctx >= cap and (n - idx) >= 12:
                rollovers += 1
                sim_ctx = reset_base
                sim_in += reset_base
            elif was_drop:
                sim_in += sim_ctx
            else:
                sim_in += min(inp, delta + 1500)
                sim_cr += max(0, sim_ctx - delta)
        prev_real_ctx = real_ctx
    return sim_in, sim_cr, rollovers


def compute_adaptive_threshold(active: dict[str, Any] | None, cfg: dict[str, Any]) -> dict[str, Any]:
    """Compute the optimal token rollover cap dynamically for the current session."""
    tmode = cfg.get("threshold_mode", "auto")
    if tmode != "auto" and str(tmode).isdigit():
        cap_k = int(tmode)
        return {
            "cap_k": cap_k,
            "is_auto": False,
            "reason": f"Manual threshold fixed at {cap_k}k tokens.",
        }

    if not active:
        return {
            "cap_k": 125,
            "is_auto": True,
            "reason": "Auto-tuned baseline (125k) — adjusts live from cache hit rate & drops.",
        }

    drops = active.get("drops", 0)
    warm_hit = active.get("warm_hit_pct", 0.0)
    ttl = active.get("ttl_cliff_sec", 150)
    turns = max(1, active.get("turns", 1))
    growth_per_turn = max(0, (active.get("cur_tot", 0) - 18_000)) // turns

    if drops >= 2:
        cap_k = 100
        reason = f"Auto-tuned to 100k ({drops} cache drops — prevents expensive cold reloads)"
    elif growth_per_turn >= 7000:
        cap_k = 105
        reason = f"Auto-tuned to 105k (fast context growth ~{growth_per_turn // 1000}k/turn)"
    elif ttl <= 90 or drops == 1:
        cap_k = 110
        reason = "Auto-tuned to 110k (short TTL / cache drop risk — optimal break-even)"
    elif warm_hit >= 95.0 and drops == 0:
        cap_k = 145
        reason = f"Auto-tuned to 145k ({warm_hit}% warm cache, 0 drops — riding hot streak)"
    else:
        cap_k = 125
        reason = f"Auto-tuned to 125k ({warm_hit}% warm cache — balanced quota break-even)"

    return {"cap_k": cap_k, "is_auto": True, "reason": reason}


def load_events_summary() -> dict[str, Any]:
    global _ev_cache
    if not EVENTS_FILE.is_file():
        return {"events": 0, "saved_tok": 0, "replay_tok": 0, "by_cat": []}
    try:
        st = EVENTS_FILE.stat()
    except OSError:
        return {"events": 0, "saved_tok": 0, "replay_tok": 0, "by_cat": []}
    if _ev_cache[0] == st.st_mtime and _ev_cache[1] == st.st_size:
        return _ev_cache[2]

    cnt = saved_tok = 0
    by_cat: dict[str, dict[str, int]] = collections.defaultdict(lambda: {"n": 0, "tok": 0})
    try:
        with EVENTS_FILE.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                cnt += 1
                tk = int(ev.get("est_tokens_saved") or 0)
                saved_tok += tk
                k = f"{ev.get('source', '?')}:{ev.get('category', '?')}"
                by_cat[k]["n"] += 1
                by_cat[k]["tok"] += tk
    except OSError:
        pass

    top = sorted(
        [{"name": k, "n": v["n"], "tok": v["tok"]} for k, v in by_cat.items()],
        key=lambda x: x["tok"],
        reverse=True,
    )[:8]
    summary = {"events": cnt, "saved_tok": saved_tok, "replay_tok": saved_tok * 6, "by_cat": top}
    _ev_cache = (st.st_mtime, st.st_size, summary)
    return summary


def compute_savings_analytics(
    all_sessions: list[dict[str, Any]],
    hooks: dict[str, Any],
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Honest, exact token & quota savings breakdown across all tracked sessions:
      1. Realized Savings (Prompt Cache + Hook Compression + Logged Rollovers)
      2. Smart Rollover Impact (Measured from real transcript trajectories by session tier)
      3. Per-Type breakdown: Input, Cache, Output, Total Quota (% less usage & +% extra capacity)
    """
    n_sess = len(all_sessions)
    tot_turns = sum(s["turns"] for s in all_sessions)
    tot_in = sum(s["tot_in"] for s in all_sessions)
    tot_cr = sum(s["tot_cr"] for s in all_sessions)
    tot_out = sum(s["tot_out"] for s in all_sessions)
    tot_drops = sum(s["drops"] for s in all_sessions)

    actual_quota = sum(s["actual_quota"] for s in all_sessions)
    uncached_baseline = sum(s["uncached_baseline_quota"] for s in all_sessions)

    # Hook savings (direct input saved + 6x cache replay avoided)
    hook_in_saved = int(hooks.get("saved_tok") or 0)
    hook_cr_saved = int(hooks.get("replay_tok") or 0)

    # Logged rollover savings (from actual rollover history entries)
    logged_rollovers = [h for h in history if h.get("kind") in ("auto", "manual")]
    logged_in_saved = sum(int(h.get("est_input_saved") or 0) for h in logged_rollovers)
    logged_cr_saved = sum(int(h.get("est_cache_saved") or 0) for h in logged_rollovers)

    # Output comparison: compare newer Token-Saver sessions (post-install or handoff-era) vs older baseline
    # Split chronologically: older half vs newer half (or sessions with/without concise rule)
    chrono = sorted(all_sessions, key=lambda s: s["last_ts"])
    out_saved_tok = 0
    out_saved_pct = 0.0
    out_note = "Measured across all turns"
    if len(chrono) >= 6:
        # Compare last 10 sessions (with Token Saver active) against prior baseline
        recent_slice = chrono[-min(12, len(chrono) // 2):]
        older_slice = chrono[:-len(recent_slice)]
        older_turns = sum(s["turns"] for s in older_slice)
        recent_turns = sum(s["turns"] for s in recent_slice)
        if older_turns > 0 and recent_turns > 0:
            old_avg_out = sum(s["tot_out"] for s in older_slice) / older_turns
            new_avg_out = sum(s["tot_out"] for s in recent_slice) / recent_turns
            if old_avg_out > 0:
                delta_per_turn = old_avg_out - new_avg_out
                out_saved_pct = round((delta_per_turn / old_avg_out) * 100.0, 1)
                out_saved_tok = int(max(0.0, delta_per_turn * recent_turns))
                out_note = f"{int(new_avg_out)} vs {int(old_avg_out)} tok/turn baseline"

    # Smart rollover counterfactual across all sessions (what Smart Auto saves on long sessions)
    sim_in_total = sum(s["sim_in"] for s in all_sessions)
    sim_cr_total = sum(s["sim_cr"] for s in all_sessions)
    sim_quota_total = sum(s["sim_quota"] for s in all_sessions)

    ro_in_saved = max(0, int(tot_in - sim_in_total))
    ro_cr_saved = max(0, int(tot_cr - sim_cr_total))
    ro_quota_saved = max(0.0, actual_quota - sim_quota_total)

    # Combined Token-Saver + Rollover savings per token class
    input_saved_tok = ro_in_saved + hook_in_saved + logged_in_saved
    input_denom = tot_in + hook_in_saved + logged_in_saved
    input_saved_pct = round((input_saved_tok / input_denom) * 100.0, 1) if input_denom > 0 else 0.0

    cache_saved_tok = ro_cr_saved + hook_cr_saved + logged_cr_saved
    cache_denom = tot_cr + hook_cr_saved + logged_cr_saved
    cache_saved_pct = round((cache_saved_tok / cache_denom) * 100.0, 1) if cache_denom > 0 else 0.0

    # Prompt caching itself saves 95% of cache-read tokens vs paying full input price
    cache_discount_quota_saved = tot_cr * (W_IN - W_CR)
    cache_discount_pct = (
        round((cache_discount_quota_saved / uncached_baseline) * 100.0, 1)
        if uncached_baseline > 0 else 0.0
    )

    # Total Token-Saver Quota Reduction (Rollover + Hooks + Output reduction)
    extra_quota_saved = (
        ro_quota_saved
        + hook_in_saved * W_IN
        + hook_cr_saved * W_CR
        + logged_in_saved * W_IN
        + logged_cr_saved * W_CR
        + max(0, out_saved_tok) * W_OUT
    )
    quota_base = actual_quota + hook_in_saved * W_IN + hook_cr_saved * W_CR + logged_in_saved * W_IN + logged_cr_saved * W_CR + max(0, out_saved_tok) * W_OUT
    less_usage_pct = round((extra_quota_saved / quota_base) * 100.0, 1) if quota_base > 0 else 0.0
    less_usage_pct = max(0.0, min(95.0, less_usage_pct))
    # Effective more usable token capacity from same quota: 1 / (1 - r) - 1
    more_capacity_pct = round(((1.0 / (1.0 - less_usage_pct / 100.0)) - 1.0) * 100.0, 1) if less_usage_pct < 95.0 else 0.0

    total_tokens_saved = input_saved_tok + cache_saved_tok + max(0, out_saved_tok)
    total_tokens_base = input_denom + cache_denom + tot_out + max(0, out_saved_tok)
    total_tokens_saved_pct = round((total_tokens_saved / total_tokens_base) * 100.0, 1) if total_tokens_base > 0 else 0.0

    # Tier breakdown (Short / Medium / Long / Very Long / Extreme)
    tier_defs = [
        ("Short (<75k)", lambda s: s["peak_ctx"] < 75_000),
        ("Medium (75k–110k)", lambda s: 75_000 <= s["peak_ctx"] < 110_000),
        ("Long (110k–200k)", lambda s: 110_000 <= s["peak_ctx"] < 200_000),
        ("Very long (>200k)", lambda s: 200_000 <= s["peak_ctx"] < 400_000),
        ("Extreme (>400k)", lambda s: s["peak_ctx"] >= 400_000),
    ]
    tiers = []
    for label, pred in tier_defs:
        sub = [s for s in all_sessions if pred(s)]
        if not sub:
            tiers.append({
                "label": label, "sessions": 0, "avg_peak": 0, "avg_turns": 0,
                "quota_saved_pct": 0.0, "more_capacity_pct": 0.0, "in_cache_saved_pct": 0.0,
            })
            continue
        a_q = sum(s["actual_quota"] for s in sub)
        s_q = sum(s["sim_quota"] for s in sub)
        a_ic = sum(s["tot_in"] * W_IN + s["tot_cr"] * W_CR for s in sub)
        s_ic = sum(s["sim_in"] * W_IN + s["sim_cr"] * W_CR for s in sub)
        q_pct = round((1.0 - s_q / a_q) * 100.0, 1) if a_q > 0 else 0.0
        ic_pct = round((1.0 - s_ic / a_ic) * 100.0, 1) if a_ic > 0 else 0.0
        cap_gain = round(((1.0 / (1.0 - q_pct / 100.0)) - 1.0) * 100.0, 1) if q_pct < 95.0 else 0.0
        tiers.append({
            "label": label,
            "sessions": len(sub),
            "avg_peak": int(sum(s["peak_ctx"] for s in sub) / len(sub)),
            "avg_turns": round(sum(s["turns"] for s in sub) / len(sub), 1),
            "quota_saved_pct": q_pct,
            "more_capacity_pct": cap_gain,
            "in_cache_saved_pct": ic_pct,
        })

    return {
        "sessions_count": n_sess,
        "turns_total": tot_turns,
        "drops_total": tot_drops,
        "totals": {
            "total_tokens_saved": total_tokens_saved,
            "total_tokens_saved_pct": total_tokens_saved_pct,
            "avg_quota_saved_pct": less_usage_pct,
            "less_usage_pct": less_usage_pct,
            "more_capacity_pct": more_capacity_pct,
            "quota_units_saved": int(extra_quota_saved),
            "actual_quota_units": int(actual_quota),
            "cache_discount_quota_saved": int(cache_discount_quota_saved),
            "cache_discount_pct": cache_discount_pct,
        },
        "breakdown": {
            "input": {
                "used": tot_in,
                "saved": input_saved_tok,
                "saved_pct": input_saved_pct,
            },
            "cache": {
                "used": tot_cr,
                "saved": cache_saved_tok,
                "saved_pct": cache_saved_pct,
                "hit_rate_pct": round((tot_cr / (tot_in + tot_cr)) * 100.0, 1) if (tot_in + tot_cr) else 0.0,
            },
            "output": {
                "used": tot_out,
                "saved": max(0, out_saved_tok),
                "saved_pct": out_saved_pct,
                "avg_per_turn": (tot_out // tot_turns) if tot_turns else 0,
                "note": out_note,
            },
            "quota": {
                "used_units": int(actual_quota),
                "saved_units": int(extra_quota_saved),
                "saved_pct": less_usage_pct,
                "more_capacity_pct": more_capacity_pct,
            },
        },
        "tiers": tiers,
        "logged_rollovers_count": len(logged_rollovers),
    }


def _find_agentapi_cmd() -> list[str] | None:
    exe = os.environ.get("ANTIGRAVITY_AGENTAPI_EXE")
    if exe and Path(exe).is_file():
        return [exe, "agentapi"]
    shim = Path.home() / ".gemini" / "antigravity" / "bin" / "agentapi"
    if shim.is_file():
        return [str(shim)]
    return None


def build_handoff_packet(ws: str, conv_id: str = "") -> dict[str, Any]:
    if not ws or not Path(ws).is_dir():
        ws = str(Path.home())
    cmd = [sys.executable, str(PLUGIN_ROOT / "scripts" / "handoff_pack.py"), ws]
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
        base_cmd = _find_agentapi_cmd()
        if not base_cmd:
            return {"ok": False, "error": "agentapi binary unavailable; handoff saved to archive instead."}
        cmd = [*base_cmd, "new-conversation"]
        if model and model != "inherit":
            cmd.extend(["--model", model])
        cmd.append(msg)
        p = subprocess.run(cmd, text=True, capture_output=True, timeout=18)
        if p.returncode != 0:
            return {"ok": False, "error": p.stderr.strip() or f"agentapi exit {p.returncode}"}
        raw_out = p.stdout.strip()
        try:
            parsed = json.loads(raw_out) if raw_out else {}
            to_conv = str(
                parsed.get("conversationId")
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

    if BRAIN_DIR.is_dir():
        cands = []
        for p in BRAIN_DIR.glob("*/.system_generated/logs/transcript.jsonl"):
            try:
                cands.append((p.stat().st_mtime, p))
            except OSError:
                continue
        cands.sort(reverse=True)

        if conv_id:
            target = BRAIN_DIR / conv_id / ".system_generated/logs/transcript.jsonl"
            if target.is_file():
                active = parse_transcript_cached(target)

        for _, p in cands[:120]:
            rec = parse_transcript_cached(p)
            if not rec:
                continue
            if active is None:
                active = rec
            all_sessions.append(rec)

    cfg = load_config()
    adaptive = compute_adaptive_threshold(active, cfg)

    if active:
        age = max(0, int(now - active["last_ts"]))
        recent_10m = sum(1 for t in active.get("turn_timestamps", []) if (now - t) <= 600)
        active = {
            **{k: v for k, v in active.items() if k != "turn_timestamps"},
            "idle_sec": age,
            "ttl_remaining_sec": max(0, active["ttl_cliff_sec"] - age),
            "recent_turns_10m": recent_10m,
        }

    hooks = load_events_summary()
    history = load_rollover_history()
    savings = compute_savings_analytics(all_sessions, hooks, history)

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
        "last_auto_event": _last_auto_event,
        "can_new_chat": bool(_find_agentapi_cmd()),
        "workspace": os.environ.get("ANTIGRAVITY_WORKSPACE_PATH", str(Path.cwd())),
        "updates": get_update_snapshot(),
    }


def _parse_semver(v: str) -> tuple[int, int, int]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", str(v or ""))
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def get_installed_version() -> str:
    pj = PLUGIN_ROOT / "plugin.json"
    if pj.is_file():
        try:
            d = json.loads(pj.read_text(encoding="utf-8"))
            if d.get("version"):
                return str(d["version"]).lstrip("v")
        except Exception:
            pass
    return "1.0.0"


def get_local_changelog() -> str:
    cl = PLUGIN_ROOT / "CHANGELOG.md"
    if cl.is_file():
        try:
            return cl.read_text(encoding="utf-8")[:12000]
        except OSError:
            pass
    return ""


def check_requirements() -> list[dict[str, Any]]:
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = (sys.version_info.major, sys.version_info.minor) >= (3, 9)

    perl_ok = False
    perl_ver = "Not found"
    try:
        p = subprocess.run(["perl", "-e", "print $^V"], text=True, capture_output=True, timeout=3)
        if p.returncode == 0 and p.stdout.strip():
            perl_ok = True
            perl_ver = p.stdout.strip()
    except Exception:
        pass

    plugin_writable = os.access(PLUGIN_ROOT, os.W_OK)
    data_exists = DATA_DIR.is_dir() or os.access(DATA_DIR.parent, os.W_OK)
    return [
        {
            "name": "Python 3.9+",
            "status": "ok" if py_ok else "bad",
            "detail": f"Python {py_ver}",
            "required": True,
        },
        {
            "name": "Perl 5 (fast ~2ms hooks)",
            "status": "ok" if perl_ok else "warn",
            "detail": perl_ver,
            "required": True,
        },
        {
            "name": "Plugin folder writable",
            "status": "ok" if plugin_writable else "bad",
            "detail": str(PLUGIN_ROOT),
            "required": True,
        },
        {
            "name": "Statistics & archive preserved",
            "status": "ok" if data_exists else "warn",
            "detail": f"Stored separately in {DATA_DIR} (never overwritten)",
            "required": False,
        },
    ]


def get_update_snapshot() -> dict[str, Any]:
    """Fast local + cached update snapshot (never blocks build_state on network)."""
    installed = get_installed_version()
    base = {
        "repo": GITHUB_REPO,
        "releases_url": GITHUB_RELEASES_URL,
        "installed_version": installed,
        "latest_version": installed,
        "update_available": False,
        "release_notes": "",
        "published_at": "",
        "tarball_url": "",
        "local_changelog": get_local_changelog(),
        "requirements": check_requirements(),
        "checked_at": 0,
        "error": "",
    }
    if _update_cache[1]:
        merged = {**base, **_update_cache[1], "installed_version": installed, "requirements": base["requirements"]}
        merged["update_available"] = _parse_semver(merged.get("latest_version", installed)) > _parse_semver(installed)
        return merged
    return base


def parse_sha256sums(text: str, filename: str) -> str | None:
    """Extract expected SHA-256 hex digest for `filename` from a SHA256SUMS.txt file."""
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2:
            digest = parts[0].lower()
            name = parts[-1].lstrip("*")
            if name == filename and re.fullmatch(r"[0-9a-f]{64}", digest):
                return digest
    return None


def verify_sha256_bytes(blob: bytes, expected_hex: str) -> bool:
    actual = hashlib.sha256(blob).hexdigest().lower()
    return actual == expected_hex.strip().lower()


def check_github_updates(force: bool = False) -> dict[str, Any]:
    global _update_cache
    now = time.time()
    if not force and _update_cache[1] and (now - _update_cache[0]) < 600:
        return get_update_snapshot()

    installed = get_installed_version()
    req = urllib.request.Request(
        GITHUB_RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"agy-tokensaver/{installed}",
        },
    )
    info: dict[str, Any] = {
        "latest_version": installed,
        "tag_name": "",
        "release_notes": "",
        "published_at": "",
        "asset_name": "",
        "asset_url": "",
        "checksum_url": "",
        "checked_at": int(now),
        "error": "",
    }
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        tag_raw = str(data.get("tag_name") or "").strip()
        if not tag_raw or not re.match(r"^v?\d+\.\d+\.\d+", tag_raw):
            info["error"] = "Latest release has no valid semver tag."
        else:
            info["tag_name"] = tag_raw
            info["latest_version"] = tag_raw.lstrip("v")
            info["release_notes"] = str(data.get("body") or "")
            info["published_at"] = str(data.get("published_at") or "")[:10]
            info["releases_url"] = str(data.get("html_url") or GITHUB_RELEASES_URL)
            for asset in data.get("assets") or []:
                aname = str(asset.get("name") or "")
                aurl = str(asset.get("browser_download_url") or "")
                if aname.endswith(".zip") and aname.startswith("agy-tokensaver-"):
                    info["asset_name"] = aname
                    info["asset_url"] = aurl
                elif aname == "SHA256SUMS.txt":
                    info["checksum_url"] = aurl
    except urllib.error.HTTPError as e:
        if e.code == 404:
            info["error"] = "No published GitHub release found yet."
        else:
            info["error"] = f"GitHub API HTTP {e.code}"
    except Exception as e:
        info["error"] = f"Could not reach GitHub ({e})"

    _update_cache = (now, info)
    return get_update_snapshot()


def apply_update() -> dict[str, Any]:
    """Download latest tagged release ZIP, verify SHA-256 checksum, and update files while preserving plugin_data."""
    info = check_github_updates(force=True)
    if info.get("error"):
        return {"ok": False, "error": info["error"], "updates": info}
    if not info.get("tag_name") or not info.get("asset_url") or not info.get("checksum_url"):
        return {
            "ok": False,
            "error": "Update refused: latest tagged release is missing a release ZIP or SHA256SUMS.txt (unverified main branch updates are disabled).",
            "updates": info,
        }

    installed_before = info["installed_version"]
    asset_name = info["asset_name"]
    ua = {"User-Agent": f"agy-tokensaver/{installed_before}"}

    try:
        with urllib.request.urlopen(urllib.request.Request(info["checksum_url"], headers=ua), timeout=12) as cr:
            sums_text = cr.read().decode("utf-8", errors="replace")
        expected_sha = parse_sha256sums(sums_text, asset_name)
        if not expected_sha:
            return {"ok": False, "error": f"SHA256SUMS.txt does not contain a checksum for {asset_name}.", "updates": info}

        with urllib.request.urlopen(urllib.request.Request(info["asset_url"], headers=ua), timeout=30) as ar:
            zip_bytes = ar.read()

        if not verify_sha256_bytes(zip_bytes, expected_sha):
            return {"ok": False, "error": f"SHA-256 checksum mismatch for {asset_name}; update aborted.", "updates": info}

        with tempfile.TemporaryDirectory(prefix="agy-ts-upd-") as td:
            zip_path = Path(td) / asset_name
            zip_path.write_bytes(zip_bytes)

            extract_dir = Path(td) / "src"
            extract_dir.mkdir()
            with zipfile.ZipFile(zip_path, "r") as zf:
                for zi in zf.infolist():
                    p = Path(zi.filename)
                    if p.is_absolute() or ".." in p.parts:
                        continue
                    zf.extract(zi, extract_dir)

            roots = [p for p in extract_dir.iterdir() if p.is_dir()]
            src_root = roots[0] if len(roots) == 1 else extract_dir
            if not (src_root / "plugin.json").is_file():
                return {"ok": False, "error": "Invalid release archive (missing plugin.json).", "updates": info}

            # Copy updated plugin files into PLUGIN_ROOT while NEVER touching .git or plugin_data
            preserve = {".git", ".data", "__pycache__"}
            for item in src_root.iterdir():
                if item.name in preserve:
                    continue
                dest = PLUGIN_ROOT / item.name
                if item.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)

            # On native Windows, pin real Python executable to avoid Microsoft Store python3 stub
            if os.name == "nt":
                mcp_cfg = PLUGIN_ROOT / "mcp_config.json"
                if mcp_cfg.is_file():
                    try:
                        cfg_data = json.loads(mcp_cfg.read_text(encoding="utf-8"))
                        srv_cfg = cfg_data.get("mcpServers", {}).get("token_saver_ui")
                        if isinstance(srv_cfg, dict):
                            srv_cfg["command"] = sys.executable
                            mcp_cfg.write_text(json.dumps(cfg_data, indent=2) + "\n", encoding="utf-8")
                    except Exception:
                        pass
            else:
                for pat in ("scripts/*.pl", "scripts/*.py", "mcp_app/*.py", "tests/*.sh", "install.py", "install.sh", "Install-TokenSaver.command"):
                    for fp in PLUGIN_ROOT.glob(pat):
                        try:
                            fp.chmod(fp.stat().st_mode | 0o111)
                        except OSError:
                            pass

        # Best-effort live MCP server refresh inside Antigravity
        ls_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
        csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
        if ls_addr and csrf:
            try:
                rreq = urllib.request.Request(
                    f"http://{ls_addr}/exa.language_server_pb.LanguageServerService/RefreshMcpServers",
                    data=json.dumps({"serverName": "token-saver_token_saver_ui"}).encode("utf-8"),
                    headers={"Content-Type": "application/json", "x-codeium-csrf-token": csrf},
                    method="POST",
                )
                urllib.request.urlopen(rreq, timeout=3).read()
            except Exception:
                pass

        new_info = check_github_updates(force=True)
        return {
            "ok": True,
            "from_version": installed_before,
            "to_version": new_info["installed_version"],
            "sha256": expected_sha[:16],
            "stats_preserved": True,
            "updates": new_info,
        }
    except Exception as e:
        return {"ok": False, "error": f"Update failed: {e}", "updates": info}


def run_smart_rollover_tick() -> None:
    """Single anti-spam adaptive rollover evaluation tick."""
    global _last_auto_event
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
    cap = cap_k * 1000
    now = int(time.time())

    # Anti-spam & break-even guardrails:
    # 1. Context must exceed adaptive cap
    if act["cur_tot"] < cap:
        return
    # 2. Must be a sustained multi-turn session (never roll over tiny bursts)
    if act["turns"] < cfg.get("min_turns", 14):
        return
    # 3. Must have active momentum (>=3 turns in last 10m) — avoids spawning when user is already done!
    if act.get("recent_turns_10m", 0) < 3:
        return
    # 4. Settled idle window (20s..90s): don't interrupt active turn, don't touch old chats
    if not (20 <= act["idle_sec"] <= 90):
        return

    with _state_lock:
        rstate = _load_rollover_state()
        if cid in rstate["rolled_convs"]:
            return
        cooldown_sec = cfg.get("cooldown_min", 20) * 60
        if now - rstate.get("last_rollover_ts", 0) < cooldown_sec:
            return
        rstate["rolled_convs"][cid] = now
        rstate["last_rollover_ts"] = now
        _save_rollover_state(rstate)

    spawn = (mode == "smart_auto") and st.get("can_new_chat", False)
    kind = "auto" if spawn else "snapshot"
    res = perform_rollover(
        ws=st["workspace"],
        active=act,
        kind=kind,
        model=cfg.get("model", "inherit"),
        spawn_chat=spawn,
        cap_k=cap_k,
    )
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
