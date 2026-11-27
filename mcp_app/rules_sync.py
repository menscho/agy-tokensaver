"""Cache-safe deferred synchronization of opt-in concise rules to rules/AGENTS.md."""
from __future__ import annotations

import os
import time
from pathlib import Path

from config import CFG_FILE, PLUGIN_ROOT, _atomic_write_json, _cfg_lock, _is_sandbox_data_dir, load_config

_CONCISE_RULE_LINE = "1. Concise replies: skip pleasantries, preambles, and post-tool narration; keep code, diffs, paths, and errors verbatim."
_EDIT_RULE_LINE = "2. Targeted & batched edits: change existing files with targeted edits, not full-file rewrites; batch >=2 hunks/files or edit+verify into `apply_edits` (`bestEffort: true` for >=3 files)."
_BATCH_RULE_LINE = "3. Hybrid batching: use the `SessionStart` snapshot before running `ls`/`git status`; pack >=2 text/regex/glob/fuzzy/read-only git queries into `batch_search`; outline unknown >300L files with `quick_outline` before parallel ranged `read_file`; batch >=2 independent checks into `run_checks` (`outputMode: \"compact\"`)."
_CACHE_RULE_LINE = "4. Cache & multimodal hygiene: in >150k sessions delegate image inspection to a subagent returning text-only findings; avoid toggling Plan Mode mid-loop; keep verify commands fast (<=45s) and set `WaitMsBeforeAsync` (5000-10000) for <=10s commands."
_RULES_TEXT = (
    "# Token Saver\n"
    + _CONCISE_RULE_LINE + "\n"
    + _EDIT_RULE_LINE + "\n"
    + _BATCH_RULE_LINE + "\n"
    + _CACHE_RULE_LINE + "\n"
)


def _should_defer_concise_sync() -> bool:
    """Protect active warm KV caches (>20k tokens, within hot TTL window) from mid-session system-prompt edits (arXiv:2601.06007).
    Empirical cliffs: Neon retains ~90% cache hits up to 240s (protect 300s); Hydrogen drops at 60-90s (protect 150s)."""
    if _is_sandbox_data_dir() or os.environ.get("AGY_TS_SYNC_RULES") == "1":
        return False
    try:
        from transcript_io import discover_session_sources, parse_session_cached
        cands = discover_session_sources()
        if not cands:
            return False
        now = time.time()
        _mt, sid, tx, db = cands[0]
        if now - _mt > 330:
            return False
        rec = parse_session_cached(sid, tx, db)
        if not rec:
            return False
        age = max(0, int(now - rec["last_ts"]))
        ttl_cliff = int(rec.get("ttl_cliff_sec") or 240)
        protect_sec = 150 if ttl_cliff <= 90 else 300
        return rec["cur_tot"] >= 20_000 and age < protect_sec
    except Exception:
        return False


def flush_pending_concise_sync(force: bool = False) -> bool:
    """Apply deferred or drifted Concise Output rule once active warm cache cools or before rollover."""
    with _cfg_lock:
        cfg = load_config()
        desired = bool(cfg.get("concise_output", False))
        agents_md = PLUGIN_ROOT / "rules" / "AGENTS.md"
        can_touch_repo = not _is_sandbox_data_dir() or os.environ.get("AGY_TS_SYNC_RULES") == "1"
        drifted = can_touch_repo and (desired != agents_md.is_file() or (desired and _rules_text_stale(agents_md)))
        if not cfg.get("pending_concise_sync") and not drifted:
            return False
        if not force and _should_defer_concise_sync():
            return False
        _sync_concise_rule(desired)
        if cfg.get("pending_concise_sync"):
            cfg["pending_concise_sync"] = False
            _atomic_write_json(CFG_FILE, cfg)
        return True


def _rules_text_stale(agents_md: Path) -> bool:
    """True when an existing rules/AGENTS.md was written by an older version (re-synced via the same cache-safe deferral)."""
    try:
        return agents_md.read_text(encoding="utf-8") != _RULES_TEXT
    except OSError:
        return False


def _sync_concise_rule(enabled: bool) -> None:
    """Create minimal opt-in rules/AGENTS.md only when concise_output=True; remove when off so default tax is 0 tok/turn."""
    try:
        # Never mutate repo rules/AGENTS.md when running under an isolated test PLUGIN_DATA dir
        if _is_sandbox_data_dir() and os.environ.get("AGY_TS_SYNC_RULES") != "1":
            return
        rules_dir = PLUGIN_ROOT / "rules"
        agents_md = rules_dir / "AGENTS.md"
        if not enabled:
            if agents_md.is_file():
                agents_md.unlink()
            return
        rules_dir.mkdir(parents=True, exist_ok=True)
        agents_md.write_bytes(_RULES_TEXT.encode("utf-8"))
    except OSError:
        pass


