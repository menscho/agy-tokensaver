#!/usr/bin/env python3
"""
Hooks-only (Auto-Handoff OFF) replay of REAL local Antigravity sessions.

For every local session (IDE `~/.gemini/antigravity` and CLI `~/.gemini/antigravity-cli`):
  * Exact per-turn token usage (input / cache_read / output) is taken from the transcript
    PLANNER_RESPONSE records when present, otherwise decoded from the session's
    `conversations/<id>.db` `gen_metadata` protobuf (field 1.4: 2=input, 5=cache_read, 3=output).
  * Every recorded tool call + its recorded tool result (transcript_full.jsonl) is replayed
    through the ACTUAL PreToolUse hooks of this checkout:
      - read_file / view_file -> read_hook.pl against a reconstructed virtual file
        (same basename/extension, same line count & byte size, rewritten only when the real
        file changed between reads, so mtime-based dedupe behaves like the live session)
      - run_command          -> cmd_hook.pl, and if it wraps, cmd_wrap.pl --test-stdin on the
        recorded output with the recorded exit code
      - find_by_name         -> find_hook.pl
  * Saved tool-result bytes are converted to tokens (4 B/token) and removed from the context of
    every later turn (first as uncached input, afterwards as cache read) until the next
    context compaction (CHECKPOINT / >40% context drop). Denials that force a retry are charged
    an extra small turn (0.05x context + 150 output tokens).
  * Quota weights are identical to core.py: 1x uncached input, 0.05x cache read, 6x output.

The same adjusted turn tuples are then fed to core._simulate_rollover_session to report the
combined "hooks + Auto-Handoff" figure.

Usage:
  python3 scripts/transcript_replay.py              # table
  python3 scripts/transcript_replay.py --json out.json
  python3 scripts/transcript_replay.py --detail     # per-session + per-category breakdown
"""

from __future__ import annotations

import argparse
import ast
import collections
import hashlib
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT / "mcp_app"))
sys.path.insert(0, str(SCRIPTS))
import core  # noqa: E402

W_IN, W_CR, W_OUT = core.W_IN, core.W_CR, core.W_OUT
BYTES_PER_TOK = 4
NOTICE_BYTES = 94          # "A pre-tool hook changed the arguments ..." notice
RETRY_OUT_TOK = 150        # output tokens of a forced retry turn after a deny
NATIVE_READ_CAP = 46_080

GEMINI = Path(os.environ.get("AGY_TS_GEMINI_HOME") or (Path.home() / ".gemini"))
APP_DIRS = [GEMINI / "antigravity", GEMINI / "antigravity-cli"]



from replay_io import (  # noqa: E402, F401
    VirtualFS, _b, _cmd_output, _frontmatter, _pb_fields, _run_perl, _rv, _tool_calls,
    db_turns, discover_sessions, load_session, plugin_overhead_bytes,
)
from replay_modes import run_modes  # noqa: E402, F401
try:
    import replay_ext  # type: ignore  # noqa: E402
except Exception:
    replay_ext = None

