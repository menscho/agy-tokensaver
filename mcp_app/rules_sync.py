"""Cache-safe deferred synchronization of opt-in concise rules to rules/AGENTS.md."""
from __future__ import annotations

import os
import time

from config import CFG_FILE, PLUGIN_ROOT, _atomic_write_json, _cfg_lock, _is_sandbox_data_dir, load_config

_CONCISE_RULE_LINE = "7. Concise mode (SoT/TALE/Caveman-lite): skip pleasantries, preambles, and pre/post-tool chatter; keep code, diffs, paths, errors, and root-cause debugging verbatim."


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


