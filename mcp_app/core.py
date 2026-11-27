#!/usr/bin/env python3
"""
Token Saver Core Facade — re-exports modular mcp_app components for backwards compatibility.
Pure Python 3 stdlib — zero external dependencies, zero LLM tokens.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from config import (  # noqa: E402, F401
    APP_DIRS, AUTO_CAP_MAX, AUTO_CAP_MIN, BRAIN_DIR, CFG_FILE, COLD_MIN_CTX, COLD_START_TOK,
    DATA_DIR, DELEGATED_EXTRA_TOK, DELEGATED_POST_DROP_MIN_CTX, EVENTS_FILE, FRESH_CTX_TOK,
    GEMINI_HOME, GITHUB_RELEASES_API, GITHUB_RELEASES_URL, GITHUB_REPO, HANDOFF_PACKET_TOK,
    HERE, MOMENTUM_MIN_TURNS, MOMENTUM_WINDOW_SEC, PACKET_REPLY_OUT_TOK, PLUGIN_ROOT,
    POLICY_CONTINUE_PRIOR, POLICY_DROP_PRIOR, POLICY_HORIZON_TURNS, POLICY_MARGIN,
    REDISCOVERY_TAX_TOK, ROLLOVER_HISTORY_FILE, ROLLOVER_STATE_FILE, SETTLE_GAP_SEC, W_CR,
    W_IN, W_OUT, WARMUP_TURNS, _DEFAULT_DATA_DIR, _SIM_CFG, _atomic_write_json, _cfg_lock,
    _infer_profile, _is_sandbox_data_dir, _load_rollover_state, _save_rollover_state,
    _state_lock, append_rollover_history, has_direct_handoff_cap, load_config,
    load_rollover_history, parse_iso_ts, save_config,
)
from rollover import (  # noqa: E402, F401
    RolloverTracker, _p_continue, _simulate_rollover_session, compute_adaptive_threshold,
    continuation_probability, rollover_cost_tok, rollover_policy,
)
from rules_sync import (  # noqa: E402, F401
    _CONCISE_RULE_LINE, _should_defer_concise_sync, _sync_concise_rule, flush_pending_concise_sync,
)
from snapshots import (  # noqa: E402, F401
    _find_agentapi_cmd, _last_auto_event, auto_rollover_loop, build_handoff_packet,
    build_repo_digest, build_state, perform_rollover, run_smart_rollover_tick,
)
from telemetry import (  # noqa: E402, F401
    _ev_cache, compute_savings_analytics, load_events_summary,
)
from transcript_io import (  # noqa: E402, F401
    _BG_START, _SUBAGENTS, _build_session_record, _db_gen_usage, _fsig, _has_tool_calls,
    _pb_fields, _rv, _scan_transcript, _session_paths, _settled_flags, _tx_cache,
    discover_session_sources, parse_session_cached, parse_transcript_cached,
)
from updater import (  # noqa: E402, F401
    _parse_semver, _update_cache, apply_update, check_github_updates, check_requirements,
    get_installed_version, get_local_changelog, get_update_snapshot, parse_sha256sums,
    verify_gh_attestation, verify_sha256_bytes,
)
