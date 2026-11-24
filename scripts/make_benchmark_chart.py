#!/usr/bin/env python3
"""
Render docs/assets/benchmark-chart.svg from transcript_replay.py --json outputs.

  python3 scripts/transcript_replay.py --json after.json
  python3 scripts/make_benchmark_chart.py --after after.json [--before before.json] [--out docs/assets/benchmark-chart.svg]

Two panels per row: "Hooks only (Auto-Handoff OFF)" and "Hooks + Auto-Handoff". With --before,
each panel shows the previous release as a thin grey bar under the current green bar.
Values are quota savings in percent (positive = less quota); regressions render red, left of 0.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
W, LABEL_W = 960, 236
PANELS = [("Hooks only · Auto-Handoff OFF", "hooks"), ("Hooks + Auto-Handoff (live-reachable)", "both")]
GREEN, RED, GREY, DIM, TXT, SUB = "#4ade80", "#f87171", "#3a3a44", "#5e5e68", "#ececef", "#8e8e98"


def rows_of(s: dict) -> list[dict]:
    out = []
    for t in s["tiers"]:
        out.append({"label": t["label"].replace("-", "–"), "sub": f"{t['n']} session{'s' if t['n'] != 1 else ''} · avg peak {t['avg_peak'] // 1000}k",
                    "hooks": t["hooks"], "both": t["both"]})
    for lab, key, sub in (("Per-session median", "median", f"each of {s['n']} sessions weighted equally"),
                          ("Per-session mean", "mean", f"mean of {s['n']} session deltas"),
                          ("Total quota-weighted", "total", "aggregate replayed / actual quota")):
        out.append({"label": lab, "sub": sub, "hooks": s["q_hooks"][key], "both": s["q_both"][key], "summary": True,
                    "bold": key == "total"})
    return out


def render(after: dict, before: dict | None) -> str:
    ra = rows_of(after)
    rb = {r["label"]: r for r in rows_of(before)} if before else {}
    vals = [abs(r[k]) for r in ra for _, k in PANELS] + [abs(r[k]) for r in rb.values() for _, k in PANELS]
    vmax = max(5.0, max(vals or [5.0]))
    step = 1 if vmax <= 6 else (2 if vmax <= 12 else (5 if vmax <= 25 else 10))
    vmax = ((int(vmax) // step) + 1) * step
    pw = (W - LABEL_W - 36 - 30) / 2            # panel width
    neg = 34                                     # px reserved left of zero for regressions
    row_h = 42 if before else 38
    top = 112
    n_tier = sum(1 for r in ra if not r.get("summary"))
    h = top + len(ra) * row_h + 24 + 46
    p: list[str] = []
    a = p.append
    a(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {h}" width="{W}" height="{h}" role="img" aria-label="Real-session replay benchmark">')
    a('  <defs><style>.t{font-family:"Google Sans","Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}'
      '.m{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}</style></defs>')
    a(f'  <rect width="{W}" height="{h}" rx="14" fill="#070708"/>')
    a(f'  <rect x="0.5" y="0.5" width="{W - 1}" height="{h - 1}" rx="13.5" fill="none" stroke="#222227"/>')
    a(f'  <text x="36" y="38" class="t" font-size="16.5" font-weight="500" fill="{TXT}">Real-Session Replay · {after["n"]} local Antigravity sessions, {after["turns"]:,} turns</text>')
    sub = ("python3 scripts/transcript_replay.py · recorded tool calls replayed through the hooks · exact per-turn tokens · "
           "quota = 1× in + 0.05× cache + 6× out")
    a(f'  <text x="36" y="59" class="t" font-size="11.5" fill="{SUB}">{escape(sub)}</text>')
    if before:
        a(f'  <rect x="{W - 300}" y="29" width="14" height="5" rx="2" fill="{GREY}"/><text x="{W - 280}" y="35" class="t" font-size="11" fill="{SUB}">v1.0.0 (before)</text>')
        a(f'  <rect x="{W - 180}" y="27" width="14" height="9" rx="2" fill="{GREEN}"/><text x="{W - 160}" y="35" class="t" font-size="11" fill="{SUB}">this build (after)</text>')
    grid_y0, grid_y1 = top - 14, top + len(ra) * row_h - 6
    for pi, (title, _k) in enumerate(PANELS):
        x0 = LABEL_W + pi * (pw + 30)
        zx = x0 + neg
        scale = (pw - neg - 56) / vmax
        a(f'  <text x="{x0 + pw / 2:.0f}" y="{top - 22}" class="t" font-size="12" font-weight="500" fill="{SUB}" text-anchor="middle">{escape(title)}</text>')
        g = 0
        while g <= vmax:
            gx = zx + g * scale
            a(f'  <line x1="{gx:.1f}" y1="{grid_y0}" x2="{gx:.1f}" y2="{grid_y1}" stroke="#18181d"/>')
            a(f'  <text x="{gx:.1f}" y="{grid_y1 + 16}" class="m" font-size="10.5" fill="{DIM}" text-anchor="middle">{g}%</text>')
            g += step
    y = top
    for i, r in enumerate(ra):
        if i == n_tier:
            a(f'  <line x1="36" y1="{y - 12}" x2="{W - 36}" y2="{y - 12}" stroke="#222227"/>')
        fw = ' font-weight="500"' if r.get("bold") else ""
        a(f'  <text x="36" y="{y + 2}" class="t" font-size="13"{fw} fill="{TXT}">{escape(r["label"])}</text>')
        a(f'  <text x="36" y="{y + 17}" class="t" font-size="11" fill="{DIM}">{escape(r["sub"])}</text>')
        for pi, (_t, k) in enumerate(PANELS):
            x0 = LABEL_W + pi * (pw + 30)
            zx = x0 + neg
            scale = (pw - neg - 56) / vmax
            v = r[k]
            bw = max(3.0, abs(v) * scale)
            col = GREEN if v > 0.05 else (RED if v < -0.05 else "#45454f")
            bx = zx if v >= 0 else zx - min(bw, neg - 2)
            a(f'  <rect x="{bx:.1f}" y="{y - 9}" width="{(bw if v >= 0 else min(bw, neg - 2)):.1f}" height="12" rx="3" fill="{col}"/>')
            lx = (zx + bw + 6) if v >= 0 else (zx + 6)
            a(f'  <text x="{lx:.1f}" y="{y + 1}" class="m" font-size="11.5"{fw} fill="{col if abs(v) > 0.05 else SUB}">{-v:+.1f}%</text>')
            b = rb.get(r["label"])
            if b is not None:
                bv = b[k]
                bbw = max(2.0, abs(bv) * scale)
                bbx = zx if bv >= 0 else zx - min(bbw, neg - 2)
                a(f'  <rect x="{bbx:.1f}" y="{y + 6}" width="{(bbw if bv >= 0 else min(bbw, neg - 2)):.1f}" height="4" rx="2" fill="{GREY}"/>')
                a(f'  <text x="{(zx + bbw + 6) if bv >= 0 else zx + 6:.1f}" y="{y + 12}" class="m" font-size="9.5" fill="{DIM}">{-bv:+.1f}%</text>')
        y += row_h
    foot = (f"Signed quota delta (negative = less quota). Plugin prompt overhead ({after.get('overhead_tok_per_turn', 0):,} tok/turn) charged on every turn. "
            "Before and after use the same causal rollover simulator (17k cold start + 8k re-read tax).")
    a(f'  <text x="36" y="{h - 20}" class="t" font-size="10.5" fill="{DIM}">{escape(foot)}</text>')
    a("</svg>")
    return "\n".join(p) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--after", required=True)
    ap.add_argument("--before")
    ap.add_argument("--out", default=str(ROOT / "docs" / "assets" / "benchmark-chart.svg"))
    a = ap.parse_args()
    after = json.loads(Path(a.after).read_text(encoding="utf-8"))
    before = json.loads(Path(a.before).read_text(encoding="utf-8")) if a.before else None
    Path(a.out).write_text(render(after, before), encoding="utf-8")
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
