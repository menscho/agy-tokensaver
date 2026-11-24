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
GEMINI_HOME = Path(os.environ.get("AGY_TS_GEMINI_HOME") or (Path.home() / ".gemini"))
APP_DIRS = (GEMINI_HOME / "antigravity", GEMINI_HOME / "antigravity-cli")   # IDE + CLI (read-only)
BRAIN_DIR = GEMINI_HOME / "antigravity" / "brain"
_DEFAULT_DATA_DIR = Path.home() / ".gemini" / "antigravity" / "plugin_data" / "token-saver"
DATA_DIR = Path(os.environ.get("PLUGIN_DATA") or _DEFAULT_DATA_DIR)


def _is_sandbox_data_dir() -> bool:
    return bool(os.environ.get("PLUGIN_DATA")) and DATA_DIR.resolve() != _DEFAULT_DATA_DIR.resolve()

CFG_FILE = DATA_DIR / "hud_config.json"
ROLLOVER_STATE_FILE = DATA_DIR / "rollover_state.json"
ROLLOVER_HISTORY_FILE = DATA_DIR / "rollover_history.json"
EVENTS_FILE = DATA_DIR / "events.jsonl"

_cfg_lock = threading.Lock()
_state_lock = threading.Lock()
_tx_cache: dict[str, tuple[Any, dict[str, Any]]] = {}
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
        "profile": "zero_loss",        # "zero_loss" (0% quality loss) | "small_loss" (~0.1-1% risk)
        "auto_rollover": True,
        "mode": "prepare_only",        # "prepare_only" (safe default) | "smart_auto" | "off"
        "threshold_mode": "auto",      # "auto" | "75" | "90" | "110" | "140" | "180"
        "threshold_k": 110,
        "model": "inherit",
        "min_turns": 14,
        "cooldown_min": 20,
        "compact_reads": False,
        "concise_output": False,
        "pending_concise_sync": False,
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
            compact_reads = bool(d.get("compact_reads", False))
            concise_output = bool(d.get("concise_output", False))
            profile = str(d.get("profile") or ("small_loss" if (mode == "smart_auto" or compact_reads or concise_output) else "zero_loss"))
            if profile not in ("zero_loss", "small_loss"):
                profile = "zero_loss"
            tmode = str(d.get("threshold_mode") or "auto")
            if tmode not in ("auto", "75", "90", "110", "140", "180"):
                tmode = "auto"
            tk = max(60, min(300, int(d.get("threshold_k", 110))))
            return {
                "profile": profile,
                "auto_rollover": mode != "off",
                "mode": mode,
                "threshold_mode": tmode,
                "threshold_k": tk,
                "model": str(d.get("model") or "inherit"),
                "min_turns": max(6, min(40, int(d.get("min_turns", 14)))),
                "cooldown_min": max(5, min(120, int(d.get("cooldown_min", 20)))),
                "compact_reads": compact_reads,
                "concise_output": concise_output,
                "pending_concise_sync": bool(d.get("pending_concise_sync", False)),
            }
    except Exception:
        pass
    return default


def save_config(patch: dict[str, Any]) -> dict[str, Any]:
    with _cfg_lock:
        cur = load_config()
        merged = {**cur, **patch}
        if "profile" in patch:
            prof = str(patch["profile"])
            if prof == "small_loss":
                merged["profile"] = "small_loss"
                merged["mode"] = "smart_auto"
                merged["auto_rollover"] = True
                merged["compact_reads"] = True
                merged["concise_output"] = True
            else:
                merged["profile"] = "zero_loss"
                merged["mode"] = "prepare_only"
                merged["auto_rollover"] = True
                merged["compact_reads"] = False
                merged["concise_output"] = False
        elif "mode" in patch:
            mode = str(patch["mode"])
            if mode not in ("smart_auto", "prepare_only", "off"):
                mode = "smart_auto"
            merged["mode"] = mode
            merged["auto_rollover"] = mode != "off"
            merged["profile"] = "small_loss" if (mode == "smart_auto" or merged.get("compact_reads") or merged.get("concise_output")) else "zero_loss"
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

        concise_changed = bool(merged.get("concise_output", False)) != bool(cur.get("concise_output", False))
        defer_sync = False
        if concise_changed and _should_defer_concise_sync():
            defer_sync = True

        clean = {
            "profile": merged.get("profile", "zero_loss"),
            "auto_rollover": bool(merged["auto_rollover"]),
            "mode": merged["mode"],
            "threshold_mode": merged["threshold_mode"],
            "threshold_k": merged["threshold_k"],
            "model": str(merged.get("model") or "inherit"),
            "min_turns": max(6, min(40, int(merged.get("min_turns", 14)))),
            "cooldown_min": max(5, min(120, int(merged.get("cooldown_min", 20)))),
            "compact_reads": bool(merged.get("compact_reads", False)),
            "concise_output": bool(merged.get("concise_output", False)),
            "pending_concise_sync": defer_sync or (bool(cur.get("pending_concise_sync", False)) and not concise_changed),
        }
        _atomic_write_json(CFG_FILE, clean)
        if not clean["pending_concise_sync"]:
            _sync_concise_rule(clean["concise_output"])
        return clean


