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

    g_tot = float(base["q_hooks"]["total"])
    g_med = float(base["q_hooks"].get("median", 0.0))
    g_p90 = float(base["q_hooks"].get("p90", 0.0))
    gc_tot = float(comp["q_hooks"]["total"])
    ho_tot = float(base["q_both"]["total"])
    ho_med = float(base["q_both"].get("median", 0.0))
    ho_p90 = float(base["q_both"].get("p90", 0.0))
    hc_tot = float(comp["q_both"]["total"])

    dists = [d for d in (m.get("distributions") or []) if d.get("type") in ("replay", "measured")]
    has_dist = bool(dists and any(d.get("per_session") for d in dists))
    H = 600 if has_dist else 440

    p: list[str] = []
    a = p.append
    a(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="Real-Session Replay Estimate Benchmark">')
    a('  <defs><style>.t{font-family:"Google Sans","Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}'
      '.m{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}</style></defs>')
    a(f'  <rect width="{W}" height="{H}" rx="14" fill="{BG}"/>')
    a(f'  <rect x="0.5" y="0.5" width="{W-1}" height="{H-1}" rx="13.5" fill="none" stroke="{BORDER}"/>')

    w = m.get("weights") or [1, 0.05, 6]
    a(f'  <text x="32" y="36" class="t" font-size="16.5" font-weight="500" fill="{TXT}">Real-Session Replay Estimate · {m["n"]} sessions, {m["turns"]:,} turns ({m["quota_m"]:.1f}M cost-proxy units)</text>')
    a(f'  <text x="32" y="56" class="t" font-size="11.5" fill="{SUB}">python3 scripts/benchmark.py --modes --report · Cost weights ({w[0]:g}× in + {w[1]:g}× cache + {w[2]:g}× out) · {m["overhead_tok_per_turn"]} tok/turn prompt overhead</text>')

    # Two summary cards: Auto-Handoff + Read Guards (Default) vs Guards Only (Same Chat)
    a(f'  <rect x="32" y="74" width="436" height="106" rx="10" fill="{CARD}" stroke="{GREEN}"/>')
    a(f'  <text x="48" y="96" class="t" font-size="12" font-weight="500" fill="{GREEN}">AUTO-HANDOFF + READ GUARDS (DEFAULT)</text>')
    a(f'  <text x="452" y="96" class="m" font-size="11" fill="{GREEN}" text-anchor="end">replay estimate</text>')
    a(f'  <text x="48" y="130" class="m" font-size="21" font-weight="500" fill="{TXT}">{ho_tot:.1f}% saved · med {ho_med:.1f}%</text>')
    a(f'  <text x="452" y="128" class="m" font-size="12" fill="{SUB}" text-anchor="end">p90 {ho_p90:.1f}%</text>')
    a(f'  <text x="48" y="152" class="t" font-size="11.5" fill="{SUB}">Starts a fresh chat above cap or on cold cache (+6k re-read tax charged)</text>')
    a(f'  <text x="48" y="168" class="t" font-size="11.5" fill="{DIM}">With Compact Reads: {hc_tot:.1f}% saved · ~0% change on short chats</text>')

    a(f'  <rect x="492" y="74" width="436" height="106" rx="10" fill="{CARD}" stroke="{BORDER}"/>')
    a(f'  <text x="508" y="96" class="t" font-size="12" font-weight="500" fill="{SUB}">GUARDS ONLY (SAME CHAT)</text>')
    a(f'  <text x="912" y="96" class="m" font-size="11" fill="{GREEN}" text-anchor="end">replay estimate</text>')
    a(f'  <text x="508" y="130" class="m" font-size="21" font-weight="500" fill="{TXT}">{g_tot:.1f}% saved · med {g_med:.1f}%</text>')
    a(f'  <text x="912" y="128" class="m" font-size="12" fill="{SUB}" text-anchor="end">p90 {g_p90:.1f}%</text>')
    a(f'  <text x="508" y="152" class="t" font-size="11.5" fill="{SUB}">{base["saved_tool_tok"]:,} tool tok cut (lockfile, binary, dup-read, find/grep guards)</text>')
    a(f'  <text x="508" y="168" class="t" font-size="11.5" fill="{DIM}">Keeps same chat intact · with Compact Reads: {gc_tot:.1f}% saved</text>')

    # Horizontal comparison bars
    bar_x0, bar_w, vmax = 300, 460, 40.0
    scale = bar_w / vmax
    top_y = 222
    rows = [
        ("Auto-Handoff + Read Guards (Default)", "New chat above cap + lossless read guards", ho_tot, GREEN),
        ("Auto-Handoff + Compact Reads", "Adds tighter 120L/80L file caps", hc_tot, "#86efac"),
        ("Guards Only (Same Chat)", "Lockfile, duplicate-read, and loop guards only", g_tot, AMBER),
        ("Guards + Compact Reads", "Tighter file caps without chat resets", gc_tot, "#fcd34d"),
    ]

    a(f'  <text x="32" y="204" class="t" font-size="12" font-weight="500" fill="{SUB}">REPLAY-ESTIMATED COST REDUCTION ACROSS {m["n"]} SESSIONS (% LOWER COST)</text>')
    grid_y0, grid_y1 = top_y - 10, top_y + len(rows) * 40 - 6
    for g in range(0, 41, 10):
        gx = bar_x0 + g * scale
        a(f'  <line x1="{gx:.1f}" y1="{grid_y0}" x2="{gx:.1f}" y2="{grid_y1}" stroke="{GRID}"/>')
        a(f'  <text x="{gx:.1f}" y="{grid_y1 + 14}" class="m" font-size="10.5" fill="{DIM}" text-anchor="middle">{g}%</text>')

    y = top_y
    for lab, sub, lo, col in rows:
        fw = ' font-weight="500"' if "Default" in lab else ""
        a(f'  <text x="32" y="{y + 4}" class="t" font-size="12"{fw} fill="{TXT}">{escape(lab)}</text>')
        a(f'  <text x="32" y="{y + 17}" class="t" font-size="10" fill="{DIM}">{escape(sub)}</text>')
        w1 = max(4.0, min(vmax, max(0.0, lo)) * scale)
        a(f'  <rect x="{bar_x0:.1f}" y="{y - 5}" width="{w1:.1f}" height="13" rx="3" fill="{col}"/>')
        a(f'  <text x="{bar_x0 + w1 + 8:.1f}" y="{y + 5}" class="m" font-size="11"{fw} fill="{col}">{lo:.1f}% lower cost (replay est.)</text>')
        y += 40

    if has_dist:
        dy0 = grid_y1 + 36
        a(f'  <line x1="32" y1="{dy0 - 14}" x2="{W - 32}" y2="{dy0 - 14}" stroke="{BORDER}"/>')
        a(f'  <text x="32" y="{dy0 + 4}" class="t" font-size="12" font-weight="500" fill="{SUB}">PER-SESSION COST REDUCTION (DOTS = SESSIONS · BOX = IQR · TICK = MEDIAN · DIAMOND = P90)</text>')
        px0, pw = 300, 420
        pmin, pmax = -5.0, 50.0

        def px(val: float) -> float:
            clamped = max(pmin, min(pmax, val))
            return px0 + (clamped - pmin) / (pmax - pmin) * pw

        py_start = dy0 + 26
        py_end = py_start + len(dists) * 32 - 8
        for g in (0, 10, 20, 30, 40, 50):
            gx = px(g)
            stroke = "#2c2c34" if g == 0 else GRID
            a(f'  <line x1="{gx:.1f}" y1="{py_start - 10}" x2="{gx:.1f}" y2="{py_end}" stroke="{stroke}"/>')
            a(f'  <text x="{gx:.1f}" y="{py_end + 14}" class="m" font-size="10" fill="{DIM}" text-anchor="middle">{g}%</text>')

        py = py_start
        for d in dists:
            col = AMBER if "Guards" in d["mode"] and "Handoff" not in d["mode"] else GREEN
            lab = d["mode"].replace(" (Default)", "")
            a(f'  <text x="32" y="{py + 4}" class="t" font-size="11.5" fill="{TXT}">{escape(lab)}</text>')
            for idx, v in enumerate(d.get("per_session") or []):
                jy = ((idx % 5) - 2) * 2.0
                a(f'  <circle cx="{px(v):.1f}" cy="{py + jy:.1f}" r="2.1" fill="{col}" fill-opacity="0.35"/>')
            x25, x75 = px(d["p25"]), px(d["p75"])
            a(f'  <rect x="{x25:.1f}" y="{py - 6}" width="{max(2.0, x75 - x25):.1f}" height="12" rx="2" fill="{col}" fill-opacity="0.25" stroke="{col}" stroke-width="1.2"/>')
            xm = px(d["median"])
            a(f'  <line x1="{xm:.1f}" y1="{py - 8}" x2="{xm:.1f}" y2="{py + 8}" stroke="{TXT}" stroke-width="2"/>')
            x90 = px(d["p90"])
            a(f'  <polygon points="{x90:.1f},{py-5} {x90+4.5:.1f},{py} {x90:.1f},{py+5} {x90-4.5:.1f},{py}" fill="{col}"/>')
            a(f'  <text x="{px0 + pw + 12:.1f}" y="{py + 4}" class="m" font-size="10.5" fill="{SUB}">med {d["median"]:.1f}% · p90 {d["p90"]:.1f}% · &gt;5%: {d["pct_gt_5"]:.0f}%</text>')
            py += 32

    a(f'  <line x1="32" y1="{H - 44}" x2="{W - 32}" y2="{H - 44}" stroke="{BORDER}"/>')
    a(f'  <text x="32" y="{H - 27}" class="t" font-size="10.5" fill="{DIM}">Measured via local transcript replay (retries + {m["overhead_tok_per_turn"]} tok/turn overhead + 6k/rollover tax charged).</text>')
    a(f'  <text x="32" y="{H - 12}" class="t" font-size="10.5" fill="{DIM}">Cost weights ({w[0]:g}× input, {w[1]:g}× cache read, {w[2]:g}× output) are a configurable proxy (--weights), not Antigravity billing weights.</text>')
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
