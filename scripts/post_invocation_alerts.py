#!/usr/bin/env python3
"""PostInvocation hook: zero-LLM-token UI alerts on context/cache drops & huge overwrites (python3 -S stdlib-only)."""

from __future__ import annotations

import os
import re
import sys
from typing import NamedTuple

from hook_common import (
    data_dir,
    decode_hook_input,
    decode_line,
    emit_json,
    get_int,
    get_str,
    hooks_disabled,
    read_stdin_capped,
    safe_cid,
    script_plugin_root,
)

ISO_SEC_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})")


class _Turn(NamedTuple):
    in_tok: int
    cr_tok: int
    tot: int
    model: str
    ts: str
    cp: bool
    has_tools: bool


def _iso_sec(t: str) -> int | None:
    m = ISO_SEC_RE.match(t)
    if not m:
        return None
    d, h, mn, s = int(m.group(3)), int(m.group(4)), int(m.group(5)), int(m.group(6))
    return d * 86400 + h * 3600 + mn * 60 + s


def _has_direct_handoff(dd: str) -> bool:
    caps_file = os.path.join(dd, "hook_caps.json")
    if not os.path.isfile(caps_file):
        return False
    try:
        with open(caps_file, "r", encoding="utf-8", errors="replace") as fh:
            d = decode_line(fh.read(2048).encode("utf-8", errors="replace"))
        return bool(d and d.get("has_ls_addr"))
    except Exception:
        return False


def _silent() -> int:
    sys.stdout.buffer.write(b"{}")
    sys.stdout.buffer.flush()
    return 0