_CONCISE_RULE_LINE = "7. Concise mode (SoT/Caveman-lite): skip pleasantries, preambles, and pre/post-tool chatter; keep code, diffs, paths, errors, and root-cause debugging verbatim."


def _should_defer_concise_sync() -> bool:
    """Protect active warm KV caches (>30k tokens, idle <150s) from mid-session system-prompt edits (arXiv:2601.06007)."""
    if _is_sandbox_data_dir() or os.environ.get("AGY_TS_SYNC_RULES") == "1":
        return False
    try:
        cands = discover_session_sources()
        if not cands:
            return False
        now = time.time()
        _mt, sid, tx, db = cands[0]
        if now - _mt > 180:
            return False
        rec = parse_session_cached(sid, tx, db)
        if not rec:
            return False
        age = max(0, int(now - rec["last_ts"]))
        return rec["cur_tot"] >= 30_000 and age < rec.get("ttl_cliff_sec", 150)
    except Exception:
        return False


def flush_pending_concise_sync(force: bool = False) -> bool:
    """Apply deferred Concise Output rule once active warm cache cools or before spawning a fresh chat."""
    with _cfg_lock:
        cfg = load_config()
        if not cfg.get("pending_concise_sync"):
            return False
        if not force and _should_defer_concise_sync():
            return False
        _sync_concise_rule(bool(cfg.get("concise_output", False)))
        cfg["pending_concise_sync"] = False
        _atomic_write_json(CFG_FILE, cfg)
        return True


