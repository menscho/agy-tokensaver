"""Configuration, paths, constants, and rollover state persistence."""
from __future__ import annotations

import datetime
import json
import os
import threading
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

# Quota cost weights (matches stats.py): uncached input 1.0x, cache read 0.05x, output 6.0x
W_IN = 1.0
W_CR = 0.05
W_OUT = 6.0


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


_PROFILE_PRESETS: dict[str, dict[str, Any]] = {
    "small_loss":         {"mode": "smart_auto",   "auto_rollover": True, "compact_reads": True,  "concise_output": True},
    "handoff_only":       {"mode": "smart_auto",   "auto_rollover": True, "compact_reads": True,  "concise_output": False},
    "concise_no_handoff": {"mode": "prepare_only", "auto_rollover": True, "compact_reads": True,  "concise_output": True},
    "compact_reads":      {"mode": "prepare_only", "auto_rollover": True, "compact_reads": True,  "concise_output": False},
    "zero_loss":          {"mode": "prepare_only", "auto_rollover": True, "compact_reads": False, "concise_output": False},
}


def _infer_profile(mode: str, compact_reads: bool, concise_output: bool) -> str:
    if mode == "smart_auto":
        return "small_loss" if concise_output else "handoff_only"
    if concise_output:
        return "concise_no_handoff"
    if compact_reads:
        return "compact_reads"
    return "zero_loss"


def load_config() -> dict[str, Any]:
    default = {
        "profile": "zero_loss",        # "zero_loss" (Mode 1, 0% loss default) | "compact_reads" (2a) | "concise_no_handoff" (2b) | "handoff_only" (2c) | "small_loss" (Mode 2 Max)
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
            profile = str(d.get("profile") or _infer_profile(mode, compact_reads, concise_output))
            if profile not in _PROFILE_PRESETS:
                profile = _infer_profile(mode, compact_reads, concise_output)
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
            if prof not in _PROFILE_PRESETS:
                prof = "small_loss"
            merged["profile"] = prof
            merged.update(_PROFILE_PRESETS[prof])
        elif "mode" in patch:
            mode = str(patch["mode"])
            if mode not in ("smart_auto", "prepare_only", "off"):
                mode = "smart_auto"
            merged["mode"] = mode
            merged["auto_rollover"] = mode != "off"
            merged["profile"] = _infer_profile(mode, bool(merged.get("compact_reads", True)), bool(merged.get("concise_output", True)))
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
        import rules_sync
        if concise_changed and rules_sync._should_defer_concise_sync():
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
            rules_sync._sync_concise_rule(clean["concise_output"])
        return clean



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


