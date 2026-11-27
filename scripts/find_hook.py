#!/usr/bin/env python3
"""PreToolUse hook for IDE/Cortex find_by_name / find (python3 -S stdlib-only)."""

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

EXCLUDES = (
    "node_modules",
    ".git",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".turbo",
    ".parcel-cache",
    "target",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".venv",
    "venv",
    "vendor",
    "bower_components",
    "Pods",
    "DerivedData",
    ".gradle",
    ".terraform",
    ".cache",
    "coverage",
    "out",
)
HOME_RE = re.compile(r"^~([/\\].*)?$")


def main() -> int:
    plugin_root = script_plugin_root(__file__)
    doc = decode_hook_input(read_stdin_capped())
    if doc is None or hooks_disabled(plugin_root):
        emit_allow()
        return 0

    ti = tool_input(doc)
    excl_in = ti.get("Excludes")
    if isinstance(excl_in, list) and len(excl_in) > 0:
        emit_allow()
        return 0

    sdir = get_str(ti, "SearchDirectory") or ""
    pat = get_str(ti, "Pattern") or ""
    if sdir:
        deny_msg = check_loop_guard(
            doc,
            plugin_root,
            f"find {sdir} {pat}",
            "[token-saver] Repeated identical find_by_name 3x with no edits since prior listing. Reuse earlier results or change Pattern (retry once to override).",
        )
        if deny_msg:
            emit_deny(deny_msg)
            return 0

    m = HOME_RE.match(sdir)
    if m:
        home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
        if home:
            sdir = home + (m.group(1) or "")
    if not sdir or not os.path.isdir(sdir):
        emit_allow()
        return 0

    use = [d for d in EXCLUDES if d not in sdir and d not in pat and os.path.isdir(os.path.join(sdir, d))]
    if not use:
        emit_allow()
        return 0

    emit_allow({"Excludes": use})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