def _sync_concise_rule(enabled: bool) -> None:
    """Opt-in Caveman-lite output rule synced to rules/AGENTS.md only on explicit user toggle."""
    try:
        # Never mutate repo rules/AGENTS.md when running under an isolated test PLUGIN_DATA dir
        if _is_sandbox_data_dir() and os.environ.get("AGY_TS_SYNC_RULES") != "1":
            return
        agents_md = PLUGIN_ROOT / "rules" / "AGENTS.md"
        if not agents_md.is_file():
            return
        lines = [l for l in agents_md.read_text(encoding="utf-8").splitlines() if not l.startswith("7. Concise mode")]
        if enabled:
            lines.append(_CONCISE_RULE_LINE)
        agents_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def _load_rollover_state() -> dict[str, Any]:
    if not ROLLOVER_STATE_FILE.is_file():
        return {"rolled_convs": {}, "snapshotted_convs": {}, "last_rollover_ts": 0}
    try:
        d = json.loads(ROLLOVER_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(d, dict):
            rc = dict(d.get("rolled_convs") or {})
            sc = dict(d.get("snapshotted_convs") or {})
            # Backward-compatible migration: entries previously recorded under Archive Only ("snapshot")
            # must NOT block upgrading that same chat to Mode 2 ("smart_auto") later.
            if rc and "snapshotted_convs" not in d:
                hist = load_rollover_history()
                auto_prefixes = {h.get("from_conv", "") for h in hist if h.get("kind") in ("auto", "manual")}
                snap_prefixes = {h.get("from_conv", "") for h in hist if h.get("kind") == "snapshot"}
                for cid in list(rc.keys()):
                    p8 = cid[:8]
                    if p8 not in auto_prefixes and (p8 in snap_prefixes or not auto_prefixes):
                        sc[cid] = rc.pop(cid)
            return {
                "rolled_convs": rc,
                "snapshotted_convs": sc,
                "last_rollover_ts": int(d.get("last_rollover_ts") or 0),
            }
    except Exception:
        pass
    return {"rolled_convs": {}, "snapshotted_convs": {}, "last_rollover_ts": 0}


def _save_rollover_state(st: dict[str, Any]) -> None:
    # Keep last 200 entries so state file never grows unbounded
    for key in ("rolled_convs", "snapshotted_convs"):
        m = st.get(key) or {}
        if len(m) > 200:
            st[key] = dict(sorted(m.items(), key=lambda kv: kv[1], reverse=True)[:150])
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


# ----------------------------------------------------------------------------- rollover cost model
# Every constant below is either measured on the 30 local sessions or is an explicit prior.
#  * FRESH_CTX_TOK: context of the FIRST generation of a fresh conversation (system prompt + tool
#    schemas + rules + first user message). Measured: median 15,375 (IDE, n=10) / 15,350 (CLI, n=20),
#    range 10.0k-17.0k.
#  * HANDOFF_PACKET_TOK: scripts/handoff_pack.py output measured on the 10 real workspaces used in
#    those sessions (chars/4): now 180-610 tok (median 320, p90 ~390); before the densification
#    410-1,040 (median 547) while listing NO edited files (string tool_calls were not parsed).
#  * COLD_START_TOK = FRESH (15.4k) + PACKET p90 (0.4k) + ~1.2k slack for the max packet and the
#    model's resume context = 17k. (Previously an unmeasured 22k.)
#  * REDISCOVERY_TAX_TOK: re-reading files the old chat already had in context. NOT measured ->
#    kept at the old conservative 8k.
#  * Warm-up misses: fresh conversations on this machine often miss the prompt cache on their first
#    1-3 generations (cr=0 at turn 1/2 in 17 of 30 sessions). The counterfactual replays THIS
#    session's own measured warm-up miss pattern for the new conversation (charged, not assumed 0).
FRESH_CTX_TOK = 15_400
HANDOFF_PACKET_TOK = 400
COLD_START_TOK = 17_000
REDISCOVERY_TAX_TOK = 8_000
PACKET_REPLY_OUT_TOK = 150      # output of the new chat's first reply to the packet (6x weighted)
WARMUP_TURNS = 3
SETTLE_GAP_SEC = 25             # live tick fires 20..90 s after the last generation (10 s period)
MOMENTUM_WINDOW_SEC = 600
MOMENTUM_MIN_TURNS = 3

# Auto policy priors (causal: no knowledge of how many turns remain)
POLICY_HORIZON_TURNS = 12       # assumed remaining turns once a chat is past min_turns (was the oracle)
POLICY_MARGIN = 1.5             # required gain / cost ratio over that horizon
POLICY_DROP_PRIOR = (0.5, 20)   # Beta-ish prior: 0.5 TTL drops per 20 warm turns (~2.5%/turn)
AUTO_CAP_MIN = 90_000
AUTO_CAP_MAX = 220_000
# Live rollovers can only fire when an agent run has settled. Whether the user then continues in the
# new chat is unknown at decision time, so the auto policy scales its horizon by p_continue: the
# Laplace-smoothed share of this user's PAST settled points (ctx >= AUTO_CAP_MIN) that were followed
# by more generations. On the 30 local sessions that share is 0/13 -> auto-spawn backs off.
POLICY_CONTINUE_PRIOR = (1, 2)

_SIM_CFG = {"threshold_mode": "auto", "threshold_k": 110, "min_turns": 14}


# ----------------------------------------------------------------------------- session sources
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
class RolloverTracker:
    """Causal per-conversation features, updated one generation at a time.
    The SAME tracker feeds the live HUD/tick (over the real chat) and the counterfactual simulation
    (over the simulated chat), so the benchmark measures the policy that ships."""

    __slots__ = ("turns", "ctx", "peak", "growth", "drops", "compactions", "warm_in", "warm_cr",
                 "warm_miss", "_own_warm")

    def __init__(self, warm_miss: list[float] | None = None) -> None:
        self.turns = 0
        self.ctx = 0
        self.peak = 0
        self.growth = 0.0
        self.drops = 0
        self.compactions = 0
        self.warm_in = 0
        self.warm_cr = 0
        self.warm_miss: list[float] = list(warm_miss) if warm_miss is not None else []
        self._own_warm = warm_miss is None

    def classify(self, inp: int, cr: int) -> str:
        ctx, prev = inp + cr, self.ctx
        if self.turns == 0:
            return "first"
        if prev >= 20_000 and ctx < prev * 0.6:
            return "compaction"            # context shrank: Antigravity checkpoint / truncation
        if prev >= 4_000 and cr < prev * 0.7:
            return "warmup" if self.turns <= WARMUP_TURNS else "drop"   # prefix not served from cache
        return "turn"

    def push(self, inp: int, cr: int, out: int = 0) -> str:
        ev = self.classify(inp, cr)
        ctx, prev = inp + cr, self.ctx
        if ev == "compaction":
            self.compactions += 1
        elif ev == "drop":
            self.drops += 1
        if self._own_warm and 1 <= self.turns <= WARMUP_TURNS and prev > 0:
            self.warm_miss.append(min(1.0, max(0.0, (prev - cr) / prev)))
        if self.turns > 0 and ev != "compaction":
            d = ctx - prev
            if d > 0:
                self.growth = float(d) if self.growth == 0 else 0.7 * self.growth + 0.3 * d
        if self.turns > 0:
            self.warm_in += inp
            self.warm_cr += cr
        self.turns += 1
        self.ctx = ctx
        if ctx > self.peak:
            self.peak = ctx
        return ev

    def features(self) -> dict[str, Any]:
        wden = self.warm_in + self.warm_cr
        return {
            "ctx": self.ctx,
            "turns": self.turns,
            "growth": int(self.growth),
            "drops": self.drops,
            "compactions": self.compactions,
            "warm_turns": max(0, self.turns - 1 - WARMUP_TURNS),
            "warm_hit_pct": round(self.warm_cr / wden * 100.0, 1) if wden else 0.0,
            "warm_miss": [round(m, 3) for m in self.warm_miss[:WARMUP_TURNS]],
        }


def rollover_cost_tok(feat: dict[str, Any] | None = None, cold_start: int = COLD_START_TOK,
                      rediscovery_tax: int = REDISCOVERY_TAX_TOK) -> int:
    """Uncached tokens a rollover costs: cold start + re-read tax + expected warm-up cache misses of
    the new chat (this chat's own measured warm-up pattern applied to the new chat's size)."""
    base = cold_start + rediscovery_tax
    wm = (feat or {}).get("warm_miss") or []
    return int(base + sum(min(1.0, max(0.0, float(m))) for m in wm[:WARMUP_TURNS]) * base)


def rollover_policy(feat: dict[str, Any] | None, cfg: dict[str, Any] | None = None,
                    cold_start: int = COLD_START_TOK, rediscovery_tax: int = REDISCOVERY_TAX_TOK) -> dict[str, Any]:
    """THE rollover policy (live tick, HUD threshold and counterfactual all call this).
    Causal: uses only the current chat's context size, turn count, cache-drop history and measured
    warm-up behaviour. Auto mode picks the smallest context C at which a rollover pays back
    POLICY_MARGIN x its cost within POLICY_HORIZON_TURNS turns:
        gain/turn = w * (C - cost),  w = 0.05 + p_drop * 0.95  (cache read + expected TTL-drop rebuild)
        net cost  = cost + warmup - 0.05 * C  (the replaced old-chat turn is not paid)
    """
    cfg = cfg or _SIM_CFG
    f = feat or {}
    cost = rollover_cost_tok(f, cold_start, rediscovery_tax)
    min_turns = int(cfg.get("min_turns", 14))
    tmode = str(cfg.get("threshold_mode", "auto"))
    drops = int(f.get("drops", 0))
    warm_turns = int(f.get("warm_turns", 0))
    a, b = POLICY_DROP_PRIOR
    p_drop = (drops + a) / (warm_turns + b)
    w = W_CR + min(0.5, p_drop) * (W_IN - W_CR)
    p_cont = min(1.0, max(0.0, float(f.get("p_continue", cfg.get("p_continue", 1.0)))))
    paused = False
    if tmode != "auto" and tmode.isdigit():
        cap = int(tmode) * 1000
        is_auto = False
        reason = f"Manual threshold fixed at {cap // 1000}k tokens."
    else:
        h, m = POLICY_HORIZON_TURNS * p_cont, POLICY_MARGIN
        cap_raw = (m * cost + h * w * cost) / (h * w + m * W_CR) if h > 0 else float("inf")
        paused = cap_raw > AUTO_CAP_MAX
        cap = int(min(AUTO_CAP_MAX, max(AUTO_CAP_MIN, cap_raw)) // 1000 * 1000)
        is_auto = True
        if paused:
            reason = (f"Auto rollover paused: only ~{p_cont * 100:.0f}% of your past settled long chats "
                      f"continued afterwards, so a ~{cost // 1000}k rollover would not pay back.")
        else:
            reason = (f"Auto {cap // 1000}k: rollover costs ~{cost // 1000}k uncached; with "
                      f"{drops} cache drop(s) in {warm_turns} warm turns (~{p_drop * 100:.1f}%/turn) it pays back "
                      f"{m:g}x within {h:.0f} expected turns above {cap // 1000}k.")
    ctx = int(f.get("ctx", 0))
    gain_turn = w * max(0, ctx - cost)
    be = (cost - W_CR * ctx) / gain_turn if gain_turn > 0 else None
    roll = bool(f) and not paused and ctx >= cap and int(f.get("turns", 0)) >= min_turns
    return {
        "paused": paused,
        "p_continue": round(p_cont, 3),
        "cap": cap,
        "cap_k": cap // 1000,
        "is_auto": is_auto,
        "reason": reason,
        "roll": roll,
        "cost_tok": cost,
        "p_drop": round(p_drop, 4),
        "break_even_turns": round(be, 1) if be is not None and be > 0 else 0.0,
    }


def _simulate_rollover_session(
    turns: list[tuple[int, int, int]],
    cap: int | None = None,
    rediscovery_tax: int = REDISCOVERY_TAX_TOK,
    settled: list[bool] | None = None,
    cfg: dict[str, Any] | None = None,
    cold_start: int = COLD_START_TOK,
    detail: bool = False,
) -> Any:
    """
    Causal counterfactual: replays the session generation by generation and lets `rollover_policy`
    (the live policy) decide after each generation whether to roll over - it never sees the future.
      * cap=None -> auto policy (the shipped default); cap=int -> manual threshold at that cap.
      * settled  -> optional per-generation flags (see _settled_flags): rollover may only fire after
        generations where the live tick could fire. None = assume every generation is eligible
        (UPPER BOUND: the live tick cannot roll over in the middle of an agent run).
      * per-conversation turn counter: after a rollover the new chat must reach min_turns again.
      * cost of a rollover: the NEXT generation runs in the new chat with cold_start +
        rediscovery_tax + its real new content all uncached, followed by this session's own
        measured warm-up cache misses; a rollover after the final generation is still paid
        (cold start + PACKET_REPLY_OUT_TOK output folded into input-equivalents).
      * real TTL cache drops are replayed on the (smaller) simulated context; a real compaction
        (context shrinks > 40%) is NOT a cache drop: the smaller simulated chat keeps its warm cache
        (unless it is at least as large as the real chat was, then it compacts the same way).
    Returns (sim_in, sim_cr, rollovers) [, detail list when detail=True].
    """
    log: list[dict[str, Any]] = []
    if not turns:
        return (0.0, 0.0, 0, log) if detail else (0.0, 0.0, 0)
    pcfg = dict(cfg or _SIM_CFG)
    if cap is not None:
        pcfg["threshold_mode"] = str(max(1, int(cap) // 1000))
    n = len(turns)
    base = cold_start + rediscovery_tax
    real = RolloverTracker()
    conv = RolloverTracker()
    sim_in = sim_cr = 0.0
    rollovers = 0
    rolled = False
    pending = False
    j = 0                      # generation index inside the current simulated (new) chat
    sim_ctx = 0
    for idx, (inp, cr, out) in enumerate(turns):
        prev_real = real.ctx
        g_before = real.growth
        ev = real.push(inp, cr, out)
        if not rolled:
            ti, tc = inp, cr
            sim_ctx = inp + cr
        else:
            old = sim_ctx
            delta = (inp + cr) - prev_real
            if pending:
                new = max(0, delta) if ev != "compaction" else int(g_before)
                sim_ctx = base + new
                ti, tc = sim_ctx, 0
                pending = False
                j = 0
            else:
                j += 1
                if ev == "compaction":
                    new = int(g_before)
                    if old + new >= prev_real * 0.95:      # as large as the real chat -> compacts too
                        sim_ctx = inp + cr
                        ti, tc = inp, cr
                    else:
                        sim_ctx = old + new
                        ti, tc = min(inp, sim_ctx), max(0, sim_ctx - min(inp, sim_ctx))
                else:
                    sim_ctx = max(FRESH_CTX_TOK, old + delta)
                    if ev == "drop":
                        ti, tc = sim_ctx, 0
                    else:
                        wm = real.warm_miss
                        miss = wm[j - 1] if 1 <= j <= len(wm) and j <= WARMUP_TURNS else 0.0
                        ti = min(sim_ctx, min(inp, sim_ctx) + miss * old)
                        tc = sim_ctx - ti
        sim_in += ti
        sim_cr += tc
        conv.push(int(ti), int(tc), out)
        pol = rollover_policy(conv.features(), pcfg, cold_start, rediscovery_tax)
        if pol["roll"] and (settled is None or (idx < len(settled) and settled[idx])):
            rollovers += 1
            rolled = True
            if detail:
                log.append({"after_turn": idx, "ctx": conv.ctx, "conv_turns": conv.turns, "cap": pol["cap"],
                            "remaining": n - 1 - idx})
            if idx == n - 1:
                sim_in += base + PACKET_REPLY_OUT_TOK * W_OUT / W_IN   # spawned chat nobody used
            else:
                pending = True
            conv = RolloverTracker(warm_miss=real.warm_miss)
    if detail:
        return sim_in, sim_cr, rollovers, log
    return sim_in, sim_cr, rollovers


# ----------------------------------------------------------------------------- session parsing
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
    sim_cfg = {**_SIM_CFG, "p_continue": _p_continue(0, 0)}     # no cross-session history: prior
    sim_in, sim_cr, sim_rollovers = _simulate_rollover_session(turn_tuples, None, settled=settled, cfg=sim_cfg)
    ub_in, ub_cr, ub_rollovers = _simulate_rollover_session(turn_tuples, None)

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


def _p_continue(continued: int, points: int) -> float:
    a, b = POLICY_CONTINUE_PRIOR
    return (continued + a) / (points + b)


def continuation_probability(sessions: list[dict[str, Any]], exclude: str = "") -> float:
    """Share of past settled long-chat points (live tick could have rolled over) that the user
    followed with more generations - learned from this machine's own history, causal for the
    current chat (the current chat itself is excluded)."""
    pts = cont = 0
    for s in sessions:
        if s.get("conversation_id") == exclude:
            continue
        pts += int(s.get("settled_hi", 0))
        cont += int(s.get("settled_hi_continued", 0))
    return _p_continue(cont, pts)


def compute_adaptive_threshold(active: dict[str, Any] | None, cfg: dict[str, Any]) -> dict[str, Any]:
    """Live rollover threshold for the current chat = rollover_policy (same function the
    counterfactual simulation uses)."""
    if not active:
        pol = rollover_policy({"ctx": 0, "turns": 0, "drops": 0, "warm_turns": 0, "warm_miss": []}, cfg)
        if pol["is_auto"]:
            pol["reason"] = (f"Auto baseline {pol['cap_k']}k (rollover cost ~{pol['cost_tok'] // 1000}k) - "
                             "adapts live to cache drops and warm-up misses.")
        pol["roll"] = False
        return pol
    feat = active.get("rollover_features")
    if not feat:
        feat = {
            "ctx": int(active.get("cur_tot", 0)),
            "turns": int(active.get("turns", 0)),
            "drops": int(active.get("drops", 0)),
            "warm_turns": max(0, int(active.get("turns", 0)) - 1 - WARMUP_TURNS),
            "warm_miss": [],
        }
    if "p_continue" in active:
        feat = {**feat, "p_continue": active["p_continue"]}
    return rollover_policy(feat, cfg)


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
    out_note = "Concise ON (est. -20%..-35% on new chats)" if concise_on else "Unchanged in Zero Quality Loss mode"

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
    cmd = [sys.executable, str(PLUGIN_ROOT / "scripts" / "handoff_pack.py"), ws, "--no-save"]
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
        flush_pending_concise_sync(force=True)
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

    # IDE + CLI sessions; token usage falls back to conversations/<id>.db when the transcript has none
    cands = discover_session_sources()
    if conv_id:
        for app in APP_DIRS:
            tx, db = _session_paths(app, conv_id)
            if tx is not None or db is not None:
                active = parse_session_cached(conv_id, tx, db, app.name)
                if active:
                    break

    for _mt, sid, tx, db in cands[:120]:
        rec = parse_session_cached(sid, tx, db)
        if not rec:
            continue
        if active is None:
            active = rec
        all_sessions.append(rec)

    cfg = load_config()
    if active:
        active = {**active, "p_continue": continuation_probability(all_sessions, active["conversation_id"])}
    adaptive = compute_adaptive_threshold(active, cfg)

    if active:
        age = max(0, int(now - active["last_ts"]))
        ttl_cliff = int(active.get("ttl_cliff_sec") or 150)
        is_cold = age >= ttl_cliff
        recent_10m = sum(1 for t in active.get("turn_timestamps", []) if (now - t) <= 600)
        active = {
            **{k: v for k, v in active.items() if k != "turn_timestamps"},
            "idle_sec": age,
            "ttl_remaining_sec": max(0, ttl_cliff - age),
            "is_cold": is_cold,
            "cold_rebuild_save_est": max(0, int(active["cur_tot"]) - COLD_START_TOK) if is_cold else 0,
            "recent_turns_10m": recent_10m,
        }

    hooks = load_events_summary()
    history = load_rollover_history()
    savings = compute_savings_analytics(all_sessions, hooks, history, cfg)

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
    """Single anti-spam adaptive + cold-cache rollover evaluation tick (CADOC arXiv:2609.37012)."""
    global _last_auto_event
    # Apply any deferred rules/AGENTS.md sync as soon as no warm large cache is active
    flush_pending_concise_sync(force=False)
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
    now = int(time.time())

    # Only IDE chats (agentapi spawns IDE conversations); never a CLI session from here.
    if act.get("app") and act.get("app") != "antigravity":
        return
    # Agent run must be finished: spawning a new chat mid-run would NOT stop the old one.
    if not act.get("last_settled"):
        return

    # Cold-cache trigger (arXiv:2609.37012 / arXiv:2607.19214): once a >=75k chat's KV cache has
    # ALREADY expired (idle >= ttl_cliff), resuming it pays cur_tot uncached vs 17k fresh handoff
    # (instant Turn-1 payback, zero warm cache wasted).
    is_cold_payback = bool(
        act.get("is_cold")
        and act.get("cur_tot", 0) >= 75_000
        and act.get("turns", 0) >= 6
        and act.get("idle_sec", 0) <= 600
    )

    if not is_cold_payback:
        # Warm-chat guardrails: shared causal policy + active momentum + settled window
        if not st["adaptive"].get("roll"):
            return
        if act.get("recent_turns_10m", 0) < 3:
            return
        if not (20 <= act["idle_sec"] <= 600):
            return

    spawn = (mode == "smart_auto") and st.get("can_new_chat", False)
    bucket_key = "rolled_convs" if spawn else "snapshotted_convs"

    with _state_lock:
        rstate = _load_rollover_state()
        # Mode 1 ("snapshot") NEVER blocks upgrading the same chat to Mode 2 ("smart_auto") later!
        if cid in rstate[bucket_key]:
            return
        cooldown_sec = cfg.get("cooldown_min", 20) * 60
        if spawn and now - rstate.get("last_rollover_ts", 0) < cooldown_sec:
            return
        rstate[bucket_key][cid] = now
        if spawn:
            rstate["last_rollover_ts"] = now
        _save_rollover_state(rstate)

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