def replay_session(sess: dict, tmp: Path, perl_env: dict) -> dict:
    sid = sess["id"]
    pdata = tmp / "pdata" / sid
    pdata.mkdir(parents=True, exist_ok=True)
    env = {**perl_env, "PLUGIN_DATA": str(pdata)}
    vfs = VirtualFS(tmp / "vfs" / sid)
    n = len(sess["turns"])
    saved_by_turn = [0] * (n + 1)     # bytes saved in results produced after planner turn k
    retries_by_turn = [0] * (n + 1)
    cats: collections.Counter = collections.Counter()
    cat_n: collections.Counter = collections.Counter()
    dirty: set[str] = set()
    tool_bytes = 0

    for ev in sess["events"]:
        k = min(ev["turn"], n - 1)
        name, args, content = ev["name"], ev["args"], ev["content"]
        tool_bytes += _b(content)
        if name in ("write_to_file", "write_file", "edit_file", "replace_file_content", "multi_replace_file_content"):
            tgt = args.get("TargetFile") or args.get("Path") or args.get("AbsolutePath") or ""
            if tgt:
                dirty.add(str(tgt).lower())
            continue
        if name in ("read_file", "view_file"):
            real = str(args.get("Path") or args.get("AbsolutePath") or "")
            if not real:
                continue
            info = vfs.parse_read(content)
            vp = vfs.materialize(real, info, real.lower() in dirty)
            dirty.discard(real.lower())
            ti: dict = {"Path": str(vp)}
            for key in ("StartLine", "EndLine", "ByteOffset", "ContentOffset"):
                if key in args and str(args[key]).lstrip("-").isdigit():
                    ti[key] = int(args[key])
            payload = {"toolInput": ti, "stepIdx": ev["step"], "conversationId": sid}
            rc, out = _run_perl("read_hook.pl", payload, env)
            try:
                res = json.loads(out)
            except Exception:
                continue
            before = _b(content)
            if res.get("decision") == "deny":
                after = _b(res.get("reason") or "")
                reason = res.get("reason") or ""
                tag = "read:dedupe" if re.search(r"already|unchanged|in context|re-?read", reason, re.I) else "read:block"
                if tag != "read:dedupe":
                    retries_by_turn[k] += 1
            elif res.get("overwrite"):
                ov = res["overwrite"]
                s = int(ov.get("StartLine") or ti.get("StartLine") or 1)
                e = int(ov.get("EndLine") or 0)
                if e:
                    head = content.split("\n")
                    hdr_b = sum(_b(x) + 1 for x in head if not NUM_LINE.match(x))
                    after = hdr_b + vfs.slice_bytes(real, s, e) + NOTICE_BYTES
                    tag = "read:cap"
                    # Docs (SKILL.md, references, notes) are read to be read in full: assume the
                    # model fetches the cut remainder in a follow-up turn (no saving, +1 turn).
                    if re.search(r"\.(?:md|mdx|markdown|txt|rst)$", real, re.I):
                        after = before + NOTICE_BYTES
                        retries_by_turn[k] += 1
                        tag = "read:cap_doc"
                else:
                    after, tag = before, "read:allow"
            else:
                after, tag = before, "read:allow"
            d = before - after
            if d != 0:
                saved_by_turn[k] += d
                cats[tag] += d
                cat_n[tag] += 1
            continue
        if name in ("run_command", "shell_exec"):
            cmd = str(args.get("CommandLine") or args.get("command") or "")
            if not cmd or "background task" in content[:300]:
                continue
            payload = {"toolInput": {"CommandLine": cmd, "Cwd": str(args.get("Cwd") or "")}}
            _rc, out = _run_perl("cmd_hook.pl", payload, env)
            try:
                res = json.loads(out)
            except Exception:
                continue
            ov = (res.get("overwrite") or {})
            new_cmd = ov.get("CommandLine") or ov.get("command") or ""
            m = re.search(r"cmd_wrap\.pl\S*\s+--cat\s+([A-Za-z0-9_]+)", new_cmd)
            if not m:
                continue
            cat = m.group(1)
            rcode, body = _cmd_output(content)
            _r, wrapped = _run_perl("cmd_wrap.pl", {}, env, stdin_text=body,
                                    args=["--cat", cat, "--test-stdin", "--test-rc", str(rcode)])
            before = _b(body)
            after = _b(wrapped) + NOTICE_BYTES
            d = before - after
            saved_by_turn[k] += d
            cats[f"cmd:{cat}"] += d
            cat_n[f"cmd:{cat}"] += 1
            continue
        if replay_ext is not None and hasattr(replay_ext, "handle"):
            try:
                hres = replay_ext.handle(name, args, content, env=env, sid=sid, step=ev["step"], tmp=tmp)
            except Exception:
                hres = None
            if hres:
                d, tag, retry = int(hres[0]), str(hres[1]), int(hres[2] if len(hres) > 2 else 0)
                if d:
                    saved_by_turn[k] += d
                    cats[tag] += d
                    cat_n[tag] += 1
                retries_by_turn[k] += retry
                continue
        if name in ("find_by_name",):
            payload = {"toolInput": {k2: (str(v) if not isinstance(v, (int, float)) else v) for k2, v in args.items()}}
            _rc, out = _run_perl("find_hook.pl", payload, env)
            try:
                res = json.loads(out)
            except Exception:
                continue
            if res.get("decision") == "deny":
                d = _b(content) - _b(res.get("reason") or "")
                saved_by_turn[k] += d
                retries_by_turn[k] += 1
                cats["find:block"] += d
                cat_n["find:block"] += 1
    return {"saved_by_turn": saved_by_turn, "retries_by_turn": retries_by_turn, "cats": cats, "cat_n": cat_n, "tool_bytes": tool_bytes}


