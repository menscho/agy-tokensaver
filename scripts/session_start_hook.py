#!/usr/bin/env python3
"""Fail-open SessionStart hook injecting a sub-50ms <=1.5KB workspace snapshot.

Runs once per Antigravity session under `python3 -S` (stdlib-only).
Respects AGY_TS_HOOKS=off, AGY_TS_STARTUP_SNAPSHOT=off, and dashboard mode=off /
startup_snapshot=false. Never blocks startup on malformed/missing input.

Incorporates engineering patterns adapted from INK-Q/agy-fast (MIT License).
"""

from __future__ import annotations

import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(SCRIPT_DIR)
MCP_APP_DIR = os.path.join(PLUGIN_ROOT, "mcp_app")
for _p in (SCRIPT_DIR, MCP_APP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from hook_common import (  # noqa: E402
    data_dir,
    decode_hook_input,
    emit_json,
    hooks_disabled,
    read_stdin_capped,
    script_plugin_root,
)

_OFF_RE = re.compile(r"^(?:0|off|false|disabled)$", re.I)


def _snapshot_disabled(plugin_root: str) -> bool:
    env_v = os.environ.get("AGY_TS_STARTUP_SNAPSHOT")
    if env_v is not None and _OFF_RE.match(env_v.strip()):
        return True
    if hooks_disabled(plugin_root):
        return True
    cfg = os.path.join(data_dir(plugin_root), "hud_config.json")
    try:
        with open(cfg, "rb") as fh:
            buf = fh.read(4096).decode("utf-8", errors="replace")
        if re.search(r'"startup_snapshot"\s*:\s*false\b', buf, re.I):
            return True
    except OSError:
        pass
    return False


def _first_non_empty(val: object) -> str:
    if isinstance(val, list):
        for item in val:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def main() -> int:
    try:
        plugin_root = script_plugin_root(__file__)
        if _snapshot_disabled(plugin_root):
            emit_json({})
            return 0
        raw = read_stdin_capped(256 * 1024)
        doc = decode_hook_input(raw)
        if not doc:
            emit_json({})
            return 0

        ws = ""
        if isinstance(doc.get("cwd"), str) and doc["cwd"].strip():
            ws = doc["cwd"].strip()
        else:
            for key in ("workspacePaths", "workspace_paths", "workspaceRoots", "workspace_roots"):
                cand = _first_non_empty(doc.get(key))
                if cand:
                    ws = cand
                    break
        if not ws or not os.path.isdir(ws):
            emit_json({})
            return 0

        from fast_snapshot import MAX_SNAPSHOT_BYTES, build_workspace_snapshot
        from fast_utils import resolve_edits_format, truncate_with_byte_notice, utf8_len

        snapshot = build_workspace_snapshot(ws, max_bytes=MAX_SNAPSHOT_BYTES, git_timeout_ms=800)
        if not snapshot:
            emit_json({})
            return 0

        tx = doc.get("transcriptPath") or doc.get("transcript_path")
        edits_fmt, host_lbl = resolve_edits_format(
            transcript_path=tx if isinstance(tx, str) else None,
        )
        msg = snapshot
        if edits_fmt == "string":
            notice = (
                f"[agy-tokensaver compat] host={host_lbl} | apply_edits.edits (lazy call_mcp_tool mode): "
                "pass as JSON array string (not native array) to avoid call_mcp_tool [] drops."
            )
            combined = f"{snapshot}\n{notice}"
            if utf8_len(combined) <= MAX_SNAPSHOT_BYTES:
                msg = combined
            else:
                sb = MAX_SNAPSHOT_BYTES - utf8_len(notice) - 1
                msg = (
                    f"{truncate_with_byte_notice(snapshot, sb)}\n{notice}"
                    if sb >= 64
                    else truncate_with_byte_notice(combined, MAX_SNAPSHOT_BYTES)
                )

        emit_json({"injectSteps": [{"systemMessage": {"systemMessage": msg}}]})
        return 0
    except Exception:
        try:
            sys.stdout.buffer.write(b"{}")
            sys.stdout.buffer.flush()
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
