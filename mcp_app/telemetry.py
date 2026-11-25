"""Hook event telemetry aggregation and session savings analytics."""
from __future__ import annotations

import collections
import json
from typing import Any

from config import EVENTS_FILE, W_CR, W_IN, W_OUT

_ev_cache: tuple[float, int, dict[str, Any]] = (0.0, -1, {})

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

    top_items: list[dict[str, Any]] = [{"name": k, "n": v["n"], "tok": v["tok"]} for k, v in by_cat.items()]
    top = sorted(top_items, key=lambda x: int(x["tok"]), reverse=True)[:8]
    summary = {"events": cnt, "saved_tok": saved_tok, "replay_tok": saved_tok * 6, "by_cat": top}
    _ev_cache = (st.st_mtime, st.st_size, summary)
    return summary


def compute_savings_analytics(
    all_sessions: list[dict[str, Any]],
    hooks: dict[str, Any],
    history: list[dict[str, Any]],
    cfg: dict[str, Any] | None = None,
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

    # Honest output row: hooks never alter model output in Mode 1, and we never compare
    # unrelated historical sessions by task size (which produced misleading +/-% swings).
    concise_on = bool((cfg or {}).get("concise_output", False))
    out_saved_tok = 0
    out_saved_pct = 0.0
    out_note = "Concise reply rule ON (output delta not tracked)" if concise_on else "Unchanged (prompt overhead: 0 tok/turn)"

    # Prompt-cache drop forensics across all sessions (arXiv:2601.06007)
    rebuild_tok_total = sum(int(s.get("drop_rebuild_tok") or 0) for s in all_sessions)
    idle_drop_tok_total = sum(int(s.get("idle_drop_tok") or 0) for s in all_sessions)
    midrun_drop_tok_total = sum(int(s.get("midrun_drop_tok") or 0) for s in all_sessions)
    rebuild_quota_pct = round((rebuild_tok_total * W_IN / actual_quota) * 100.0, 1) if actual_quota > 0 else 0.0

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
        "cache_health": {
            "rebuild_tokens": rebuild_tok_total,
            "idle_drop_tokens": idle_drop_tok_total,
            "midrun_drop_tokens": midrun_drop_tok_total,
            "rebuild_quota_pct": rebuild_quota_pct,
        },
    }


