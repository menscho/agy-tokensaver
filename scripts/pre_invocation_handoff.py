#!/usr/bin/env python3
"""PreInvocation automatic handoff hook (python3 -S stdlib-only, fail-open).

Fires at invocationNum == 0 (start of a new user turn, BEFORE the LLM runs).
When the conversation's KV cache has gone cold (>=75k) or context exceeded the
adaptive cap:
  - Direct mode (when ANTIGRAVITY_LS_ADDRESS is present in hook env): spawns the
    continuation conversation directly via ConversationService / agentapi, archives
    the old chat, and returns terminationBehavior="terminate" (0 LLM tokens spent).
  - Delegated mode (when Antigravity omits LS env vars from hooks): saves the
    compact handoff packet + exact user request to <PLUGIN_DATA>/pending/<cid>.json
    and injects a 1-step directive so the agent immediately runs `handoff_exec.py`
    (which has full ConversationService access), launching the fresh chat with the
    user's request already running and archiving the old chat seamlessly.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from hook_common import (
    data_dir,
    decode_hook_input,
    decode_line,
    emit_json,
    get_int,
    get_str,
    hooks_disabled,
    model_cache_profile,
    read_stdin_capped,
    safe_cid,
    script_plugin_root,
)

# Opt-in Max-mode rules; must match mcp_app/rules_sync.py _RULES_TEXT byte-for-byte (unit-tested) so the two
# writers never flip-flop rules/AGENTS.md and invalidate the prompt cache.
RULES_TEXT = (
    "# Token Saver\n"
    "1. Concise replies: skip pleasantries, preambles, and post-tool narration; keep code, diffs, paths, and errors verbatim.\n"
    "2. Targeted & batched edits: change existing files with targeted edits, not full-file rewrites; batch >=2 hunks/files or edit+verify into `apply_edits` (`bestEffort: true` for >=3 files).\n"
    "3. Hybrid batching: use the `SessionStart` snapshot before running `ls`/`git status`; pack >=2 text/regex/glob/fuzzy/read-only git queries into `batch_search`; outline unknown >300L files with `quick_outline` before parallel ranged `read_file`; batch >=2 independent checks into `run_checks` (`outputMode: \"compact\"`).\n"
    "4. Cache & multimodal hygiene: in >150k sessions delegate image inspection to a subagent returning text-only findings; avoid toggling Plan Mode mid-loop; keep verify commands fast (<=45s) and set `WaitMsBeforeAsync` (5000-10000) for <=10s commands.\n"
)

# Shared causal rollover constants (kept in sync with mcp_app/config.py & rollover.py)
COLD_START_TOK = 17_000
REDISCOVERY_TAX_TOK = 6_000
DELEGATED_EXTRA_TOK = 2_300  # Path 2 1-turn delegated handoff overhead (directive + 180 out*6; Turn 2 terminated)
WARMUP_TURNS = 3
W_IN = 1.0
W_CR = 0.05
POLICY_HORIZON_TURNS = 12
POLICY_MARGIN = 1.5
POLICY_DROP_PRIOR = (0.5, 20)
POLICY_DROP_PRIOR_SHORT_TTL = (1.2, 16)
AUTO_CAP_MIN = 75_000
AUTO_CAP_MAX = 220_000
COLD_MIN_CTX = 55_000
COLD_MIN_TURNS = 4
MODEL_SWITCH_MIN_CTX = 45_000
HEAVY_PHASE_MIN_CTX = 55_000
DELEGATED_HEAVY_MIN_CTX = 75_000
DELEGATED_POST_DROP_MIN_CTX = 90_000
DELEGATED_TRIVIAL_BYPASS_CTX = 180_000
MIDRUN_ADVISORY_MIN_CTX = 75_000
MODEL_ADVISORY_MIN_CTX = 40_000  # below this a cache miss is cheap; don't nag
HIGH_CTX_FASTTRACK_CTX = 120_000
HIGH_CTX_FASTTRACK_TURNS = 6
COOLDOWN_BYPASS_CTX = 180_000

_STAY_RE = re.compile(r"(?:^|\s)(?:\[stay\]|--stay)(?:\s|$)", re.I)
_HEAVY_PHASE_RE = re.compile(
    r"\b(?:swarm|subagents?|workflow|teamwork|parallel agents?|run[_ ]all|full benchmark|run.*evals?|stress test)\b",
    re.I,
)
_KNOWN_MODEL_RE = re.compile(r"(?:gemini-|claude-|gpt-|o[134]-)", re.I)
_TRIVIAL_REPLY_RE = re.compile(
    r"^\s*(?:yes|yeah|yep|ok|okay|sure|thanks|thank you|looks good|lgtm|go ahead|continue|proceed|nice|great|got it|cool|right|correct|are you sure|so you'?re? sure)[.!?,\s]*$",
    re.I,
)


def _silent() -> int:
    sys.stdout.buffer.write(b"{}")
    sys.stdout.buffer.flush()
    return 0


def _parse_iso_ts(ts_str: str) -> float | None:
    if not ts_str:
        return None
    try:
        return datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _load_hud_config(dd: str) -> dict[str, Any]:
    default: dict[str, Any] = {
        "auto_rollover": True,
        "mode": "smart_auto",
        "threshold_mode": "auto",
        "threshold_k": 110,
        "model": "inherit",
        "min_turns": 10,
        "cooldown_min": 8,
        "auto_archive": True,
        "concise_output": False,
        "pending_concise_sync": False,
    }
    cfg_path = os.path.join(dd, "hud_config.json")
    if not os.path.isfile(cfg_path):
        return default
    try:
        with open(cfg_path, "r", encoding="utf-8", errors="replace") as fh:
            d = json.loads(fh.read(16384))
        if isinstance(d, dict):
            out = dict(default)
            out.update(d)
            return out
    except Exception:
        pass
    return default


def _load_json_dict(path: str) -> dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            d = json.loads(fh.read())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _atomic_write_json(path: str, obj: Any, private: bool = False) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, indent=2, ensure_ascii=False))
    if private and os.name != "nt":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


def _has_tool_calls(tc: Any) -> bool:
    if not tc:
        return False
    if isinstance(tc, str):
        s = tc.strip()
        return bool(s) and s not in ("[]", "None", "null")
    return bool(tc)


def _scan_transcript_state(tx_path: str) -> dict[str, Any] | None:
    """Single-pass scan of PLANNER_RESPONSE turns for causal rollover features."""
    turns = 0
    ctx = 0
    peak = 0
    drops = 0
    warm_miss: list[float] = []
    last_ts: float | None = None
    last_model = ""
    last_final = True
    user_inputs = 0

    try:
        with open(tx_path, "rb") as fh:
            for raw_line in fh:
                if b'"USER_INPUT"' in raw_line:
                    rec = decode_line(raw_line)
                    if rec and get_str(rec, "type") == "USER_INPUT":
                        user_inputs += 1
                    continue
                if b'"PLANNER_RESPONSE"' not in raw_line:
                    continue
                rec = decode_line(raw_line)
                if not rec or get_str(rec, "type") != "PLANNER_RESPONSE":
                    continue
                last_final = not _has_tool_calls(rec.get("tool_calls"))
                mdl = get_str(rec, "model") or ""
                if mdl:
                    last_model = mdl
                ts = _parse_iso_ts(get_str(rec, "created_at") or "")
                if ts is not None:
                    last_ts = ts
                inp = get_int(rec, "input_tokens") or 0
                cr = get_int(rec, "cache_read_tokens") or 0
                out = get_int(rec, "output_tokens") or 0
                if inp == 0 and cr == 0 and out == 0:
                    continue
                prev = ctx
                cur = inp + cr
                if turns > 0:
                    is_compaction = prev >= 20_000 and cur < prev * 0.6
                    if not is_compaction and prev >= 4_000 and cr < prev * 0.7:
                        if turns > WARMUP_TURNS:
                            drops += 1
                    if 1 <= turns <= WARMUP_TURNS and prev > 0:
                        warm_miss.append(min(1.0, max(0.0, (prev - cr) / prev)))
                turns += 1
                ctx = cur
                if cur > peak:
                    peak = cur
    except OSError:
        return None

    if turns == 0:
        return None

    return {
        "turns": turns,
        "ctx": ctx,
        "peak": peak,
        "drops": drops,
        "warm_turns": max(0, turns - 1 - WARMUP_TURNS),
        "warm_miss": warm_miss[:WARMUP_TURNS],
        "last_ts": last_ts,
        "last_model": last_model,
        "last_final": last_final,
        "user_inputs": user_inputs,
    }


def _compute_policy(
    state: dict[str, Any], cfg: dict[str, Any], ttl_cliff: int = 150, delegated: bool = False,
    ctx_cap: int | None = None,
) -> dict[str, Any]:
    base = COLD_START_TOK + REDISCOVERY_TAX_TOK + (DELEGATED_EXTRA_TOK if delegated else 0)
    wm = state.get("warm_miss") or []
    cost = int(base + sum(min(1.0, max(0.0, float(m))) for m in wm[:WARMUP_TURNS]) * base)
    min_turns = max(6, min(40, int(str(cfg.get("min_turns", 10)))))
    tmode = str(cfg.get("threshold_mode", "auto"))
    drops = int(state.get("drops", 0))
    warm_turns = int(state.get("warm_turns", 0))
    a, b = POLICY_DROP_PRIOR_SHORT_TTL if ttl_cliff <= 120 else POLICY_DROP_PRIOR
    p_drop = (drops + a) / (warm_turns + b)
    w = W_CR + min(0.5, p_drop) * (W_IN - W_CR)

    if tmode != "auto" and tmode.isdigit():
        cap = int(tmode) * 1000
    else:
        # User literally just submitted a prompt -> p_continue = 1.0
        h, m = float(POLICY_HORIZON_TURNS), POLICY_MARGIN
        cap_raw = (m * cost + h * w * cost) / (h * w + m * W_CR)
        cap = int(min(AUTO_CAP_MAX, max(AUTO_CAP_MIN, cap_raw)) // 1000 * 1000)
        if ctx_cap and 0 < ctx_cap < cap:
            cap = int(ctx_cap)  # Gemini 4: cache misses climb with context size

    ctx = int(state.get("ctx", 0))
    turns = int(state.get("turns", 0))
    eff_min_turns = min(min_turns, HIGH_CTX_FASTTRACK_TURNS) if ctx >= HIGH_CTX_FASTTRACK_CTX else min_turns
    warm_roll = ctx >= cap and turns >= eff_min_turns
    post_drop_floor = DELEGATED_POST_DROP_MIN_CTX if delegated else AUTO_CAP_MIN
    post_drop_roll = drops >= 1 and ctx >= post_drop_floor and turns >= 6
    return {"cap": cap, "cap_k": cap // 1000, "cost_tok": cost, "warm_roll": warm_roll, "post_drop_roll": post_drop_roll}


def _maybe_terminate_post_handoff(dd: str, cid: str) -> int | None:
    """Eliminate Turn 2 in Path 2 (delegated) handoffs: right after `handoff_exec.py` completes inside
    `run_command`, PreInvocation (invocationNum > 0) terminates immediately with the clickable link
    via user_markdown instead of spending a 2nd full-context LLM turn just to echo the link."""
    if not cid:
        return None
    ev = _load_json_dict(os.path.join(dd, "last_auto_event.json"))
    if not ev.get("ok"):
        return None
    from_full = str(ev.get("from_conv_full") or "")
    new_id = str(ev.get("to_conv_full") or "")
    if not new_id or (from_full != cid and str(ev.get("from_conv") or "") != cid[:8]):
        return None
    now_ts = int(time.time())
    ev_ts = int(ev.get("ts") or 0)
    if ev_ts <= 0 or (now_ts - ev_ts) > 120:
        return None
    done_dir = os.path.join(dd, "alerts")
    done_file = os.path.join(done_dir, f"{safe_cid(cid)}.handoff_done")
    try:
        if os.path.isfile(done_file):
            with open(done_file, "r", encoding="utf-8", errors="replace") as fh:
                if fh.read().strip() == new_id:
                    return None
        os.makedirs(done_dir, exist_ok=True)
        with open(done_file, "w", encoding="utf-8") as fh:
            fh.write(new_id + "\n")
    except OSError:
        pass
    arch_note = " Old chat archived." if ev.get("archived_old", True) else ""
    md = (
        f"**Token Saver Auto-Handoff** -> [**Open continued chat**](conversation://{new_id})\n\n"
        f"Carried your message and compact context into a fresh chat — already running.{arch_note}"
    )
    emit_json(
        {
            "injectSteps": [
                {
                    "systemMessage": {
                        "systemMessage": "",
                        "metadata": {"user_markdown": md},
                    }
                }
            ],
            "terminationBehavior": "terminate",
        }
    )
    return 0


def _maybe_midrun_drop_advisory(dd: str, cid: str, tx: str, inv_num: int = 1) -> int:
    """Mid-run (invocationNum > 0) zero-bloat cache-drop shield.
    Fires AT MOST once per 10-15 minutes in two high-leverage situations:
      1. Immediately after a real mid-run KV cache drop on a >=75k chat.
      2. Proactively once when a long autonomous run (inv_num >= 6) crosses the model's
         high-risk TTL cliff zone (110k on Hydrogen / 180k on Neon) so the agent batches
         calls and avoids >TTL synchronous waits (appended at tail -> 0 prefix invalidation)."""
    if not cid or not tx or not os.path.isfile(tx):
        return _silent()
    adv_dir = os.path.join(dd, "alerts")
    adv_file = os.path.join(adv_dir, f"{safe_cid(cid)}.midrun_adv")
    now_ts = time.time()
    try:
        if os.path.isfile(adv_file) and (now_ts - os.path.getmtime(adv_file)) < 600:
            return _silent()
    except OSError:
        pass
    try:
        sz = os.path.getsize(tx)
        if sz == 0:
            return _silent()
        with open(tx, "rb") as fh:
            if sz > 32768:
                fh.seek(sz - 32768)
            tail = fh.read(32768)
    except OSError:
        return _silent()

    last_two: list[tuple[int, int, str]] = []
    for raw_line in tail.splitlines():
        if b'"PLANNER_RESPONSE"' not in raw_line:
            continue
        rec = decode_line(raw_line)
        if not rec or get_str(rec, "type") != "PLANNER_RESPONSE":
            continue
        inp = get_int(rec, "input_tokens") or 0
        cr = get_int(rec, "cache_read_tokens") or 0
        out = get_int(rec, "output_tokens") or 0
        if inp == 0 and cr == 0 and out == 0:
            continue
        last_two.append((inp, cr, get_str(rec, "model") or ""))
        if len(last_two) > 2:
            del last_two[0]

    if len(last_two) < 2:
        return _silent()
    (p_in, p_cr, _p_m), (c_in, c_cr, c_m) = last_two
    prev_tot = p_in + p_cr
    cur_tot = c_in + c_cr
    is_compaction = cur_tot < prev_tot * 0.80
    prof = model_cache_profile(c_m)
    ttl = int(prof["ttl_sec"])
    is_real_drop = prev_tot >= MIDRUN_ADVISORY_MIN_CTX and c_in >= 40_000 and c_cr < prev_tot * 0.5 and not is_compaction

    proactive_floor = int(prof["proactive_floor"])
    is_proactive = (
        not is_real_drop
        and not is_compaction
        and inv_num >= 6
        and cur_tot >= proactive_floor
        and not os.path.isfile(adv_file)
    )
    if not (is_real_drop or is_proactive):
        return _silent()

    try:
        os.makedirs(adv_dir, exist_ok=True)
        with open(adv_file, "w", encoding="utf-8") as fh:
            fh.write(f"{int(now_ts)}\t{cur_tot}\n")
    except OSError:
        pass

    advice = f"Batch independent tool calls in parallel, avoid >{ttl}s blocking waits, and finish this phase cleanly (Auto-Handoff rotates on the next user turn)."
    if is_real_drop:
        msg = f"[token-saver] Mid-run KV cache drop (~{c_in // 1000}k rebuilt; {ttl}s TTL). {advice}"
    else:
        msg = f"[token-saver] High-context cache shield (~{cur_tot // 1000}k ctx; {ttl}s TTL). {advice}"
    emit_json({"injectSteps": [{"systemMessage": {"systemMessage": msg}}]})
    return 0


_MODEL_ADVICE = {
    "orchestrator": "Orchestrator mode often misses the prompt cache between planner and worker turns. "
                    "For long chats, picking Gemini 4 Neon directly has been reported to cut uncached input about 3x.",
    "carbon": "Gemini 4 Carbon often misses the prompt cache, even on turns seconds apart. "
              "For long chats, Gemini 4 Neon usually keeps the cache (~96-98% hits reported); High instead of XHigh also helps.",
    "argon": "Gemini 4 Argon has shown 0% cache hits on long contexts in user reports. "
             "For long chats, Gemini 4 Neon usually keeps the cache (~96-98% hits reported).",
}


def _maybe_model_advisory(dd: str, cid: str, prof: dict[str, Any], model: str, cur_ctx: int) -> int:
    """Once per conversation, tell the USER (not the model: empty systemMessage = 0 prompt tokens)
    that the selected model is known to drop its prompt cache, and which model keeps it."""
    family = str(prof.get("family") or "")
    advice = _MODEL_ADVICE.get(family)
    if not advice or not cid or cur_ctx < MODEL_ADVISORY_MIN_CTX:
        return _silent()
    adv_dir = os.path.join(dd, "alerts")
    marker = os.path.join(adv_dir, f"{safe_cid(cid)}.model_adv")
    if os.path.isfile(marker):
        return _silent()
    try:
        os.makedirs(adv_dir, exist_ok=True)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(f"{int(time.time())}\t{family}\t{cur_ctx}\n")
    except OSError:
        return _silent()
    md = f"**agy-tokensaver:** this chat is at ~{cur_ctx // 1000}k tokens on `{model or family}`. {advice}"
    emit_json({"injectSteps": [{"systemMessage": {"systemMessage": "", "metadata": {"user_markdown": md}}}]})
    return 0


def _has_fresh_uploads(artifact_dir: str, now_ts: float) -> bool:
    """Avoid intercepting turns where the user just attached images/files to this prompt."""
    if not artifact_dir:
        return False
    up_dir = os.path.join(artifact_dir, ".user_uploaded")
    if not os.path.isdir(up_dir):
        return False
    try:
        for entry in os.scandir(up_dir):
            try:
                if now_ts - entry.stat().st_mtime <= 45.0:
                    return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def _build_handoff(ws: str, cid: str, user_prompt: str) -> tuple[str, int]:
    import handoff_pack

    ws_path = Path(ws) if (ws and os.path.isdir(ws)) else Path.home()
    packet = handoff_pack.build_packet(ws_path, cid, exclude_prompt=user_prompt).rstrip()
    full_prompt = f"{packet}\n\n## Next User Request\n{user_prompt.strip()}\n"
    return full_prompt, len(packet) // 4


def _flush_concise_rule_if_needed(plugin_root: str, dd: str, cfg: dict[str, Any]) -> None:
    """Reconcile rules/AGENTS.md right before spawning a fresh chat."""
    if os.environ.get("PLUGIN_DATA") and os.environ.get("AGY_TS_SYNC_RULES") != "1":
        default_dd = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity", "plugin_data", "token-saver")
        if os.path.abspath(dd) != os.path.abspath(default_dd):
            return
    try:
        rules_dir = os.path.join(plugin_root, "rules")
        agents_md = os.path.join(rules_dir, "AGENTS.md")
        enabled = bool(cfg.get("concise_output", False))
        if enabled:
            cur = ""
            if os.path.isfile(agents_md):
                with open(agents_md, "rb") as rfh:
                    cur = rfh.read().decode("utf-8", errors="replace")
            if cur != RULES_TEXT:  # missing or written by an older version; a fresh chat has no warm cache to break
                os.makedirs(rules_dir, exist_ok=True)
                with open(agents_md, "wb") as fh:
                    fh.write(RULES_TEXT.encode("utf-8"))
        elif os.path.isfile(agents_md):
            os.unlink(agents_md)
        if cfg.get("pending_concise_sync"):
            cfg["pending_concise_sync"] = False
            _atomic_write_json(os.path.join(dd, "hud_config.json"), cfg)
    except OSError:
        pass


def _resolve_workspace(doc: dict[str, Any]) -> str:
    wps = doc.get("workspacePaths")
    if isinstance(wps, list):
        for w in wps:
            if isinstance(w, str) and w and os.path.isdir(w):
                return w
    cwd = get_str(doc, "cwd") or ""
    if cwd and os.path.isdir(cwd):
        return cwd
    return os.path.expanduser("~")


def _record_hook_caps(dd: str, has_ls: bool, has_csrf: bool, has_agentapi: bool, inv_num: int | None) -> None:
    """Record non-sensitive capability booleans for dashboard/doctor diagnostics (throttled)."""
    caps_path = os.path.join(dd, "hook_caps.json")
    now_i = int(time.time())
    try:
        if os.path.isfile(caps_path) and (now_i - int(os.path.getmtime(caps_path))) < 60:
            return
        _atomic_write_json(
            caps_path,
            {
                "has_ls_addr": has_ls,
                "has_csrf": has_csrf,
                "has_agentapi_exe": has_agentapi,
                "last_inv_num": inv_num,
                "ts": now_i,
            },
        )
    except Exception:
        pass


def main() -> int:
    plugin_root = script_plugin_root(__file__)
    doc = decode_hook_input(read_stdin_capped())
    if doc is None or hooks_disabled(plugin_root):
        return _silent()

    dd = data_dir(plugin_root)
    cfg = _load_hud_config(dd)
    if str(cfg.get("mode", "smart_auto")) != "smart_auto" or not cfg.get("auto_rollover", True):
        return _silent()

    # Mid-run (invocationNum > 0): terminate immediately if Path 2 handoff just succeeded (0-token Turn 2!),
    # otherwise never hand off mid-loop, only emit a 1-line advisory right after a big cache drop.
    inv_num = get_int(doc, "invocationNum")
    cid = get_str(doc, "conversationId") or ""
    tx = get_str(doc, "transcriptPath") or ""
    if inv_num is not None and inv_num > 0:
        term_rc = _maybe_terminate_post_handoff(dd, cid)
        if term_rc is not None:
            return term_rc
        return _maybe_midrun_drop_advisory(dd, cid, tx, inv_num=inv_num)

    user_input = (get_str(doc, "lastUserInput") or "").strip()
    if not cid or not tx or not user_input or not os.path.isfile(tx):
        return _silent()

    # Skip slash commands, existing handoffs, or explicit [stay] opt-out
    if (
        user_input.startswith("/")
        or user_input.startswith("# Session Continuation Handoff")
        or _STAY_RE.search(user_input)
    ):
        return _silent()

    ls_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
    csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "").strip()
    _record_hook_caps(dd, bool(ls_addr), bool(csrf), bool(os.environ.get("ANTIGRAVITY_AGENTAPI_EXE")), inv_num)

    now_ts = time.time()
    artifact_dir = get_str(doc, "artifactDirectoryPath") or ""
    if _has_fresh_uploads(artifact_dir, now_ts):
        return _silent()

    state = _scan_transcript_state(tx)
    if state is None or not state["last_final"] or state["user_inputs"] == 0:
        return _silent()

    # Never interrupt a chat already rolled over (or attempted within the last 5 minutes)
    rstate_path = os.path.join(dd, "rollover_state.json")
    rstate = _load_json_dict(rstate_path)
    rolled_convs = dict(rstate.get("rolled_convs") or {})
    if cid in rolled_convs:
        return _silent()
    pending_dir = os.path.join(dd, "pending")
    pending_file = os.path.join(pending_dir, f"{safe_cid(cid)}.json")
    if os.path.isfile(pending_file):
        try:
            if now_ts - os.path.getmtime(pending_file) < 300:
                return _silent()
        except OSError:
            pass

    cur_ctx = int(state["ctx"])
    turns = int(state["turns"])
    last_model = str(state.get("last_model") or "")
    hook_model = (get_str(doc, "modelName") or "").strip()
    last_ts = state.get("last_ts")
    idle_sec = max(0, int(now_ts - last_ts)) if isinstance(last_ts, (int, float)) else 0
    cache_prof = model_cache_profile(hook_model or last_model)
    # Empirical cold windows: Neon keeps ~90% warm hits through 150-240s; Hydrogen drops start ~60-90s;
    # Carbon/Argon/Orchestrator lose the cache far sooner (docs/perf.md section 8).
    ttl_cliff = int(cache_prof["ttl_sec"])
    cold_ttl = ttl_cliff
    delegated = not bool(ls_addr)

    # In delegated mode (Path 2, no LS env), avoid interrupting trivial 1-turn confirmations/questions
    # below 180k — paying a delegated handoff + fresh cold start for "yes" / "are you sure?" loses tokens.
    if (
        delegated
        and cur_ctx < DELEGATED_TRIVIAL_BYPASS_CTX
        and len(user_input) <= 45
        and _TRIVIAL_REPLY_RE.match(user_input)
    ):
        return _silent()

    # Triggers 1 & 2 (Cold-Cache / Model-Switch Instant Payback):
    # ONLY deliver instant Turn-0 payback in Direct mode (Path 1, ls_addr present -> 0-token terminate).
    # In Delegated mode (Path 2), Turn 0 still runs in the old chat and pays the cold rebuild either way,
    # so low-threshold cold/switch triggers hurt ROI; Path 2 relies on heavy_phase, post_drop (>=90k), and warm_cap.
    is_cold_trigger = bool(
        not delegated
        and last_ts is not None
        and idle_sec >= cold_ttl
        and cur_ctx >= COLD_MIN_CTX
        and turns >= COLD_MIN_TURNS
    )

    is_model_switch_trigger = bool(
        not delegated
        and hook_model
        and last_model
        and hook_model.lower() != last_model.lower()
        and _KNOWN_MODEL_RE.search(hook_model)
        and _KNOWN_MODEL_RE.search(last_model)
        and cur_ctx >= MODEL_SWITCH_MIN_CTX
        and turns >= 3
    )

    # Trigger 3: Pre-Heavy-Phase / Pre-Swarm Shield (rotate BEFORE starting multi-minute swarms/workflows)
    heavy_floor = DELEGATED_HEAVY_MIN_CTX if delegated else HEAVY_PHASE_MIN_CTX
    is_heavy_phase_trigger = bool(
        cur_ctx >= heavy_floor
        and turns >= COLD_MIN_TURNS
        and _HEAVY_PHASE_RE.search(user_input)
    )

    pol = _compute_policy(state, cfg, ttl_cliff=ttl_cliff, delegated=delegated, ctx_cap=cache_prof["ctx_cap"])
    is_post_drop_trigger = bool(pol["post_drop_roll"])
    is_warm_trigger = bool(pol["warm_roll"])

    if not (
        is_cold_trigger
        or is_model_switch_trigger
        or is_heavy_phase_trigger
        or is_post_drop_trigger
        or is_warm_trigger
    ):
        return _maybe_model_advisory(dd, cid, cache_prof, hook_model or last_model, cur_ctx)

    # Instant-payback triggers (cold cache / model switch) and emergency/post-drop rotations
    # only enforce a 60s anti-burst guard; normal warm triggers respect cooldown_min.
    is_urgent = is_cold_trigger or is_model_switch_trigger or is_post_drop_trigger or cur_ctx >= COOLDOWN_BYPASS_CTX
    cooldown_sec = max(60, int(cfg.get("cooldown_min", 8)) * 60)
    last_ro_ts = int(rstate.get("last_rollover_ts") or 0)
    if not is_urgent and int(now_ts) - last_ro_ts < cooldown_sec:
        return _silent()
    if is_urgent and int(now_ts) - last_ro_ts < 60:
        return _silent()

    ws = _resolve_workspace(doc)
    _flush_concise_rule_if_needed(plugin_root, dd, cfg)

    try:
        full_prompt, pkt_tokens = _build_handoff(ws, cid, user_input)
    except Exception:
        return _silent()

    if not full_prompt:
        return _silent()

    cfg_model = str(cfg.get("model") or "inherit").strip()
    target_model = cfg_model if cfg_model and cfg_model != "inherit" else (
        hook_model if (hook_model and hook_model != "unknown") else (last_model if last_model and last_model != "unknown" else "")
    )
    if is_cold_trigger:
        trigger = "cold_cache"
    elif is_model_switch_trigger:
        trigger = "model_switch"
    elif is_heavy_phase_trigger:
        trigger = "heavy_phase"
    elif is_post_drop_trigger and not is_warm_trigger:
        trigger = "post_drop"
    else:
        trigger = "adaptive_cap"
    auto_archive = bool(cfg.get("auto_archive", True))

    cur_k = cur_ctx / 1000.0
    est_save_k = max(0, cur_ctx - COLD_START_TOK) / 1000.0
    if is_cold_trigger:
        why = f"cold cache ({idle_sec}s idle > {ttl_cliff}s TTL; ~{cur_k:.1f}k context -> ~17k fresh, est. ~{est_save_k:.1f}k saved)"
    elif is_model_switch_trigger:
        why = f"model switch ({last_model} -> {hook_model}; avoids 100% cache-miss rebuild of ~{cur_k:.1f}k)"
    elif is_heavy_phase_trigger:
        why = f"heavy phase/swarm prep (~{cur_k:.1f}k context -> ~17k fresh before multi-minute waits)"
    elif is_post_drop_trigger and not is_warm_trigger:
        why = f"post-drop escalation ({state.get('drops', 0)} drop(s) at ~{cur_k:.1f}k; resetting before next TTL cliff)"
    else:
        why = f"context reached ~{cur_k:.1f}k >= {pol['cap_k']}k cap"

    # Path 1: Direct 0-token ConversationService handoff (when LS env is provided to hooks)
    if ls_addr:
        try:
            import handoff_exec

            res = handoff_exec.execute_handoff(
                cid=cid,
                ws=ws,
                full_prompt=full_prompt,
                pkt_tokens=pkt_tokens,
                user_input=user_input,
                cur_ctx=cur_ctx,
                turns=turns,
                cap_k=pol["cap_k"],
                trigger=trigger,
                target_model=target_model,
                auto_archive=auto_archive,
                kind="auto",
                dd=dd,
            )
            if res.get("ok") and res.get("new_id"):
                new_id = str(res["new_id"])
                arch_note = " Old chat archived." if res.get("archived_old") else ""
                md = (
                    f"**Token Saver Auto-Handoff** -> [**Open continued chat**](conversation://{new_id})\n\n"
                    f"Carried your message and compact context (~{pkt_tokens} tok) straight into a fresh chat — "
                    f"already running ({why}).{arch_note} *(Add `[stay]` to a message to skip auto-handoff.)*"
                )
                emit_json(
                    {
                        "injectSteps": [
                            {
                                "systemMessage": {
                                    "systemMessage": "",
                                    "metadata": {"user_markdown": md},
                                }
                            }
                        ],
                        "terminationBehavior": "terminate",
                    }
                )
                return 0
        except Exception:
            pass
        return _silent()

    # Path 2: Seamless 1-step delegated handoff via run_command (which has full ConversationService access)
    try:
        _atomic_write_json(
            pending_file,
            {
                "cid": cid,
                "ws": ws,
                "full_prompt": full_prompt,
                "pkt_tokens": pkt_tokens,
                "user_input": user_input,
                "cur_ctx": cur_ctx,
                "turns": turns,
                "cap_k": pol["cap_k"],
                "trigger": trigger,
                "target_model": target_model,
                "auto_archive": auto_archive,
                "delegated": True,
                "ts": int(now_ts),
            },
            private=True,
        )
    except OSError:
        return _silent()

    exec_script = os.path.join(plugin_root, "scripts", "handoff_exec.py")
    py_exe = sys.executable or "python3"
    cmd_str = f'"{py_exe}" "{exec_script}" --pending "{pending_file}"'
    directive = (
        f"[token-saver] AUTO-HANDOFF TRIGGERED ({why}). Do NOT do the user's request in this chat. "
        f"Call `run_command` once with CommandLine: `{cmd_str}` (WaitMsBeforeAsync: 10000). "
        f"It opens a fresh chat with a compact handoff and the user's exact request, and archives this one. "
        f"On `AUTO_HANDOFF_OK:`, reply with ONLY that markdown link. If it fails, do the request normally."
    )
    emit_json(
        {
            "injectSteps": [
                {
                    "systemMessage": {
                        "systemMessage": directive,
                    }
                }
            ]
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
