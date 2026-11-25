#!/usr/bin/env python3
"""PreToolUse hook for grep_search (python3 -S stdlib-only)."""

from __future__ import annotations

import os
import re

from hook_common import (
    check_loop_guard,
    decode_hook_input,
    emit_allow,
    emit_deny,
    get_str,
    hooks_disabled,
    read_stdin_capped,
    script_plugin_root,
    tool_input,
)

WIN_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/])")
SPLIT_PARENT_RE = re.compile(r"^(.*[\\/])([^\\/]+)$")
DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:[\\/]$")
GLOB_META_RE = re.compile(r"[*?\[\]{}!]")


def main() -> int:
    plugin_root = script_plugin_root(__file__)
    doc = decode_hook_input(read_stdin_capped())
    if doc is None or hooks_disabled(plugin_root):
        emit_allow()
        return 0

    ti = tool_input(doc)
    sp0 = get_str(ti, "SearchPath") or ""
    qry = get_str(ti, "Query") or ""
    if qry:
        deny_msg = check_loop_guard(
            doc,
            plugin_root,
            f"grep\t{sp0}\t{qry}",
            "[token-saver] Repeated identical grep_search 3x with no edits since prior matches. Reuse earlier results or broaden Query (retry once to override).",
        )
        if deny_msg:
            emit_deny(deny_msg)
            return 0

    if not sp0 or not WIN_ABS_RE.match(sp0):
        emit_allow()
        return 0

    sp = re.sub(r"[\\/]+$", "", sp0)
    if not os.path.isfile(sp):
        emit_allow()
        return 0
    m = SPLIT_PARENT_RE.match(sp)
    if not m:
        emit_allow()
        return 0
    parent, base = m.group(1), m.group(2)
    if GLOB_META_RE.search(base):
        emit_allow()
        return 0
    if not DRIVE_ROOT_RE.match(parent):
        parent = re.sub(r"[\\/]$", "", parent)
    if not parent or not os.path.isdir(parent):
        emit_allow()
        return 0

    emit_allow({"SearchPath": parent, "Includes": [base]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