# ----------------------------------------------------------------------------- counterfactual
def apply_savings(turns: list[tuple[int, int, int]], saved_by_turn: list[int], retries_by_turn: list[int], checkpoints: list[int], overhead_tok: int = 0) -> tuple[list[tuple[int, int, int]], float]:
    """Remove saved tokens from every later turn's context until the next compaction.
    `overhead_tok` (always-loaded plugin rules) is ADDED to every turn: uncached on turn 0, cached after."""
    cps = set(checkpoints)
    adj: list[tuple[int, int, int]] = []
    carried = 0           # tokens removed from the cached prefix
    extra = 0.0           # quota of forced retry turns
    prev_ctx = 0
    for i, (inp, cr, out) in enumerate(turns):
        ctx = inp + cr
        if i > 0 and ((i - 1) in cps or (prev_ctx >= 20_000 and ctx < prev_ctx * 0.6)):
            carried = 0
        fresh = (saved_by_turn[i - 1] // BYTES_PER_TOK) if i > 0 else 0
        need_fresh = max(0, fresh)
        need_old = max(0, carried)
        ni, nc = inp, cr
        t = min(need_fresh, ni); ni -= t; need_fresh -= t
        t = min(need_old + need_fresh, nc); nc -= t; rem = need_old + need_fresh - t
        t = min(rem, ni); ni -= t
        if fresh < 0:   # hook made the result larger (never expected, but never clamped)
            ni += -fresh
        if overhead_tok:
            if i == 0:
                ni += overhead_tok
            else:
                nc += overhead_tok
        adj.append((ni, nc, out))
        if i > 0 and retries_by_turn[i - 1]:
            extra += retries_by_turn[i - 1] * ((ni + nc) * W_CR + RETRY_OUT_TOK * W_OUT)
        carried += fresh
        prev_ctx = ctx
    return adj, extra


def parse_weights(spec: str | None) -> tuple[float, float, float]:
    """Parse --weights 'input=1,cache=0.05,output=6' into (w_in, w_cr, w_out)."""
    if not spec:
        return (W_IN, W_CR, W_OUT)
    vals = {"input": W_IN, "cache": W_CR, "output": W_OUT}
    for part in spec.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip().lower()
        if k in ("in", "input"):
            vals["input"] = float(v)
        elif k in ("cr", "cache", "cache_read"):
            vals["cache"] = float(v)
        elif k in ("out", "output"):
            vals["output"] = float(v)
    return (vals["input"], vals["cache"], vals["output"])


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = (len(sorted_vals) - 1) * p
    lo = int(idx)
    hi = min(len(sorted_vals) - 1, lo + 1)
    frac = idx - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _classify_session_type(sess: dict, cat_n: collections.Counter, tool_bytes: int) -> str:
    """Classify a session as test-heavy, log-heavy, or plain-editing."""
    n_test = sum(cat_n.get(k, 0) for k in ("test", "build", "lint", "pkg", "chain"))
    n_log = sum(cat_n.get(k, 0) for k in ("logs", "ssh", "http", "cloud", "k8s_yaml", "search", "cat_large", "table", "ps", "spillback_log"))
    n_cmd = sum(1 for e in sess["events"] if e["name"] in ("run_command", "shell_exec"))
    if n_test >= 3 or (n_cmd >= 4 and n_test / max(1, n_cmd) >= 0.35):
        return "test-heavy"
    if n_log >= 3 or (n_cmd >= 4 and n_log / max(1, n_cmd) >= 0.35) or tool_bytes >= 120_000:
        return "log-heavy"
    return "plain-editing"


def quota(turns: list[tuple[int, int, int]], weights: tuple[float, float, float] | None = None) -> float:
    wi, wc, wo = weights if weights else (W_IN, W_CR, W_OUT)
    return sum(a * wi + b * wc + c * wo for a, b, c in turns)


def _turns_sums(turns: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    return (sum(t[0] for t in turns), sum(t[1] for t in turns), sum(t[2] for t in turns))


def settled_flags_and_gaps(sess: dict) -> tuple[list[bool] | None, list[float | None] | None, int]:
    """Per-generation live-tick eligibility, inter-turn idle gaps (s), and measured TTL cliff."""
    if not hasattr(core, "_settled_flags"):
        return None, None, 150
    tx, _db = core._session_paths(sess["app_path"], sess["id"])
    if tx is None:
        return [False] * len(sess["turns"]), None, 150
    scan = core._scan_transcript(tx)
    planners = scan["planners"]
    meta: list = []
    if sess["src"] == "transcript":
        meta = [p for p in planners if any(p["tok"])]
    else:
        off = len(planners) - len(sess["turns"])
        meta = [planners[r + off] if 0 <= r + off < len(planners) else None for r in range(len(sess["turns"]))]
    n = len(sess["turns"])
    meta = (meta + [None] * n)[:n]
    last_model = next((p["model"] for p in reversed(planners) if p.get("model")), "")
    ttl_cliff = 90 if "hydrogen" in last_model.lower() else 150
    gaps: list[float | None] = []
    for k in range(n):
        t0 = meta[k].get("ts") if meta[k] else None
        t1 = meta[k + 1].get("ts") if k + 1 < n and meta[k + 1] else None
        gaps.append(max(0.0, t1 - t0) if (t0 and t1 and t1 >= t0) else None)
    return core._settled_flags(meta, scan["users"] > 0), gaps, ttl_cliff


def settled_flags(sess: dict) -> list[bool] | None:
    return settled_flags_and_gaps(sess)[0]


def rollover_totals(turns: list[tuple[int, int, int]], policy: str = "auto", settled: list[bool] | None = None,
                    cfg: dict | None = None, gaps: list[float | None] | None = None, ttl_cliff: int = 150) -> tuple[int, int, int, int]:
    """Return (sim_in, sim_cr, tot_out, nroll) for any weighting."""
    cap = 110_000 if policy == "v1" else None
    try:
        si, sc, nroll = core._simulate_rollover_session(turns, cap, settled=settled, cfg=cfg, gaps=gaps, ttl_cliff=ttl_cliff)
    except TypeError:   # v1.0.0 core: oracle simulator, no live gating
        si, sc, nroll = core._simulate_rollover_session(turns, 110_000)
    return int(si), int(sc), sum(t[2] for t in turns), int(nroll)


def rollover_quota(turns: list[tuple[int, int, int]], policy: str = "auto", settled: list[bool] | None = None,
                   cfg: dict | None = None, gaps: list[float | None] | None = None, ttl_cliff: int = 150,
                   weights: tuple[float, float, float] | None = None) -> tuple[float, int]:
    """policy 'auto' = shipped adaptive policy; 'v1' = v1.0.0 behaviour (fixed 110k cap).
    settled=None -> UPPER BOUND (rollover allowed after any generation, i.e. mid agent run)."""
    wi, wc, wo = weights if weights else (W_IN, W_CR, W_OUT)
    si, sc, so, nroll = rollover_totals(turns, policy, settled, cfg, gaps, ttl_cliff)
    return si * wi + sc * wc + so * wo, nroll


TIERS = [
    ("Short (<75k)", 0, 75_000),
    ("Medium (75k-110k)", 75_000, 110_000),
    ("Long (110k-200k)", 110_000, 200_000),
    ("Very long (200k-400k)", 200_000, 400_000),
    ("Extreme (>400k)", 400_000, 10**12),
]


def run(detail: bool = False, json_out: str | None = None, quiet: bool = False, policy: str = "auto",
        extra_env: dict[str, str] | None = None, weights: tuple[float, float, float] | None = None) -> dict:
    wi, wc, wo = weights if weights else (W_IN, W_CR, W_OUT)
    sessions = discover_sessions()
    # Live auto policy learns p_continue from this machine's OTHER chats (core.continuation_probability)
    recs = []
    if hasattr(core, "parse_session_cached"):
        for s in sessions:
            tx, db = core._session_paths(s["app_path"], s["id"])
            r0 = core.parse_session_cached(s["id"], tx, db, s["app"])
            if r0:
                recs.append(r0)
    perl_env = {**os.environ, **(extra_env or {})}
    rows = []
    cats_all: collections.Counter = collections.Counter()
    catn_all: collections.Counter = collections.Counter()
    overhead_tok = plugin_overhead_bytes() // BYTES_PER_TOK
    with tempfile.TemporaryDirectory(prefix="agy-ts-replay-") as td:
        tmp = Path(td)
        for s in sessions:
            r = replay_session(s, tmp, perl_env)
            cats_all.update(r["cats"])
            catn_all.update(r["cat_n"])
            adj, extra_default = apply_savings(s["turns"], r["saved_by_turn"], r["retries_by_turn"], s["checkpoints"], overhead_tok)
            # Track retry overhead tokens separately so arbitrary weights stay exact
            retry_cr = sum(r["retries_by_turn"][i - 1] * (adj[i][0] + adj[i][1]) for i in range(1, len(adj)) if r["retries_by_turn"][i - 1])
            retry_out = sum(r["retries_by_turn"][i - 1] * RETRY_OUT_TOK for i in range(1, len(adj)) if r["retries_by_turn"][i - 1])
            extra = retry_cr * wc + retry_out * wo
            t0_sum = _turns_sums(s["turns"])
            th_sum = _turns_sums(adj)
            th_sum_tot = (th_sum[0], th_sum[1] + retry_cr, th_sum[2] + retry_out)
            q0 = quota(s["turns"], (wi, wc, wo))
            qh = quota(adj, (wi, wc, wo)) + extra
            settled, gaps, ttl_cliff = settled_flags_and_gaps(s)
            cfg = None
            if policy == "auto" and hasattr(core, "continuation_probability"):
                cfg = {**core._SIM_CFG, "p_continue": core.continuation_probability(recs, exclude=s["id"])}
            r_in, r_cr, r_out, nr = rollover_totals(s["turns"], policy, settled, cfg, gaps, ttl_cliff)
            hr_in, hr_cr, hr_out, nhr = rollover_totals(adj, policy, settled, cfg, gaps, ttl_cliff)
            ru_in, ru_cr, ru_out, _ = rollover_totals(s["turns"], policy, None, cfg, gaps, ttl_cliff)
            hru_in, hru_cr, hru_out, _ = rollover_totals(adj, policy, None, cfg, gaps, ttl_cliff)
            qr = r_in * wi + r_cr * wc + r_out * wo
            qhr = hr_in * wi + (hr_cr + retry_cr) * wc + (hr_out + retry_out) * wo
            qru = ru_in * wi + ru_cr * wc + ru_out * wo
            qhru = hru_in * wi + (hru_cr + retry_cr) * wc + (hru_out + retry_out) * wo
            stype = _classify_session_type(s, r["cat_n"], r["tool_bytes"])
            rows.append({
                "id": s["id"], "app": s["app"], "src": s["src"], "turns": len(s["turns"]),
                "session_type": stype,
                "peak": max(a + b for a, b, _ in s["turns"]),
                "q0": q0, "q_hooks": qh, "q_roll": qr, "q_both": qhr, "q_roll_ub": qru, "q_both_ub": qhru, "rollovers": nhr,
                "tok_0": t0_sum, "tok_hooks": th_sum_tot,
                "tok_both": (hr_in, hr_cr + retry_cr, hr_out + retry_out),
                "tok_both_ub": (hru_in, hru_cr + retry_cr, hru_out + retry_out),
                "saved_bytes": sum(r["saved_by_turn"]), "tool_bytes": r["tool_bytes"],
                "events": len(s["events"]),
            })

    def pct(a: float, b: float) -> float:
        return (1.0 - b / a) * 100.0 if a else 0.0

    summary: dict = {"n": len(rows), "turns": sum(r["turns"] for r in rows), "tiers": [], "cats": {}, "policy": policy, "weights": [wi, wc, wo]}
    for key in ("q_hooks", "q_roll", "q_both", "q_roll_ub", "q_both_ub"):
        per = sorted(pct(r["q0"], r[key]) for r in rows)
        summary[key] = {
            "median": statistics.median(per) if per else 0.0,
            "mean": statistics.mean(per) if per else 0.0,
            "p25": _percentile(per, 0.25),
            "p75": _percentile(per, 0.75),
            "p90": _percentile(per, 0.90),
            "max": per[-1] if per else 0.0,
            "pct_gt_5": (sum(1 for p in per if p > 5.0) / len(per) * 100.0) if per else 0.0,
            "total": pct(sum(r["q0"] for r in rows), sum(r[key] for r in rows)),
            "improved": sum(1 for p in per if p > 0.05),
            "regressed": sum(1 for p in per if p < -0.05),
            "per_session": [round(p, 2) for p in per],
        }
    for label, lo, hi in TIERS:
        sub = [r for r in rows if lo <= r["peak"] < hi]
        if not sub:
            continue
        q0 = sum(r["q0"] for r in sub)
        summary["tiers"].append({
            "label": label, "n": len(sub), "avg_peak": int(sum(r["peak"] for r in sub) / len(sub)),
            "hooks": pct(q0, sum(r["q_hooks"] for r in sub)),
            "roll": pct(q0, sum(r["q_roll"] for r in sub)),
            "both": pct(q0, sum(r["q_both"] for r in sub)),
            "roll_ub": pct(q0, sum(r["q_roll_ub"] for r in sub)),
            "both_ub": pct(q0, sum(r["q_both_ub"] for r in sub)),
            "hooks_median": statistics.median([pct(r["q0"], r["q_hooks"]) for r in sub]),
            "both_median": statistics.median([pct(r["q0"], r["q_both"]) for r in sub]),
        })
    for c, v in cats_all.most_common():
        summary["cats"][c] = {"n": catn_all[c], "saved_tok": v // BYTES_PER_TOK}
    summary["saved_tool_tok"] = sum(r["saved_bytes"] for r in rows) // BYTES_PER_TOK
    summary["tool_tok"] = sum(r["tool_bytes"] for r in rows) // BYTES_PER_TOK
    summary["overhead_tok_per_turn"] = overhead_tok
    summary["rows"] = rows

    if not quiet:
        print(f"=== Real-Session Replay: {summary['n']} local Antigravity sessions, {summary['turns']:,} turns ===")
        print("Hooks replayed on recorded tool calls/results; exact per-turn tokens (transcript or conversations/*.db).")
        print("Quota = 1x input + 0.05x cache read + 6x output. Negative = less quota.\n")
        print(f"Rollover policy: {policy} ('live' = only where the live tick can fire; 'UB' = any generation, upper bound)")
        hdr = f"{'Tier':22s} | {'n':>3s} | {'AvgPeak':>7s} | {'Hooks only':>10s} | {'Handoff live':>12s} | {'Hooks+HO live':>13s} | {'Handoff UB':>10s} | {'Hooks+HO UB':>11s}"
        print(hdr)
        print("-" * len(hdr))
        for t in summary["tiers"]:
            print(f"{t['label']:22s} | {t['n']:3d} | {t['avg_peak'] // 1000:5d}k | {-t['hooks']:+9.1f}% | {-t['roll']:+11.1f}% | {-t['both']:+12.1f}% | {-t['roll_ub']:+9.1f}% | {-t['both_ub']:+10.1f}%")
        print("-" * len(hdr))
        for lab, k2 in (("Per-session median", "median"), ("Per-session mean", "mean"), ("Total quota-weighted", "total")):
            q = summary
            print(f"{lab:22s} | {summary['n']:3d} | {'':>7s} | {-q['q_hooks'][k2]:+9.1f}% | {-q['q_roll'][k2]:+11.1f}% | {-q['q_both'][k2]:+12.1f}% | {-q['q_roll_ub'][k2]:+9.1f}% | {-q['q_both_ub'][k2]:+10.1f}%")
        print("Regressed sessions: " + ", ".join(f"{k}={summary[k]['regressed']}" for k in ("q_hooks", "q_roll", "q_both", "q_roll_ub", "q_both_ub")))
        print(f"\nTool-result tokens: {summary['tool_tok']:,} recorded, {summary['saved_tool_tok']:,} removed by hooks "
              f"({(summary['saved_tool_tok'] / summary['tool_tok'] * 100) if summary['tool_tok'] else 0:.1f}%).")
        print(f"Plugin prompt overhead charged on every turn: {overhead_tok:,} tok (rules + skill/agent descriptions).")
        print(f"Sessions improved (hooks only): {summary['q_hooks']['improved']}/{summary['n']}, regressed: {summary['q_hooks']['regressed']}")
        print("By category (calls, tool tokens removed):")
        for c, v in summary["cats"].items():
            print(f"  {c:22s} n={v['n']:4d}  {v['saved_tok']:>8,d} tok")
        if detail:
            print("\nPer session:")
            for r in sorted(rows, key=lambda x: -x["peak"]):
                print(f"  {r['app'][:14]:14s} {r['id'][:8]} src={r['src']:10s} turns={r['turns']:4d} peak={r['peak'] // 1000:4d}k "
                      f"hooks={-pct(r['q0'], r['q_hooks']):+6.1f}% roll={-pct(r['q0'], r['q_roll']):+6.1f}% both={-pct(r['q0'], r['q_both']):+6.1f}% "
                      f"saved={r['saved_bytes'] // 4:,}tok/{r['tool_bytes'] // 4:,}tok")
    if json_out:
        Path(json_out).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary



def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--json", default=None)
    ap.add_argument("--modes", action="store_true", help="compare Mode 1 (Lossless) vs Mode 2 (Opt-in)")
    ap.add_argument("--report", action="store_true", help="with --modes: print distribution, per-type, and weight-sensitivity tables")
    ap.add_argument("--weights", default=None, help="cost proxy weights, e.g. input=1,cache=0.05,output=6")
    ap.add_argument("--policy", choices=("auto", "v1"), default="auto",
                    help="rollover policy to simulate: shipped adaptive 'auto' or v1.0.0 fixed 110k cap")
    a = ap.parse_args()
    w = parse_weights(a.weights)
    if a.modes:
        run_modes(policy=a.policy, json_out=a.json, report=a.report, weights=w)
        return 0
    s = run(a.detail, a.json, policy=a.policy, weights=w)
    return 0 if s["n"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
