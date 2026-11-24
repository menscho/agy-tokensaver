"""Antigravity IDE + CLI transcript and conversations/*.db protobuf reader with mtime cache."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from config import APP_DIRS, AUTO_CAP_MIN, MOMENTUM_MIN_TURNS, MOMENTUM_WINDOW_SEC, SETTLE_GAP_SEC, W_CR, W_IN, W_OUT, _SIM_CFG, parse_iso_ts
from rollover import RolloverTracker, _p_continue, _simulate_rollover_session

_tx_cache: dict[str, tuple[Any, dict[str, Any]]] = {}

def _rv(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if x < 0x80:
            return r, i


def _pb_fields(b: bytes) -> list[tuple[int, int, Any]] | None:
    out: list[tuple[int, int, Any]] = []
    i = 0
    try:
        while i < len(b):
            k, i = _rv(b, i)
            f, w = k >> 3, k & 7
            if f == 0:
                return None
            if w == 0:
                v, i = _rv(b, i)
                out.append((f, w, v))
            elif w == 1:
                i += 8
            elif w == 5:
                i += 4
            elif w == 2:
                ln, i = _rv(b, i)
                out.append((f, w, b[i:i + ln]))
                i += ln
            else:
                return None
        if i != len(b):
            return None
    except IndexError:
        return None
    return out


def _db_gen_usage(db: Path) -> list[tuple[int, int, int]]:
    """(input, cache_read, output) for EVERY gen_metadata row of conversations/<id>.db, in idx order
    (zero rows kept so rows stay aligned with transcript PLANNER_RESPONSE records).
    gen_metadata field 1.4: 2=input, 5=cache_read, 3=output. Opened read-only."""
    res: list[tuple[int, int, int]] = []
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = con.execute("select idx, data from gen_metadata order by idx").fetchall()
        finally:
            con.close()
    except Exception:
        return res
    for _idx, data in rows:
        usage: dict[int, int] = {}
        for f, w, v in _pb_fields(bytes(data or b"")) or []:
            if f == 1 and w == 2:
                for f2, w2, v2 in _pb_fields(v) or []:
                    if f2 == 4 and w2 == 2:
                        for f3, w3, v3 in _pb_fields(v2) or []:
                            if w3 == 0:
                                usage[f3] = int(v3)
                        break
                break
        res.append((usage.get(2, 0), usage.get(5, 0), usage.get(3, 0)))
    return res


def _fsig(p: Path | None) -> tuple[float, int]:
    if p is None:
        return (0.0, -1)
    try:
        st = p.stat()
        return (st.st_mtime, st.st_size)
    except OSError:
        return (0.0, -1)


def _session_paths(app: Path, sid: str) -> tuple[Path | None, Path | None]:
    tx = app / "brain" / sid / ".system_generated" / "logs" / "transcript.jsonl"
    db = app / "conversations" / f"{sid}.db"
    return (tx if tx.is_file() else None), (db if db.is_file() else None)


def discover_session_sources() -> list[tuple[float, str, Path | None, Path | None]]:
    """All local sessions of the IDE (~/.gemini/antigravity) AND the CLI (~/.gemini/antigravity-cli):
    (last-modified, conversation_id, transcript-or-None, db-or-None), newest first."""
    out: list[tuple[float, str, Path | None, Path | None]] = []
    seen: set[str] = set()
    for app in APP_DIRS:
        ids: set[str] = set()
        try:
            if (app / "brain").is_dir():
                ids |= {p.name for p in (app / "brain").iterdir() if p.is_dir()}
            if (app / "conversations").is_dir():
                ids |= {p.stem for p in (app / "conversations").glob("*.db")}
        except OSError:
            continue
        for sid in ids:
            if sid in seen:
                continue
            tx, db = _session_paths(app, sid)
            if tx is None and db is None:
                continue
            seen.add(sid)
            wal = Path(str(db) + "-wal") if db else None
            mt = max(_fsig(tx)[0], _fsig(db)[0], _fsig(wal)[0])
            out.append((mt, sid, tx, db))
    out.sort(key=lambda r: r[0], reverse=True)
    return out


_BG_START = re.compile(r"running as a background task with task id: ([A-Za-z0-9_.:/-]+)")  # stops at JSON "\n"
_SUBAGENTS = re.compile(r"You have (\d+) active subagent")


def _has_tool_calls(tc: Any) -> bool:
    if not tc:
        return False
    if isinstance(tc, str):
        s = tc.strip()
        return bool(s) and s not in ("[]", "None", "null")
    return bool(tc)


def _scan_transcript(tx: Path) -> dict[str, Any]:
    """One pass over transcript.jsonl: per-PLANNER_RESPONSE metadata + session facts.
    planner rows: {"ts", "final" (no tool calls), "quiet" (no background task / subagent pending),
    "tok" (input, cache_read, output) - zeros when the transcript carries no token fields}."""
    planners: list[dict[str, Any]] = []
    users = img_reads = checkpoints = 0
    last_goal = ""
    is_handoff = False
    bg: set[str] = set()
    subagents = 0
    with tx.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"read_file"' in line and any(ext in line.lower() for ext in (".png", ".jpg", ".jpeg", ".webp")):
                img_reads += 1
            if '"PLANNER_RESPONSE"' in line:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") != "PLANNER_RESPONSE":
                    continue
                final = not _has_tool_calls(obj.get("tool_calls"))
                planners.append({
                    "ts": parse_iso_ts(str(obj.get("created_at") or "")),
                    "final": final,
                    "quiet": final and not bg and subagents == 0,
                    "tok": (int(obj.get("input_tokens") or 0), int(obj.get("cache_read_tokens") or 0),
                            int(obj.get("output_tokens") or 0)),
                    "model": str(obj.get("model") or ""),
                })
                continue
            if '"USER_INPUT"' in line:
                try:
                    uobj = json.loads(line)
                except Exception:
                    continue
                if uobj.get("type") != "USER_INPUT":
                    continue
                users += 1
                bg.clear()          # a new user run: earlier background chatter is no longer "pending"
                uc = str(uobj.get("content") or "")
                if "# Session Continuation Handoff" in uc:
                    is_handoff = True
                m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", uc, re.S)
                clean_g = (m.group(1) if m else uc).strip()
                if clean_g and not clean_g.startswith("# Session Continuation Handoff"):
                    last_goal = clean_g[:220]
                continue
            if '"CHECKPOINT"' in line:
                checkpoints += 1
            if "background task" in line:
                for m in _BG_START.finditer(line):
                    bg.add(m.group(1).rstrip(".:"))
            if "active subagent" in line:
                ms = _SUBAGENTS.findall(line)
                if ms:
                    subagents = int(ms[-1])
            if bg and '"SYSTEM_MESSAGE"' in line:
                for tid in [t for t in bg if t in line]:
                    bg.discard(tid)
    return {"planners": planners, "users": users, "img_reads": img_reads, "checkpoints": checkpoints,
            "last_goal": last_goal, "is_handoff": is_handoff}


def _settled_flags(meta: list[dict[str, Any] | None], has_user: bool) -> list[bool]:
    """settled[k] == True iff the LIVE tick could fire a rollover right after generation k:
    the agent run has ended (final answer, no tool calls), nothing runs in the background (no pending
    background task / subagent), the conversation is user-driven (not a subagent), the chat then
    stays idle >= SETTLE_GAP_SEC (or it is the last generation), and it had momentum
    (>= MOMENTUM_MIN_TURNS generations in the last MOMENTUM_WINDOW_SEC). Unknown metadata => False."""
    n = len(meta)
    out = [False] * n
    if not has_user:
        return out
    ts = [m.get("ts") if m else None for m in meta]
    for k, m in enumerate(meta):
        if not m or not m.get("quiet") or ts[k] is None:
            continue
        nxt = next((t for t in ts[k + 1:] if t is not None), None)
        if nxt is not None and nxt - ts[k] < SETTLE_GAP_SEC:
            continue
        recent = sum(1 for t in ts[max(0, k - 40):k + 1] if t is not None and ts[k] - t <= MOMENTUM_WINDOW_SEC)
        out[k] = recent >= MOMENTUM_MIN_TURNS
    return out


# ----------------------------------------------------------------------------- causal features

def _build_session_record(sid: str, app_name: str, tx: Path | None, db: Path | None) -> dict[str, Any] | None:
    scan: dict[str, Any] = {"planners": [], "users": 0, "img_reads": 0, "checkpoints": 0, "last_goal": "",
                            "is_handoff": False}
    if tx is not None:
        try:
            scan = _scan_transcript(tx)
        except OSError:
            pass
    planners = scan["planners"]
    turn_tuples: list[tuple[int, int, int]] = []
    meta: list[dict[str, Any] | None] = []
    src = "transcript"
    if any(any(p["tok"]) for p in planners):
        for p in planners:
            if any(p["tok"]):
                turn_tuples.append(p["tok"])
                meta.append(p)
    elif db is not None:
        src = "db"
        rows = _db_gen_usage(db)
        off = len(planners) - len(rows)     # align from the end: the latest generation matters most
        for r, tok in enumerate(rows):
            if not any(tok):
                continue
            turn_tuples.append(tok)
            k = r + off
            meta.append(planners[k] if 0 <= k < len(planners) else None)
    if not turn_tuples:
        return None

    fallback_ts = max(_fsig(tx)[0], _fsig(db)[0], _fsig(Path(str(db) + "-wal") if db else None)[0])
    tracker = RolloverTracker()
    tot_in = tot_cr = tot_out = drops = tail_2049 = 0
    drop_rebuild_tok = idle_drop_tok = midrun_drop_tok = 0
    prev_cr = prev_tot = 0
    for i, (inp, cr, out) in enumerate(turn_tuples):
        tracker.push(inp, cr, out)
        tot_in += inp
        tot_cr += cr
        tot_out += out
        tot_i = inp + cr
        if i > 0:
            if prev_cr >= 4000 and cr < prev_cr * 0.8:
                drops += 1
            if prev_tot >= 20000 and inp >= 20000 and (cr == 0 or cr < prev_tot * 0.5) and not (tot_i < prev_tot * 0.85):
                drop_rebuild_tok += inp
                pm = meta[i - 1] if i - 1 < len(meta) else None
                if pm and pm.get("final"):
                    idle_drop_tok += inp
                else:
                    midrun_drop_tok += inp
            if 2048 <= inp <= 2060 and cr >= 4000:
                tail_2049 += 1
        prev_cr = cr
        prev_tot = tot_i
    turns = len(turn_tuples)
    last_model = next((p["model"] for p in reversed(planners) if p.get("model")), "")
    stamps = [m["ts"] for m in meta if m and m.get("ts")]
    first_ts = stamps[0] if stamps else fallback_ts
    last_ts = stamps[-1] if stamps else fallback_ts
    if planners and planners[-1].get("ts"):
        last_ts = max(last_ts, planners[-1]["ts"])

    has_user = scan["users"] > 0
    settled = _settled_flags(meta, has_user)
    last_p = planners[-1] if planners else None
    last_settled = bool(has_user and last_p and last_p.get("quiet"))
    hi_pts = [k for k, ok in enumerate(settled) if ok and sum(turn_tuples[k][:2]) >= AUTO_CAP_MIN]
    hi_cont = sum(1 for k in hi_pts if k < turns - 1)

    feat = tracker.features()
    ttl_cliff = 90 if "hydrogen" in last_model.lower() else 150
    gaps: list[float | None] = []
    for k in range(turns):
        t0 = meta[k].get("ts") if k < len(meta) and meta[k] else None
        t1 = meta[k + 1].get("ts") if k + 1 < len(meta) and meta[k + 1] else None
        gaps.append(max(0.0, t1 - t0) if (t0 and t1 and t1 >= t0) else None)
    sim_cfg = {**_SIM_CFG, "p_continue": _p_continue(0, 0)}     # no cross-session history: prior
    sim_in, sim_cr, sim_rollovers = _simulate_rollover_session(
        turn_tuples, None, settled=settled, cfg=sim_cfg, gaps=gaps, ttl_cliff=ttl_cliff,
    )
    ub_in, ub_cr, ub_rollovers = _simulate_rollover_session(turn_tuples, None, gaps=gaps, ttl_cliff=ttl_cliff)

    actual_quota = tot_in * W_IN + tot_cr * W_CR + tot_out * W_OUT
    sim_quota = sim_in * W_IN + sim_cr * W_CR + tot_out * W_OUT
    uncached_baseline_quota = (tot_in + tot_cr) * W_IN + tot_out * W_OUT
    cur_in, cur_cr, _ = turn_tuples[-1]
    wden = sum(t[0] for t in turn_tuples[1:]) + sum(t[1] for t in turn_tuples[1:])
    return {
        "conversation_id": sid,
        "app": app_name,
        "source": src,
        "turns": turns,
        "model": last_model or "unknown",
        "cur_tot": cur_in + cur_cr,
        "peak_ctx": tracker.peak,
        "cur_in": cur_in,
        "cur_cr": cur_cr,
        "tot_in": tot_in,
        "tot_cr": tot_cr,
        "tot_out": tot_out,
        "out_per_turn": tot_out // turns,
        "warm_hit_pct": round((sum(t[1] for t in turn_tuples[1:]) / wden) * 100, 1) if wden else 0.0,
        "drops": drops,
        "drop_rebuild_tok": drop_rebuild_tok,
        "idle_drop_tok": idle_drop_tok,
        "midrun_drop_tok": midrun_drop_tok,
        "tail_2049": tail_2049,
        "img_reads": scan["img_reads"],
        "first_ts": first_ts,
        "last_ts": last_ts,
        "turn_timestamps": stamps[-30:],
        "ttl_cliff_sec": ttl_cliff,
        "cache_burn_per_turn": int((cur_in + cur_cr) * W_CR),
        "actual_quota": actual_quota,
        "uncached_baseline_quota": uncached_baseline_quota,
        "sim_in": sim_in,
        "sim_cr": sim_cr,
        "sim_quota": sim_quota,
        "sim_rollovers": sim_rollovers,
        "sim_upper_bound_quota": ub_in * W_IN + ub_cr * W_CR + tot_out * W_OUT,
        "sim_upper_bound_rollovers": ub_rollovers,
        "user_inputs": scan["users"],
        "last_settled": last_settled,
        "settled_hi": len(hi_pts),
        "settled_hi_continued": hi_cont,
        "rollover_features": feat,
        "is_handoff_session": scan["is_handoff"],
        "goal_preview": scan["last_goal"],
    }


def parse_session_cached(sid: str, tx: Path | None, db: Path | None, app_name: str = "") -> dict[str, Any] | None:
    """Session record from transcript.jsonl and/or conversations/<id>.db (token fallback), cached by
    (mtime, size) of transcript, db and db-wal."""
    wal = Path(str(db) + "-wal") if db else None
    sig = (_fsig(tx), _fsig(db), _fsig(wal))
    key = f"{tx}|{db}"
    cached = _tx_cache.get(key)
    if cached and cached[0] == sig:
        return cached[1]
    if not app_name:
        ref = tx.parents[4] if tx is not None else (db.parent.parent if db is not None else None)
        app_name = ref.name if ref is not None else ""
    try:
        res = _build_session_record(sid, app_name, tx, db)
    except Exception:
        res = None
    if res is None:
        return None
    _tx_cache[key] = (sig, res)
    if len(_tx_cache) > 300:
        for ok in list(_tx_cache)[:80]:
            _tx_cache.pop(ok, None)
    return res


def parse_transcript_cached(tx: Path) -> dict[str, Any] | None:
    """Backward-compatible entry point: <app>/brain/<id>/.system_generated/logs/transcript.jsonl.
    Falls back to <app>/conversations/<id>.db for token usage when the transcript has none."""
    try:
        sid = tx.parents[2].name
        app = tx.parents[4]
    except IndexError:
        return None
    db = app / "conversations" / f"{sid}.db"
    return parse_session_cached(sid, tx if tx.is_file() else None, db if db.is_file() else None, app.name)


