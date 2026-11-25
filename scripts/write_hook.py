#!/usr/bin/env python3
"""PreToolUse guard for write_file / write_to_file (python3 -S stdlib-only)."""

from __future__ import annotations

import os
import re
from collections import Counter

from hook_common import (
    data_dir,
    decode_hook_input,
    emit_allow,
    emit_deny,
    get_bool,
    get_str,
    hooks_disabled,
    log_event,
    read_stdin_capped,
    resolve_path,
    safe_cid,
    script_plugin_root,
    tool_input,
)

SKIP_EXT_RE = re.compile(r"\.(?:json|md|txt|yaml|yml|toml|lock|snap|csv)$", re.I)
LAZY_PLACEHOLDER_RE = re.compile(
    r"(?://|#|/\*|<!--)\s*\.\.\.\s*(?:existing|unchanged|rest|remaining|omitted|same as before|keep existing)",
    re.I,
)


def main() -> int:
    raw = read_stdin_capped()
    if raw is None:
        emit_allow()
        return 0
    if b"Overwrite" not in raw and b"\\u" not in raw:
        emit_allow()
        return 0

    plugin_root = script_plugin_root(__file__)
    doc = decode_hook_input(raw)
    if doc is None or hooks_disabled(plugin_root):
        emit_allow()
        return 0

    ti = tool_input(doc)
    if get_bool(ti, "Append") or not get_bool(ti, "Overwrite"):
        emit_allow()
        return 0

    path = get_str(ti, "Path", "TargetFile") or ""
    if not path:
        emit_allow()
        return 0
    path = resolve_path(path, doc, ti)
    if not os.path.isfile(path):
        emit_allow()
        return 0
    try:
        old_sz = os.path.getsize(path)
    except OSError:
        emit_allow()
        return 0
    if old_sz < 4096:
        emit_allow()
        return 0
    if "/.gemini/antigravity/brain/" in path.replace("\\", "/") or SKIP_EXT_RE.search(path):
        emit_allow()
        return 0

    try:
        with open(path, "rb") as fh:
            old_content = fh.read(512 * 1024).decode("utf-8", errors="replace")
    except OSError:
        emit_allow()
        return 0
    old_lines = old_content.split("\n")
    old_cnt = len(old_lines)
    if old_cnt <= 120:
        emit_allow()
        return 0

    new_content = get_str(ti, "Content", "CodeContent")
    if new_content is None:
        emit_allow()
        return 0
    new_lines = new_content.split("\n")
    new_cnt = len(new_lines)
    base = re.sub(r"^.*[/\\]", "", path)
    new_len = len(new_content.encode("utf-8", errors="replace"))

    if new_cnt <= old_cnt * 0.60 and LAZY_PLACEHOLDER_RE.search(new_content):
        log_event(plugin_root, "write_guard", "lazy_truncation", "blocked", new_len, 0, old_sz // 4, base)
        emit_deny(
            f"[token-saver] Blocked potential lazy truncation on '{base}' ({old_cnt}L -> {new_cnt}L with placeholder comment). "
            "Use edit_file for surgical changes without losing existing code."
        )
        return 0

    if old_cnt > 120 and new_cnt > 100:
        freq = Counter(old_lines)
        matched = 0
        for ln in new_lines:
            if freq[ln] > 0:
                freq[ln] -= 1
                matched += 1
        denom = max(old_cnt, new_cnt, 1)
        if matched / denom >= 0.75:
            log_event(plugin_root, "write_guard", "huge_overwrite", "observed", new_len, 0, 0, base)
            cid = get_str(doc, "conversationId") or ""
            if cid:
                try:
                    adir = os.path.join(data_dir(plugin_root), "alerts")
                    os.makedirs(adir, exist_ok=True)
                    with open(os.path.join(adir, f"{safe_cid(cid)}.huge_write"), "w", encoding="utf-8") as afh:
                        afh.write(f"{base}\t{matched}\t{old_cnt}\n")
                except OSError:
                    pass

    emit_allow()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
