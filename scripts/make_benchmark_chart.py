#!/usr/bin/env python3
"""
Render docs/assets/benchmark-chart.svg from transcript_replay.py --modes --json outputs.

  python3 scripts/benchmark.py --modes --report --json modes.json
  python3 scripts/make_benchmark_chart.py --modes modes.json [--out docs/assets/benchmark-chart.svg]

Includes:
  - Top summary cards (Mode 1 Lossless Default vs Mode 2 Opt-in)
  - Total cost-proxy reduction bars (green = measured replay, amber = includes modeled Concise Output)
  - Per-session distribution strip + box plot (IQR box, median tick, p90 marker, >5% share)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
W = 960
BG, CARD, BORDER, GRID = "#070708", "#0d0d11", "#222227", "#18181d"
GREEN, AMBER, DIM, TXT, SUB = "#4ade80", "#f59e0b", "#5e5e68", "#ececef", "#8e8e98"


def render_two_mode(m: dict) -> str:
    base = m["base"]
    comp = m["compact_reads"]
    c20 = float(m.get("concise_20_pct", 6.2))
    c35 = float(m.get("concise_35_pct", 10.8))

    m1_tot = float(base["q_hooks"]["total"])
    m2_cr = float(comp["q_hooks"]["total"])
    m2_live = float(comp["q_both"]["total"])
    m2_live_c20 = round(m2_live + c20, 1)
    m2_live_c35 = round(m2_live + c35, 1)
    m2_ub_c20 = round(float(comp["q_both_ub"]["total"]) + c20, 1)
    m2_ub_c35 = round(float(comp["q_both_ub"]["total"]) + c35, 1)

    dists = m.get("distributions") or []
    has_dist = bool(dists and any(d.get("per_session") for d in dists))
    H = 740 if has_dist else 540

    p: list[str] = []
    a = p.append
    a(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="Real-Session Benchmark and Per-Session Distribution">')
    a('  <defs><style>.t{font-family:"Google Sans","Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}'
      '.m{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}</style></defs>')
    a(f'  <rect width="{W}" height="{H}" rx="14" fill="{BG}"/>')
    a(f'  <rect x="0.5" y="0.5" width="{W-1}" height="{H-1}" rx="13.5" fill="none" stroke="{BORDER}"/>')

    # Header
    w = m.get("weights") or [1, 0.05, 6]
    a(f'  <text x="32" y="36" class="t" font-size="16.5" font-weight="500" fill="{TXT}">Real-Session Replay · {m["n"]} sessions, {m["turns"]:,} turns ({m["quota_m"]:.1f}M cost-proxy units)</text>')
    a(f'  <text x="32" y="56" class="t" font-size="11.5" fill="{SUB}">python3 scripts/benchmark.py --modes --report · API-ratio proxy ({w[0]:g}× in + {w[1]:g}× cache + {w[2]:g}× out) · {m["overhead_tok_per_turn"]} tok/turn overhead charged</text>')

    # Two summary cards
    m1_p90 = float(base["q_hooks"].get("p90", 0.0))
    a(f'  <rect x="32" y="74" width="436" height="106" rx="10" fill="{CARD}" stroke="{GREEN}"/>')
    a(f'  <text x="48" y="96" class="t" font-size="12" font-weight="500" fill="{GREEN}">MODE 1 · LOSSLESS (DEFAULT)</text>')
    a(f'  <text x="452" y="96" class="m" font-size="11" fill="{GREEN}" text-anchor="end">measured</text>')
    a(f'  <text x="48" y="130" class="m" font-size="22" font-weight="500" fill="{TXT}">{-m1_tot:+.1f}% avg cost</text>')
    a(f'  <text x="452" y="128" class="m" font-size="12" fill="{SUB}" text-anchor="end">p90 {-m1_p90:+.1f}%</text>')
    a(f'  <text x="48" y="152" class="t" font-size="11.5" fill="{SUB}">{base["saved_tool_tok"]:,} tool tok cut · lossless output compressors &amp; read/loop guards</text>')
    a(f'  <text x="48" y="168" class="t" font-size="11.5" fill="{DIM}">Archive-Only handoffs (no automatic chat resets)</text>')

    a(f'  <rect x="492" y="74" width="436" height="106" rx="10" fill="{CARD}" stroke="{BORDER}"/>')
    a(f'  <text x="508" y="96" class="t" font-size="12" font-weight="500" fill="{SUB}">MODE 2 · OPT-IN (AUTO-HANDOFF + COMPACT)</text>')
    a(f'  <text x="912" y="96" class="m" font-size="11" fill="{AMBER}" text-anchor="end">measured + est.</text>')
    a(f'  <text x="508" y="130" class="m" font-size="22" font-weight="500" fill="{TXT}">-{m2_live:.1f}% measured</text>')
    a(f'  <text x="912" y="128" class="m" font-size="12" fill="{AMBER}" text-anchor="end">-{m2_live_c20:.1f}..-{m2_live_c35:.1f}% est.</text>')
    a(f'  <text x="508" y="152" class="t" font-size="11.5" fill="{SUB}">{comp["saved_tool_tok"]:,} tool tok cut · causal Auto-Handoff + tighter read caps</text>')
    a(f'  <text x="508" y="168" class="t" font-size="11.5" fill="{DIM}">Estimated range adds modeled -20%..-35% on Concise Output bucket</text>')

    # Horizontal comparison bars (50% max scale)
    bar_x0, bar_w, vmax = 290, 490, 50.0
    scale = bar_w / vmax
    top_y = 222
    rows = [
        ("Mode 1 · Lossless (Default)", "Measured replay · lossless hooks + Archive Only", m1_tot, None, GREEN, "exact"),
        ("Mode 2a · Compact Reads", "Measured replay · opt-in tighter read/image caps", m2_cr, None, "#86efac", "exact"),
        ("Mode 2b · + Concise Output", "Estimated · Mode 2a + modeled -20%..-35% output", round(m2_cr + c20, 1), round(m2_cr + c35, 1), AMBER, "band"),
        ("Mode 2c · + Auto-Handoff", "Measured replay · Mode 2a + live/cold Auto-Handoff", m2_live, None, GREEN, "exact"),
        ("Mode 2 · Max (All Opt-in)", "Estimated · Mode 2c + modeled -20%..-35% output", m2_live_c20, m2_live_c35, AMBER, "band"),
        ("Mode 2 · Upper Bound", "Estimated · manual resets at phase switches + Concise", m2_ub_c20, m2_ub_c35, "#d97706", "band"),
    ]

    a(f'  <text x="32" y="204" class="t" font-size="12" font-weight="500" fill="{SUB}">TOTAL COST-PROXY REDUCTION ACROSS {m["n"]} SESSIONS (NEGATIVE = LOWER COST)</text>')
    grid_y0, grid_y1 = top_y - 10, top_y + len(rows) * 40 - 6
    for g in range(0, 51, 10):
        gx = bar_x0 + g * scale
        a(f'  <line x1="{gx:.1f}" y1="{grid_y0}" x2="{gx:.1f}" y2="{grid_y1}" stroke="{GRID}"/>')
        a(f'  <text x="{gx:.1f}" y="{grid_y1 + 14}" class="m" font-size="10.5" fill="{DIM}" text-anchor="middle">-{g}%</text>')

    y = top_y
    for lab, sub, lo, hi, col, kind in rows:
        fw = ' font-weight="500"' if "Default" in lab or "Max" in lab else ""
        a(f'  <text x="32" y="{y + 4}" class="t" font-size="12"{fw} fill="{TXT}">{escape(lab)}</text>')
        a(f'  <text x="32" y="{y + 17}" class="t" font-size="10" fill="{DIM}">{escape(sub)}</text>')
        w1 = max(4.0, min(vmax, max(0.0, lo)) * scale)
        if kind == "band" and hi is not None:
            w2 = max(w1 + 4.0, min(vmax, max(0.0, hi)) * scale)
            a(f'  <rect x="{bar_x0:.1f}" y="{y - 5}" width="{w2:.1f}" height="13" rx="3" fill="{col}" fill-opacity="0.28"/>')
            a(f'  <rect x="{bar_x0:.1f}" y="{y - 5}" width="{w1:.1f}" height="13" rx="3" fill="{col}"/>')
            a(f'  <text x="{bar_x0 + w2 + 8:.1f}" y="{y + 5}" class="m" font-size="11"{fw} fill="{col}">-{lo:.1f}..-{hi:.1f}% (est.)</text>')
        else:
            a(f'  <rect x="{bar_x0:.1f}" y="{y - 5}" width="{w1:.1f}" height="13" rx="3" fill="{col}"/>')
            a(f'  <text x="{bar_x0 + w1 + 8:.1f}" y="{y + 5}" class="m" font-size="11"{fw} fill="{col}">{-lo:+.1f}% (measured)</text>')
        y += 40

    # Per-session distribution strip + box plot (IQR, median, p90, >5% share)
    if has_dist:
        dy0 = grid_y1 + 38
        a(f'  <line x1="32" y1="{dy0 - 14}" x2="{W - 32}" y2="{dy0 - 14}" stroke="{BORDER}"/>')
        a(f'  <text x="32" y="{dy0 + 4}" class="t" font-size="12" font-weight="500" fill="{SUB}">PER-SESSION SAVINGS DISTRIBUTION (DOTS = SESSIONS · BOX = IQR · TICK = MEDIAN · DIAMOND = P90)</text>')
        px0, pw = 290, 440
        pmin, pmax = -5.0, 55.0

        def px(val: float) -> float:
            clamped = max(pmin, min(pmax, val))
            return px0 + (clamped - pmin) / (pmax - pmin) * pw

        py_start = dy0 + 28
        py_end = py_start + len(dists) * 32 - 8
        for g in (0, 10, 20, 30, 40, 50):
            gx = px(g)
            stroke = "#2c2c34" if g == 0 else GRID
            a(f'  <line x1="{gx:.1f}" y1="{py_start - 10}" x2="{gx:.1f}" y2="{py_end}" stroke="{stroke}"/>')
            a(f'  <text x="{gx:.1f}" y="{py_end + 14}" class="m" font-size="10" fill="{DIM}" text-anchor="middle">{"" if g == 0 else "-"}{g}%</text>')

        py = py_start
        for d in dists:
            col = GREEN if d["type"] == "measured" else AMBER
            lab = d["mode"].replace(" (Default)", "")
            a(f'  <text x="32" y="{py + 4}" class="t" font-size="11.5" fill="{TXT}">{escape(lab)}</text>')
            # Session dots (strip plot)
            for idx, v in enumerate(d.get("per_session") or []):
                jy = ((idx % 5) - 2) * 2.0
                a(f'  <circle cx="{px(v):.1f}" cy="{py + jy:.1f}" r="2.1" fill="{col}" fill-opacity="0.35"/>')
            # IQR box (p25..p75)
            x25, x75 = px(d["p25"]), px(d["p75"])
            a(f'  <rect x="{x25:.1f}" y="{py - 6}" width="{max(2.0, x75 - x25):.1f}" height="12" rx="2" fill="{col}" fill-opacity="0.25" stroke="{col}" stroke-width="1.2"/>')
            # Median vertical line
            xm = px(d["median"])
            a(f'  <line x1="{xm:.1f}" y1="{py - 8}" x2="{xm:.1f}" y2="{py + 8}" stroke="{TXT}" stroke-width="2"/>')
            # p90 diamond
            x90 = px(d["p90"])
            a(f'  <polygon points="{x90:.1f},{py-5} {x90+4.5:.1f},{py} {x90:.1f},{py+5} {x90-4.5:.1f},{py}" fill="{col}"/>')
            # Right-side summary stats
            a(f'  <text x="{px0 + pw + 12:.1f}" y="{py + 4}" class="m" font-size="10.5" fill="{SUB}">med {-d["median"]:+.1f}% · p90 {-d["p90"]:+.1f}% · &gt;5%: {d["pct_gt_5"]:.0f}%</text>')
            py += 32

    # Footer notes
    a(f'  <line x1="32" y1="{H - 44}" x2="{W - 32}" y2="{H - 44}" stroke="{BORDER}"/>')
    a(f'  <text x="32" y="{H - 27}" class="t" font-size="10.5" fill="{DIM}">Green = measured replay on real transcripts (retries + {m["overhead_tok_per_turn"]} tok/turn overhead charged). Amber = adds modeled -20%..-35% cut on the {m["quota_mix"]["output_pct"]:.1f}% output bucket.</text>')
    a(f'  <text x="32" y="{H - 12}" class="t" font-size="10.5" fill="{DIM}">Cost weights ({w[0]:g}× input, {w[1]:g}× cache read, {w[2]:g}× output) are a configurable LLM API pricing proxy (--weights), not published Antigravity billing weights.</text>')
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
