"""Multi-mode comparison, per-session distributions, session-type breakdown, and weight sensitivity."""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

def run_modes(policy: str = "auto", json_out: str | None = None, report: bool = False,
              weights: tuple[float, float, float] | None = None) -> dict:
    """Compare Mode 1 (Lossless) vs Mode 2 (Opt-in) on all local sessions with distributions & sensitivity."""
    from transcript_replay import W_CR, W_IN, W_OUT, _percentile, run
    wi, wc, wo = weights if weights else (W_IN, W_CR, W_OUT)
    base = run(quiet=True, policy=policy, extra_env={"AGY_TS_COMPACT_READS": "0"}, weights=(wi, wc, wo))
    comp = run(quiet=True, policy=policy, extra_env={"AGY_TS_COMPACT_READS": "1"}, weights=(wi, wc, wo))

    tot_in = sum(r["tok_0"][0] for r in base["rows"])
    tot_cr = sum(r["tok_0"][1] for r in base["rows"])
    tot_out = sum(r["tok_0"][2] for r in base["rows"])
    q_tot = tot_in * wi + tot_cr * wc + tot_out * wo
    out_share = (tot_out * wo / q_tot * 100.0) if q_tot else 0.0

    c20 = round(out_share * 0.20, 1)
    c35 = round(out_share * 0.35, 1)

    b_h = -base["q_hooks"]["total"]
    c_h = -comp["q_hooks"]["total"]
    c_live = -comp["q_both"]["total"]
    c_ub = -comp["q_both_ub"]["total"]

    print(f"=== Real-Session Benchmark ({base['n']} sessions, {base['turns']:,} turns, {q_tot/1e6:.1f}M proxy units) ===")
    print(f"Cost proxy ({wi:g}x in + {wc:g}x cache + {wo:g}x out): Input {tot_in*wi/q_tot*100:.1f}% | Cache {tot_cr*wc/q_tot*100:.1f}% | Output {out_share:.1f}% | Overhead: {base['overhead_tok_per_turn']} tok/turn\n")
    print(f"{'Mode':44s} | {'Type':10s} | {'Tool Tok Cut':>12s} | {'Avg Cost Saved':>22s}")
    print("-" * 98)
    print(f"{'Mode 1: Lossless (Default)':44s} | {'measured':10s} | {base['saved_tool_tok']:9,d} tk | {b_h:+21.1f}%")
    print(f"{'Mode 2a: Compact Reads (Opt-in)':44s} | {'measured':10s} | {comp['saved_tool_tok']:9,d} tk | {c_h:+21.1f}%")
    print(f"{'Mode 2b: + Concise Output (Opt-in)':44s} | {'estimated':10s} | {comp['saved_tool_tok']:9,d} tk | {c_h - c20:+5.1f}%..{c_h - c35:+.1f}% (est.)")
    print(f"{'Mode 2c: + Auto-Handoff Live (Opt-in)':44s} | {'measured':10s} | {comp['saved_tool_tok']:9,d} tk | {c_live:+21.1f}%")
    print(f"{'Mode 2 (Max): Live + Compact + Concise':44s} | {'estimated':10s} | {comp['saved_tool_tok']:9,d} tk | {c_live - c20:+5.1f}%..{c_live - c35:+.1f}% (est.)")
    print(f"{'Mode 2 (Upper Bound, phase resets)':44s} | {'estimated':10s} | {comp['saved_tool_tok']:9,d} tk | {c_ub - c20:+5.1f}%..{c_ub - c35:+.1f}% (est.)")
    print("-" * 98)
    print("Note: 'measured' = exact transcript replay (charges retries + prompt overhead); 'estimated' adds a modeled -20%..-35% cut on the output bucket.")
    print("      Anchored Auto-Handoff (arXiv:2508.21433, Active Context Compression arXiv:2601.07190): lowest risk at phase switches.")

    # Distribution stats per mode (positive = saved %)
    def _dist_entry(label: str, kind: str, stats_obj: dict, add_out_lo: float = 0.0, add_out_hi: float = 0.0,
                    rows_src: list[dict] | None = None, field: str | None = None) -> dict:
        if add_out_lo == 0.0:
            return {
                "mode": label, "type": kind,
                "avg": round(stats_obj["total"], 1),
                "median": round(stats_obj["median"], 1),
                "p25": round(stats_obj["p25"], 1),
                "p75": round(stats_obj["p75"], 1),
                "p90": round(stats_obj["p90"], 1),
                "max": round(stats_obj["max"], 1),
                "pct_gt_5": round(stats_obj["pct_gt_5"], 1),
                "per_session": stats_obj["per_session"],
            }
        # Per-session estimated savings when adding -20% (conservative) output cut
        per_est = []
        for r in (rows_src or []):
            q0_r = r["q0"]
            if not q0_r:
                continue
            out_s = (r["tok_0"][2] * wo / q0_r) * 100.0
            base_s = (1.0 - r[field] / q0_r) * 100.0 if field else 0.0
            per_est.append(base_s + out_s * 0.20)
        per_est.sort()
        return {
            "mode": label, "type": kind,
            "avg": round(stats_obj["total"] + add_out_lo, 1),
            "avg_hi": round(stats_obj["total"] + add_out_hi, 1),
            "median": round(statistics.median(per_est), 1) if per_est else 0.0,
            "p25": round(_percentile(per_est, 0.25), 1),
            "p75": round(_percentile(per_est, 0.75), 1),
            "p90": round(_percentile(per_est, 0.90), 1),
            "max": round(per_est[-1], 1) if per_est else 0.0,
            "pct_gt_5": round(sum(1 for p in per_est if p > 5.0) / len(per_est) * 100.0, 1) if per_est else 0.0,
            "per_session": [round(p, 2) for p in per_est],
        }

    distributions = [
        _dist_entry("Mode 1: Lossless (Default)", "measured", base["q_hooks"]),
        _dist_entry("Mode 2a: Compact Reads", "measured", comp["q_hooks"]),
        _dist_entry("Mode 2b: + Concise Output", "estimated", comp["q_hooks"], c20, c35, comp["rows"], "q_hooks"),
        _dist_entry("Mode 2c: + Auto-Handoff Live", "measured", comp["q_both"]),
        _dist_entry("Mode 2 (Max): All opt-in", "estimated", comp["q_both"], c20, c35, comp["rows"], "q_both"),
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
        m1_avg, m1_med, m1_p90 = _agg(b_sub, "q_hooks")
        m2a_avg, _, _ = _agg(c_sub, "q_hooks")
        m2c_avg, m2c_med, m2c_p90 = _agg(c_sub, "q_both")
        by_type.append({
            "type": stype, "n": len(b_sub),
            "m1_avg": m1_avg, "m1_median": m1_med, "m1_p90": m1_p90,
            "m2a_avg": m2a_avg, "m2c_avg": m2c_avg, "m2c_median": m2c_med, "m2c_p90": m2c_p90,
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
        q0_r = _qsum(base["rows"], "tok_0")
        m1_r = (1.0 - _qsum(base["rows"], "tok_hooks") / q0_r) * 100.0 if q0_r else 0.0
        m2a_r = (1.0 - _qsum(comp["rows"], "tok_hooks") / q0_r) * 100.0 if q0_r else 0.0
        m2c_r = (1.0 - _qsum(comp["rows"], "tok_both") / q0_r) * 100.0 if q0_r else 0.0
        out_r = (sum(r["tok_0"][2] for r in base["rows"]) * rwo / q0_r * 100.0) if q0_r else 0.0
        sensitivity.append({
            "regime": rname, "weights": [rwi, rwc, rwo],
            "m1": round(m1_r, 1), "m2a": round(m2a_r, 1), "m2c": round(m2c_r, 1),
            "m2max_lo": round(m2c_r + out_r * 0.20, 1), "m2max_hi": round(m2c_r + out_r * 0.35, 1),
        })

    if report:
        print("\n=== Per-Session Distribution (Negative = Lower Cost) ===")
        print(f"{'Mode':34s} | {'Type':9s} | {'Avg':>7s} | {'Median':>7s} | {'p90':>7s} | {'Max':>7s} | {'>5% share':>9s}")
        print("-" * 96)
        for d in distributions:
            sfx = " (est.)" if d["type"] == "estimated" else ""
            print(f"{d['mode']:34s} | {d['type']:9s} | {-d['avg']:+6.1f}% | {-d['median']:+6.1f}% | {-d['p90']:+6.1f}% | {-d['max']:+6.1f}% | {d['pct_gt_5']:8.1f}%{sfx}")

        print("\n=== Breakdown by Session Type ===")
        print(f"{'Session Type':16s} | {'n':>3s} | {'Mode 1 Avg':>10s} | {'Mode 1 Med':>10s} | {'Mode 1 p90':>10s} | {'Mode 2a Avg':>11s} | {'Mode 2c Avg':>11s} | {'Mode 2c p90':>11s}")
        print("-" * 102)
        for t in by_type:
            print(f"{t['type']:16s} | {t['n']:3d} | {-t['m1_avg']:+9.1f}% | {-t['m1_median']:+9.1f}% | {-t['m1_p90']:+9.1f}% | {-t['m2a_avg']:+10.1f}% | {-t['m2c_avg']:+10.1f}% | {-t['m2c_p90']:+10.1f}%")

        print("\n=== Cost-Weight Sensitivity ===")
        print(f"{'Weight Regime (in/cache/out)':32s} | {'Mode 1':>8s} | {'Mode 2a':>8s} | {'Mode 2c':>8s} | {'Mode 2 Max (est.)':>20s}")
        print("-" * 88)
        for s_row in sensitivity:
            print(f"{s_row['regime']:32s} | {-s_row['m1']:+7.1f}% | {-s_row['m2a']:+7.1f}% | {-s_row['m2c']:+7.1f}% | {-s_row['m2max_lo']:+6.1f}%..{-s_row['m2max_hi']:+.1f}% (est.)")

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
        "concise_20_pct": c20,
        "concise_35_pct": c35,
        "base": base,
        "compact_reads": comp,
        "distributions": distributions,
        "by_type": by_type,
        "sensitivity": sensitivity,
    }
    if json_out:
        Path(json_out).write_text(json.dumps(res, indent=2), encoding="utf-8")
    return res


