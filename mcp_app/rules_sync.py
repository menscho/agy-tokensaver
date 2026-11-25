"""Cache-safe deferred synchronization of opt-in concise rules to rules/AGENTS.md."""
from __future__ import annotations

import os
import time

from config import CFG_FILE, PLUGIN_ROOT, _atomic_write_json, _cfg_lock, _is_sandbox_data_dir, load_config

_CONCISE_RULE_LINE = "1. Concise replies: skip pleasantries, preambles, and post-tool narration; keep code, diffs, paths, and errors verbatim."


def _should_defer_concise_sync() -> bool:
    """Protect active warm KV caches (>30k tokens, idle <150s) from mid-session system-prompt edits (arXiv:2601.06007)."""
    if _is_sandbox_data_dir() or os.environ.get("AGY_TS_SYNC_RULES") == "1":
        return False
    try:
        from transcript_io import discover_session_sources, parse_session_cached
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
        agents_md.write_text("# Token Saver\n" + _CONCISE_RULE_LINE + "\n", encoding="utf-8")
    except OSError:
        pass


