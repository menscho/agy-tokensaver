"""Multi-mode comparison, per-session distributions, session-type breakdown, and weight sensitivity."""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

def run_modes(policy: str = "auto", json_out: str | None = None, report: bool = False,
              weights: tuple[float, float, float] | None = None) -> dict:
    """Compare counterfactual replay estimates across modes on local sessions (0 tok/turn passive overhead)."""
    from transcript_replay import W_CR, W_IN, W_OUT, _percentile, run
    wi, wc, wo = weights if weights else (W_IN, W_CR, W_OUT)
    base = run(quiet=True, policy=policy, extra_env={"AGY_TS_COMPACT_READS": "0", "AGY_TS_INCLUDE_RULES": "0"}, weights=(wi, wc, wo))
    comp = run(quiet=True, policy=policy, extra_env={"AGY_TS_COMPACT_READS": "1", "AGY_TS_INCLUDE_RULES": "0"}, weights=(wi, wc, wo))

    tot_in = sum(r["tok_0"][0] for r in base["rows"])
    tot_cr = sum(r["tok_0"][1] for r in base["rows"])
    tot_out = sum(r["tok_0"][2] for r in base["rows"])
    q_tot = tot_in * wi + tot_cr * wc + tot_out * wo
    out_share = (tot_out * wo / q_tot * 100.0) if q_tot else 0.0

    b_h = base["q_hooks"]["total"]
    b_r = base["q_both"]["total"]
    b_d = base["q_both_direct"]["total"]
    c_h = comp["q_hooks"]["total"]
    c_live = comp["q_both"]["total"]
    b_ub = base["q_both_ub"]["total"]
    c_ub = comp["q_both_ub"]["total"]
    ho_tag = "Path 2 Delegated" if base.get("delegated_live") else "Path 1 Direct"

    print(f"=== Real-Session Replay Estimate ({base['n']} sessions, {base['turns']:,} turns, {q_tot/1e6:.1f}M proxy units) ===")
    print(f"Cost proxy ({wi:g}x in + {wc:g}x cache + {wo:g}x out): Input {tot_in*wi/q_tot*100:.1f}% | Cache {tot_cr*wc/q_tot*100:.1f}% | Output {out_share:.1f}% | Overhead: {base['overhead_tok_per_turn']} tok/turn")
    print(f"Positive numbers = estimated cost saved. Guard savings replay read/find/grep hooks; Auto-Handoff models PreInvocation ({ho_tag}).\n")
    print(f"{'Mode':44s} | {'Tool Tok Cut':>12s} | {'Median':>8s} | {'Est Cost Saved':>14s}")
    print("-" * 88)
    print(f"{'Auto-Handoff + Read Guards (Default)':44s} | {base['saved_tool_tok']:9,d} tk | {base['q_both']['median']:7.1f}% | {b_r:13.1f}%")
    if base.get("delegated_live"):
        print(f"{'Auto-Handoff + Read Guards (Direct Path 1)':44s} | {base['saved_tool_tok']:9,d} tk | {base['q_both_direct']['median']:7.1f}% | {b_d:13.1f}%")
    print(f"{'Auto-Handoff + Compact Reads':44s} | {comp['saved_tool_tok']:9,d} tk | {comp['q_both']['median']:7.1f}% | {c_live:13.1f}%")
    print(f"{'Guards Only (Same Chat)':44s} | {base['saved_tool_tok']:9,d} tk | {base['q_hooks']['median']:7.1f}% | {b_h:13.1f}%")
    print(f"{'Guards + Compact Reads (Same Chat)':44s} | {comp['saved_tool_tok']:9,d} tk | {comp['q_hooks']['median']:7.1f}% | {c_h:13.1f}%")
    print(f"{'Auto-Handoff Upper Bound (phase resets)':44s} | {base['saved_tool_tok']:9,d} tk | {base['q_both_ub']['median']:7.1f}% | {b_ub:13.1f}% ({c_ub:.1f}% cr)")
    print("-" * 88)
    print("Note: Replay estimate from historical transcripts (charges retries + +6k/rollover tax; assumes same trajectory post-handoff).")
    print("      Live end-to-end A/B and multi-module pass/token measurements live in eval/results/ab_long_eval.json and hard_eval.json.")

    def _dist_entry(label: str, stats_obj: dict) -> dict:
        return {
            "mode": label, "type": "replay",
            "avg": round(stats_obj["total"], 1),
            "median": round(stats_obj["median"], 1),
            "p25": round(stats_obj["p25"], 1),
            "p75": round(stats_obj["p75"], 1),
            "p90": round(stats_obj["p90"], 1),
            "max": round(stats_obj["max"], 1),
            "pct_gt_5": round(stats_obj["pct_gt_5"], 1),
            "per_session": stats_obj["per_session"],
        }

    distributions = [
        _dist_entry("Guards Only (Same Chat)", base["q_hooks"]),
        _dist_entry("Guards + Compact Reads", comp["q_hooks"]),
        _dist_entry("Auto-Handoff + Read Guards (Default)", base["q_both"]),
        _dist_entry("Auto-Handoff + Compact Reads", comp["q_both"]),
    ]

    # Per-session-type breakdown
    by_type: list[dict[str, Any]] = []
    for stype in ("test-heavy", "log-heavy", "plain-editing"):
        b_sub = [r for r in base["rows"] if r["session_type"] == stype]
        c_sub = [r for r in comp["rows"] if r["session_type"] == stype]
        if not b_sub:
            continue
        def _agg(sub: list[dict], fld: str) -> tuple[float, float, float]:
            q0s = sum(r["q0"] for r in sub)
            q1s = sum(r[fld] for r in sub)
            avg = (1.0 - q1s / q0s) * 100.0 if q0s else 0.0
            per = sorted((1.0 - r[fld] / r["q0"]) * 100.0 for r in sub if r["q0"])
            return (round(avg, 1), round(statistics.median(per), 1) if per else 0.0, round(_percentile(per, 0.90), 1))
        g_avg, g_med, g_p90 = _agg(b_sub, "q_hooks")
        ho_avg, ho_med, ho_p90 = _agg(b_sub, "q_both")
        hc_avg, _, _ = _agg(c_sub, "q_both")
        by_type.append({
            "type": stype, "n": len(b_sub),
            "g_avg": g_avg, "g_median": g_med, "g_p90": g_p90,
            "ho_avg": ho_avg, "ho_median": ho_med, "ho_p90": ho_p90, "hc_avg": hc_avg,
        })

    # Cost-weight sensitivity analysis (computed exactly from recorded token sums)
    regimes = [
        ("Default proxy (1 / 0.05 / 6)", (1.0, 0.05, 6.0)),
        ("Anthropic-style (1 / 0.10 / 5)", (1.0, 0.10, 5.0)),
        ("Output-light (1 / 0.05 / 3)", (1.0, 0.05, 3.0)),
        ("Cache-heavy (1 / 0.25 / 5)", (1.0, 0.25, 5.0)),
    ]
    if (wi, wc, wo) not in [w for _, w in regimes]:
        regimes.append((f"Custom ({wi:g} / {wc:g} / {wo:g})", (wi, wc, wo)))
    sensitivity: list[dict[str, Any]] = []
    for rname, (rwi, rwc, rwo) in regimes:
        def _qsum(rows_list: list[dict], tok_key: str) -> float:
            return sum(r[tok_key][0] * rwi + r[tok_key][1] * rwc + r[tok_key][2] * rwo for r in rows_list)
        q0_b = _qsum(base["rows"], "tok_0")
        q0_c = _qsum(comp["rows"], "tok_0")
        g_r = (1.0 - _qsum(base["rows"], "tok_hooks") / q0_b) * 100.0 if q0_b else 0.0
        gc_r = (1.0 - _qsum(comp["rows"], "tok_hooks") / q0_c) * 100.0 if q0_c else 0.0
        ho_r = (1.0 - _qsum(base["rows"], "tok_both") / q0_b) * 100.0 if q0_b else 0.0
        hc_r = (1.0 - _qsum(comp["rows"], "tok_both") / q0_c) * 100.0 if q0_c else 0.0
        sensitivity.append({
            "regime": rname, "weights": [rwi, rwc, rwo],
            "guards": round(g_r, 1), "compact_guards": round(gc_r, 1), "handoff": round(ho_r, 1), "handoff_compact": round(hc_r, 1),
        })

    if report:
        print("\n=== Per-Session Distribution (Positive = Cost Saved) ===")
        print(f"{'Mode':38s} | {'Avg':>7s} | {'Median':>7s} | {'p90':>7s} | {'Max':>7s} | {'>5% share':>9s}")
        print("-" * 88)
        for d in distributions:
            print(f"{d['mode']:38s} | {d['avg']:6.1f}% | {d['median']:6.1f}% | {d['p90']:6.1f}% | {d['max']:6.1f}% | {d['pct_gt_5']:8.1f}%")

        print("\n=== Breakdown by Session Type (Cost Saved %) ===")
        print(f"{'Session Type':16s} | {'n':>3s} | {'Guards Avg':>10s} | {'Guards p90':>10s} | {'Handoff Avg':>11s} | {'Handoff p90':>11s} | {'+Compact Avg':>12s}")
        print("-" * 92)
        for t in by_type:
            print(f"{t['type']:16s} | {t['n']:3d} | {t['g_avg']:9.1f}% | {t['g_p90']:9.1f}% | {t['ho_avg']:10.1f}% | {t['ho_p90']:10.1f}% | {t['hc_avg']:11.1f}%")

        print("\n=== Cost-Weight Sensitivity (Replay Estimate Saved %) ===")
        print(f"{'Weight Regime (in/cache/out)':32s} | {'Guards':>8s} | {'+Compact':>8s} | {'Handoff':>8s} | {'+Compact':>8s}")
        print("-" * 76)
        for s_row in sensitivity:
            print(f"{s_row['regime']:32s} | {s_row['guards']:7.1f}% | {s_row['compact_guards']:7.1f}% | {s_row['handoff']:7.1f}% | {s_row['handoff_compact']:7.1f}%")

    res = {
        "n": base["n"],
        "turns": base["turns"],
        "quota_m": round(q_tot / 1e6, 2),
        "weights": [wi, wc, wo],
        "overhead_tok_per_turn": base["overhead_tok_per_turn"],
        "quota_mix": {
            "input_pct": round(tot_in * wi / q_tot * 100.0, 1),
            "cache_pct": round(tot_cr * wc / q_tot * 100.0, 1),
            "output_pct": round(out_share, 1),
        },
        "base": base,
        "compact_reads": comp,
        "distributions": distributions,
        "by_type": by_type,
        "sensitivity": sensitivity,
    }
    if json_out:
        Path(json_out).write_text(json.dumps(res, indent=2), encoding="utf-8")
    return res


