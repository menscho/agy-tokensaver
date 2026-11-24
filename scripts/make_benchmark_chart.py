#!/usr/bin/env python3
"""
Render docs/assets/benchmark-chart.svg from transcript_replay.py --modes --json (or --json) outputs.

  python3 scripts/benchmark.py --modes --json modes.json
  python3 scripts/make_benchmark_chart.py --modes modes.json [--out docs/assets/benchmark-chart.svg]

Produces a 2-Mode Antigravity-styled comparison SVG:
  - Mode 1: Zero Quality Loss (0.0% loss, default ON)
  - Mode 2: Small Quality Loss (~0.1%-1.0% risk, opt-in)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
W = 960
BG, CARD, BORDER, GRID = "#070708", "#0d0d11", "#222227", "#18181d"
GREEN, GREEN_DIM, AMBER, RED, DIM, TXT, SUB = "#4ade80", "#166534", "#f59e0b", "#f87171", "#5e5e68", "#ececef", "#8e8e98"


def render_two_mode(m: dict) -> str:
    base = m["base"]
    comp = m["compact_reads"]
    c20 = float(m.get("concise_20_pct", 6.2))
    c35 = float(m.get("concise_35_pct", 10.8))

    # Build clean rows for the 2-mode overview + tier breakdown
    m1_tot = float(base["q_hooks"]["total"])            # positive = saved %
    m2_cr = float(comp["q_hooks"]["total"])             # compact reads only
    m2_live = float(comp["q_both"]["total"])            # exact replay: compact reads + live auto-handoff
    m2_live_c20 = round(m2_live + c20, 1)               # + concise -20% (conservative)
    m2_live_c35 = round(m2_live + c35, 1)               # + concise -35% (moderate)
    m2_ub_c20 = round(float(comp["q_both_ub"]["total"]) + c20, 1)
    m2_ub_c35 = round(float(comp["q_both_ub"]["total"]) + c35, 1)

    H = 540
    p: list[str] = []
    a = p.append
    a(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="2-Mode Real-Session Benchmark">')
    a('  <defs><style>.t{font-family:"Google Sans","Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}'
      '.m{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}</style></defs>')
    a(f'  <rect width="{W}" height="{H}" rx="14" fill="{BG}"/>')
    a(f'  <rect x="0.5" y="0.5" width="{W-1}" height="{H-1}" rx="13.5" fill="none" stroke="{BORDER}"/>')

    # Header
    a(f'  <text x="32" y="36" class="t" font-size="16.5" font-weight="500" fill="{TXT}">Real-Session Replay · {m["n"]} sessions, {m["turns"]:,} turns ({m["quota_m"]:.1f}M cost-proxy units)</text>')
    a(f'  <text x="32" y="56" class="t" font-size="11.5" fill="{SUB}">python3 scripts/benchmark.py --modes · API-ratio proxy (1× in + 0.05× cache + 6× out) · {m["overhead_tok_per_turn"]} tok/turn overhead charged</text>')

    # Two summary cards at top
    # Card 1: Mode 1 Lossless (Default)
    a(f'  <rect x="32" y="74" width="436" height="106" rx="10" fill="{CARD}" stroke="{GREEN}"/>')
    a(f'  <text x="48" y="96" class="t" font-size="12" font-weight="500" fill="{GREEN}">MODE 1 · LOSSLESS (DEFAULT)</text>')
    a(f'  <text x="452" y="96" class="m" font-size="11" fill="{GREEN}" text-anchor="end">measured</text>')
    a(f'  <text x="48" y="130" class="m" font-size="22" font-weight="500" fill="{TXT}">{-m1_tot:+.1f}% avg cost</text>')
    a(f'  <text x="452" y="128" class="m" font-size="12" fill="{SUB}" text-anchor="end">{base["saved_tool_tok"]:,} tool tok cut</text>')
    a(f'  <text x="48" y="152" class="t" font-size="11.5" fill="{SUB}">Lossless output compressors, duplicate-read &amp; loop guards,</text>')
    a(f'  <text x="48" y="168" class="t" font-size="11.5" fill="{DIM}">cache-safe prefixes, Archive-Only handoffs (no automatic chat resets)</text>')

    # Card 2: Mode 2 Opt-in Savings
    a(f'  <rect x="492" y="74" width="436" height="106" rx="10" fill="{CARD}" stroke="{BORDER}"/>')
    a(f'  <text x="508" y="96" class="t" font-size="12" font-weight="500" fill="{SUB}">MODE 2 · OPT-IN (AUTO-HANDOFF + COMPACT)</text>')
    a(f'  <text x="912" y="96" class="m" font-size="11" fill="{AMBER}" text-anchor="end">measured + est.</text>')
    a(f'  <text x="508" y="130" class="m" font-size="22" font-weight="500" fill="{TXT}">-{m2_live:.1f}% measured</text>')
    a(f'  <text x="912" y="128" class="m" font-size="12" fill="{AMBER}" text-anchor="end">-{m2_live_c20:.1f}..-{m2_live_c35:.1f}% est.</text>')
    a(f'  <text x="508" y="152" class="t" font-size="11.5" fill="{SUB}">{comp["saved_tool_tok"]:,} tool tok cut · causal Auto-Handoff + tighter read caps</text>')
    a(f'  <text x="508" y="168" class="t" font-size="11.5" fill="{DIM}">Estimated range adds modeled -20%..-35% on Concise Output (6× bucket)</text>')

    # Horizontal comparison bars (50% max scale)
    bar_x0, bar_w, vmax = 290, 490, 50.0
    scale = bar_w / vmax
    top_y = 224
    rows = [
        ("Mode 1 · Lossless (Default)", "Measured replay · lossless hooks + Archive Only", m1_tot, None, GREEN, "exact"),
        ("Mode 2a · Compact Reads", "Measured replay · opt-in tighter read/image caps", m2_cr, None, "#86efac", "exact"),
        ("Mode 2b · + Concise Output", "Estimated · Mode 2a + modeled -20%..-35% output", round(m2_cr + c20, 1), round(m2_cr + c35, 1), AMBER, "band"),
        ("Mode 2c · + Auto-Handoff", "Measured replay · Mode 2a + live/cold Auto-Handoff", m2_live, None, GREEN, "exact"),
        ("Mode 2 · Max (All Opt-in)", "Estimated · Mode 2c + modeled -20%..-35% output", m2_live_c20, m2_live_c35, AMBER, "band"),
        ("Mode 2 · Upper Bound", "Estimated · manual resets at phase switches + Concise", m2_ub_c20, m2_ub_c35, "#d97706", "band"),
    ]

    a(f'  <text x="32" y="206" class="t" font-size="12" font-weight="500" fill="{SUB}">TOTAL COST-PROXY REDUCTION ACROSS {m["n"]} SESSIONS (NEGATIVE = LOWER COST)</text>')
    grid_y0, grid_y1 = top_y - 10, top_y + len(rows) * 44 - 6
    for g in range(0, 51, 10):
        gx = bar_x0 + g * scale
        a(f'  <line x1="{gx:.1f}" y1="{grid_y0}" x2="{gx:.1f}" y2="{grid_y1}" stroke="{GRID}"/>')
        a(f'  <text x="{gx:.1f}" y="{grid_y1 + 15}" class="m" font-size="10.5" fill="{DIM}" text-anchor="middle">-{g}%</text>')

    y = top_y
    for lab, sub, lo, hi, col, kind in rows:
        fw = ' font-weight="500"' if "Default" in lab or "Max" in lab else ""
        a(f'  <text x="32" y="{y + 4}" class="t" font-size="12.5"{fw} fill="{TXT}">{escape(lab)}</text>')
        a(f'  <text x="32" y="{y + 18}" class="t" font-size="10.5" fill="{DIM}">{escape(sub)}</text>')
        w1 = max(4.0, min(vmax, max(0.0, lo)) * scale)
        if kind == "band" and hi is not None:
            w2 = max(w1 + 4.0, min(vmax, max(0.0, hi)) * scale)
            a(f'  <rect x="{bar_x0:.1f}" y="{y - 6}" width="{w2:.1f}" height="14" rx="3" fill="{col}" fill-opacity="0.28"/>')
            a(f'  <rect x="{bar_x0:.1f}" y="{y - 6}" width="{w1:.1f}" height="14" rx="3" fill="{col}"/>')
            a(f'  <text x="{bar_x0 + w2 + 8:.1f}" y="{y + 5}" class="m" font-size="11"{fw} fill="{col}">-{lo:.1f}..-{hi:.1f}% (est.)</text>')
        else:
            a(f'  <rect x="{bar_x0:.1f}" y="{y - 6}" width="{w1:.1f}" height="14" rx="3" fill="{col}"/>')
            a(f'  <text x="{bar_x0 + w1 + 8:.1f}" y="{y + 5}" class="m" font-size="11"{fw} fill="{col}">{-lo:+.1f}% (measured)</text>')
        y += 44

    # Footer notes
    a(f'  <line x1="32" y1="{H - 46}" x2="{W - 32}" y2="{H - 46}" stroke="{BORDER}"/>')
    a(f'  <text x="32" y="{H - 28}" class="t" font-size="10.5" fill="{DIM}">Green bars = measured replay on real transcripts (retries + {m["overhead_tok_per_turn"]} tok/turn overhead charged). Amber bars = includes modeled -20%..-35% cut on the {m["quota_mix"]["output_pct"]:.1f}% output bucket.</text>')
    a(f'  <text x="32" y="{H - 13}" class="t" font-size="10.5" fill="{DIM}">Cost weights (1× input, 0.05× cache read, 6× output) are a standard LLM API pricing proxy, not published Antigravity billing weights.</text>')
    a("</svg>")
    return "\n".join(p) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", help="JSON output from transcript_replay.py --modes --json")
    ap.add_argument("--after", help="Legacy single-mode JSON from transcript_replay.py --json")
    ap.add_argument("--out", default=str(ROOT / "docs" / "assets" / "benchmark-chart.svg"))
    a = ap.parse_args()
    src = a.modes or a.after
    if not src:
        ap.error("pass --modes <json> (or --after <json>)")
    data = json.loads(Path(src).read_text(encoding="utf-8"))
    if "base" not in data:
        # Wrap legacy --after JSON cleanly
        data = {
            "n": data["n"], "turns": data["turns"], "quota_m": 0.0,
            "overhead_tok_per_turn": data.get("overhead_tok_per_turn", 0),
            "quota_mix": {"input_pct": 37.5, "cache_pct": 31.6, "output_pct": 30.9},
            "concise_20_pct": 6.2, "concise_35_pct": 10.8,
            "base": data, "compact_reads": data,
        }
    Path(a.out).write_text(render_two_mode(data), encoding="utf-8")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