def main() -> int:
    plugin_root = script_plugin_root(__file__)
    doc = decode_hook_input(read_stdin_capped())
    if doc is None or hooks_disabled(plugin_root):
        return _silent()

    inv_num = get_int(doc, "invocationNum") or 0
    if inv_num < 4:
        return _silent()

    tx = get_str(doc, "transcriptPath") or ""
    cid = get_str(doc, "conversationId") or ""
    if not tx or not cid or not os.path.isfile(tx):
        return _silent()

    try:
        sz = os.path.getsize(tx)
        if sz == 0:
            return _silent()
        with open(tx, "rb") as fh:
            if sz > 49152:
                fh.seek(sz - 49152)
            tail = fh.read(49152).decode("utf-8", errors="replace")
    except OSError:
        return _silent()

    turns: list[_Turn] = []
    saw_checkpoint = False
    for line in tail.split("\n"):
        if '"CHECKPOINT"' in line:
            rec = decode_line(line)
            if rec and get_str(rec, "type") == "CHECKPOINT":
                saw_checkpoint = True
            continue
        if '"PLANNER_RESPONSE"' not in line:
            continue
        rec = decode_line(line)
        if not rec or get_str(rec, "type") != "PLANNER_RESPONSE":
            continue
        in_t = get_int(rec, "input_tokens") or 0
        cr_t = get_int(rec, "cache_read_tokens") or 0
        out_t = get_int(rec, "output_tokens") or 0
        if in_t == 0 and cr_t == 0 and out_t == 0:
            continue
        tc = rec.get("tool_calls")
        has_tc = bool(tc and str(tc).strip() not in ("[]", "None", "null", ""))
        turns.append(
            _Turn(
                in_tok=in_t,
                cr_tok=cr_t,
                tot=in_t + cr_t,
                model=get_str(rec, "model") or "",
                ts=get_str(rec, "created_at") or "",
                cp=saw_checkpoint,
                has_tools=has_tc,
            )
        )
        saw_checkpoint = False

    if not turns:
        return _silent()

    cur = turns[-1]
    prev = turns[-2] if len(turns) >= 2 else None
    scid = safe_cid(cid)
    state_dir = os.path.join(data_dir(plugin_root), "alerts")
    state_file = os.path.join(state_dir, f"{scid}.state")

    last_tier, last_drop_ts = 0, ""
    if os.path.isfile(state_file):
        try:
            with open(state_file, "r", encoding="utf-8", errors="replace") as sfh:
                st_line = sfh.readline().rstrip("\r\n")
            m_st = re.match(r"^(\d+)\t(\d+)\t(.*)$", st_line)
            if m_st:
                last_tier, last_drop_ts = int(m_st.group(1)), m_st.group(3)
        except Exception:
            pass

    if cur.cp:
        try:
            os.makedirs(state_dir, exist_ok=True)
            ep_file = os.path.join(state_dir, f"{scid}.epoch")
            ep = 0
            if os.path.isfile(ep_file):
                with open(ep_file, "r", encoding="utf-8", errors="replace") as efh:
                    ep = int((efh.readline() or "0").strip() or "0")
            with open(ep_file, "w", encoding="utf-8") as wfh:
                wfh.write(f"{ep + 1}\n")
        except Exception:
            pass

    notes: list[str] = []
    tot = cur.tot
    new_tier = last_tier
    if tot < 60000:
        new_tier = 0
    elif not cur.cp:
        cur_tier = 220 if tot >= 220000 else (150 if tot >= 150000 else (110 if tot >= 110000 else (75 if tot >= 75000 else 0)))
        if cur_tier > last_tier:
            new_tier = cur_tier
            k_tot = tot // 1000
            k_cr_cost = f"{(tot * 0.05) / 1000:.1f}"
            ttl_hint = " (`hydrogen` ~60-90s TTL cliff — avoid >60s swarms/waits here!)" if "hydrogen" in cur.model.lower() else ""
            direct_ok = _has_direct_handoff(data_dir(plugin_root))
            if cur_tier >= 110:
                extra = (
                    " Auto-Handoff will rotate to a fresh ~17k chat on your next message (avoid launching new swarms/long builds before rotating)."
                    if direct_ok
                    else f" Auto-Handoff will rotate on your next warm turn; if you pause >2-4m, use **Archive & New Chat** in agy-tokensaver first to avoid a ~{k_tot}k cold rebuild."
                )
            else:
                extra = (
                    f" A >2-4m idle or slow tool wait rebuilds ~{k_tot}k uncached — Auto-Handoff is armed for heavy phases or >110k."
                )
            notes.append(f"**Token Saver:** Context reached **~{k_tot}k tokens** (~{k_cr_cost}k uncached-equiv/turn in cache reads){ttl_hint}.{extra}")

    new_drop_ts = last_drop_ts
    if prev is not None:
        if prev.cr_tok >= 8000 and cur.cr_tok < prev.cr_tok * 0.75 and cur.ts != last_drop_ts:
            is_compaction = cur.cp or tot < prev.tot * 0.80
            if not is_compaction:
                new_drop_ts = cur.ts
                ttl_s = 90 if "hydrogen" in cur.model.lower() else 150
                cause = "prefix change or backend cache eviction"
                if prev.model and cur.model and prev.model != cur.model:
                    cause = f"model switch (`{prev.model}` -> `{cur.model}`)"
                else:
                    t1, t2 = _iso_sec(prev.ts), _iso_sec(cur.ts)
                    if t1 is not None and t2 is not None:
                        dt = t2 - t1
                        if dt >= ttl_s:
                            cause = (
                                f"mid-run tool/swarm wait (~{dt}s > {ttl_s}s TTL)"
                                if prev.has_tools
                                else f"idle TTL eviction (~{dt}s pause exceeded {ttl_s}s hot cache window)"
                            )
                        elif prev.tot >= 180_000:
                            cause = f"large-context backend eviction (~{prev.tot // 1000}k ctx)"
                notes.append(
                    f"**Token Saver:** Prompt cache dropped (**{prev.cr_tok // 1000}k -> {cur.cr_tok // 1000}k**, "
                    f"rebuilt **{cur.in_tok // 1000}k** uncached) — likely {cause}."
                )

    hw_file = os.path.join(state_dir, f"{scid}.huge_write")
    if os.path.isfile(hw_file):
        try:
            with open(hw_file, "r", encoding="utf-8", errors="replace") as hwfh:
                hw = hwfh.readline()
            os.unlink(hw_file)
            m_hw = re.match(r"^([^\t]+)\t(\d+)\t(\d+)", hw or "")
            if m_hw:
                notes.append(
                    f"**Token Saver:** Full-file `write_file` on `{m_hw.group(1)}` re-emitted "
                    f"**{m_hw.group(2)}/{m_hw.group(3)} unchanged lines** (6x output cost) — prefer `edit_file` for surgical hunks."
                )
        except OSError:
            pass

    if new_tier != last_tier or new_drop_ts != last_drop_ts:
        try:
            os.makedirs(state_dir, exist_ok=True)
            with open(state_file, "w", encoding="utf-8") as sfh:
                sfh.write(f"{new_tier}\t{tot}\t{new_drop_ts}\n")
        except OSError:
            pass

    if not notes:
        return _silent()

    md = "\n\n".join(notes)
    emit_json({"injectSteps": [{"systemMessage": {"systemMessage": "", "metadata": {"user_markdown": md}}}]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
