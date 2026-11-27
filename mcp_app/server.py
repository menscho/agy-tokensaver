#!/usr/bin/env python3
"""
Token Saver Global MCP App & Batched Accelerator Server (pure Python 3 stdlib).

Exposes:
- Resource `ui://token-saver/dashboard.html` (MIME: `text/html;profile=mcp-app`)
  with the official `@modelcontextprotocol/ext-apps` SDK bundled inline.
- Tool `open_token_saver` with `com.google.antigravity/ui` `global` entrypoint.
- App-only tools (`visibility: ["app"]`, invisible to LLM -> 0 prompt tokens):
  `ts_get_state`, `ts_save_settings`, `ts_build_handoff`, `ts_build_digest`,
  `ts_rollover_now`, `ts_clear_history`, `ts_check_updates`, `ts_apply_update`.
- 5 Eager-loaded batched local accelerator tools (engineering adapted from
  INK-Q/agy-fast, MIT License):
  `batch_search`, `apply_edits`, `quick_outline`, `read_slices`, `run_checks`.
- Direct CLI mode (`--cli <search|outline|slice|edit|check>`) for Swarm workers.
- Background daemon thread running the Smart Adaptive Auto-Rollover watchdog.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import core  # noqa: E402
from fast_checks import run_checks  # noqa: E402
from fast_edits import run_apply_edits  # noqa: E402
from fast_outline import run_quick_outline, run_read_slices  # noqa: E402
from fast_search import run_batch_search  # noqa: E402
from fast_utils import (  # noqa: E402
    MCP_CALL_BUDGET_SEC,
    resolve_edits_format,
    sanitize_schema_to_minimal_subset,
)

APP_URI = "ui://token-saver/dashboard.html"
MIME_MCP_APP = "text/html;profile=mcp-app"
SDK_PATH = HERE / "vendor" / "ext-apps-with-deps.js"
HTML_PATH = HERE / "dashboard.html"

ALL_FAST_TOOLS: tuple[str, ...] = (
    "batch_search",
    "apply_edits",
    "read_slices",
    "quick_outline",
    "run_checks",
)
_ALL_FAST_TOOL_SET = frozenset(ALL_FAST_TOOLS)

_sdk_iife_cache: str | None = None
_stdout_lock = threading.Lock()
_active_client_info: dict[str, Any] | None = None
_default_workspace_cwd: str | None = None
_selected_fast_tools: set[str] | None = None
_schema_compat_mode: str = "standard"


def _load_sdk_iife() -> str:
    global _sdk_iife_cache
    if _sdk_iife_cache is not None:
        return _sdk_iife_cache
    raw = SDK_PATH.read_text(encoding="utf-8")
    m = re.search(r"export\s*\{([^}]+)\}\s*;?\s*$", raw)
    if not m:
        raise RuntimeError("Unexpected ext-apps-with-deps.js export footer")
    body = raw[: m.start()]
    pairs: dict[str, str] = {}
    for item in m.group(1).split(","):
        item = item.strip()
        if not item:
            continue
        if " as " in item:
            local_n, exp_n = [x.strip() for x in item.split(" as ", 1)]
        else:
            local_n = exp_n = item
        pairs[exp_n] = local_n

    wanted = ["App", "applyDocumentTheme", "applyHostStyleVariables", "applyHostFonts", "getDocumentTheme"]
    mapping = ", ".join(f"{k}: {pairs[k]}" for k in wanted if k in pairs)
    _sdk_iife_cache = f"(function(){{\n{body}\nwindow.__MCP_APPS__ = {{{mapping}}};\n}})();"
    return _sdk_iife_cache


def render_dashboard_html() -> str:
    tpl = HTML_PATH.read_text(encoding="utf-8")
    sdk = _load_sdk_iife()
    return tpl.replace("/*__MCP_EXT_APPS_SDK__*/", sdk, 1)


def _build_apply_edits_tool(edits_format: str = "array") -> dict[str, Any]:
    prefer_str = edits_format == "string"
    if prefer_str:
        edits_schema: dict[str, Any] = {
            "type": "string",
            "description": (
                'JSON array string (1-50 files): [{"path","hunks"?:[{"target","replacement","startLine"?,"endLine"?,"replaceAll"?}],"create"?,"overwrite"?}]. '
                "Exact target in 1-based window (all if replaceAll). Nonempty hunks XOR full-file create."
            ),
        }
    else:
        edits_schema = {
            "type": "array",
            "minItems": 1,
            "maxItems": 50,
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "hunks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "target": {"type": "string"},
                                "replacement": {"type": "string"},
                                "startLine": {"type": "integer", "minimum": 1},
                                "endLine": {"type": "integer", "minimum": 1},
                                "replaceAll": {"type": "boolean"},
                            },
                            "required": ["target", "replacement"],
                        },
                    },
                    "create": {"type": "string"},
                    "overwrite": {"type": "boolean"},
                },
                "required": ["path"],
            },
        }
    base_desc = (
        "Atomic multi-file edits (1-50 files). bestEffort writes valid files atomically per file, "
        "gives closest lines, skips verify on failure. dryRun=false. verify shell in cwd after writes "
        "(60s/4000 chars default, agy drops at 180s). <=8800 UTF-8 bytes total. Pass explicit cwd if no roots/list."
    )
    return {
        "name": "apply_edits",
        "title": "Apply Multi-File Edits",
        "description": base_desc if prefer_str else (
            f"{base_desc} Exact target in 1-based window (all if replaceAll). Nonempty hunks XOR full-file create."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string"},
                "edits": edits_schema,
                "dryRun": {"type": "boolean"},
                "bestEffort": {"type": "boolean"},
                "verify": {"type": "string"},
                "verifyTimeoutSec": {"type": "integer", "minimum": 1, "maximum": MCP_CALL_BUDGET_SEC},
                "maxVerifyChars": {"type": "integer", "minimum": 300, "maximum": 7500},
            },
            "required": ["edits"],
        },
    }


FAST_TOOL_DEFS: dict[str, dict[str, Any]] = {
    "batch_search": {
        "name": "batch_search",
        "title": "Batch Search",
        "description": (
            "Parallel search (1-20 queries): literal text, regex, fuzzy paths, glob (*/{a,b}, no |), read-only git. "
            "Pass explicit cwd if no roots/list. path rel/abs file/dir; glob filters text/regex; fileType lang/ext. "
            "multiline spans lines; outputMode: content|files_with_matches|count; sortBy: path|mtime. <=8800B UTF-8 max."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string"},
                "queries": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["text", "regex", "glob", "fuzzy", "git"]},
                            "pattern": {"type": "string"},
                            "path": {"type": "string"},
                            "glob": {"type": "string"},
                            "multiline": {"type": "boolean"},
                            "outputMode": {"type": "string", "enum": ["content", "files_with_matches", "count"]},
                            "fileType": {"type": "string"},
                            "sortBy": {"type": "string", "enum": ["path", "mtime"]},
                            "caseSensitive": {"type": "boolean"},
                            "contextLines": {"type": "integer", "minimum": 0, "maximum": 10},
                            "maxResults": {"type": "integer", "minimum": 1, "maximum": 200},
                        },
                        "required": ["type", "pattern"],
                    },
                },
                "maxChars": {"type": "integer", "minimum": 500, "maximum": 8800},
            },
            "required": ["queries"],
        },
    },
    "quick_outline": {
        "name": "quick_outline",
        "title": "Quick Symbol Outline",
        "description": (
            "Fast symbol/section line-range outlines for large files (TS/JS/HTML/Vue/Svelte/CSS/Py/GDScript/Go/Rust/Java/MD/JSON/YAML; "
            "prefer 10-20, max 40) before parallel ranged read_file(StartLine, EndLine). Flat paths-only schema. <=8800B UTF-8 max."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string"},
                "paths": {"type": "array", "minItems": 1, "maxItems": 40, "items": {"type": "string"}},
                "maxChars": {"type": "integer", "minimum": 1000, "maximum": 8800},
            },
            "required": ["paths"],
        },
    },
    "read_slices": {
        "name": "read_slices",
        "title": "Outlines & Targeted Line Slices",
        "description": (
            "Batch outlines (max 40) + tiny <=120-line slices. For >150 known lines total, prefer parallel read_file(StartLine, EndLine). "
            "slices: 1-based inclusive [start,end]; lineRanges: 'path:start-end,s2-e2'. <=8800B UTF-8 max."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string"},
                "outlines": {"type": "array", "maxItems": 40, "items": {"type": "string"}},
                "slices": {
                    "type": "array",
                    "maxItems": 30,
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "ranges": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "array",
                                    "minItems": 2,
                                    "maxItems": 2,
                                    "items": {"type": "integer", "minimum": 1},
                                },
                            },
                        },
                        "required": ["path", "ranges"],
                    },
                },
                "lineRanges": {"type": "array", "maxItems": 30, "items": {"type": "string"}},
                "maxChars": {"type": "integer", "minimum": 1000, "maximum": 8800},
            },
        },
    },
    "run_checks": {
        "name": "run_checks",
        "title": "Concurrent Checks Runner",
        "description": (
            f"Batch >=2 independent non-interactive shell commands concurrently (1-12 cmds; concurrency 1-8 default 4; "
            f"shared 90s call budget, budgetSec up to {MCP_CALL_BUDGET_SEC}; unstarted cmds return NOT RUN). outputMode 'compact' (default) collapses PASS & folds failures; 'full' keeps stdout. <=8800B UTF-8 max."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string"},
                "commands": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 12,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "command": {"type": "string"},
                            "cwd": {"type": "string"},
                            "timeoutSec": {"type": "integer", "minimum": 1, "maximum": MCP_CALL_BUDGET_SEC},
                        },
                        "required": ["command"],
                    },
                },
                "concurrency": {"type": "integer", "minimum": 1, "maximum": 8},
                "timeoutSec": {"type": "integer", "minimum": 1, "maximum": MCP_CALL_BUDGET_SEC},
                "budgetSec": {"type": "integer", "minimum": 1, "maximum": MCP_CALL_BUDGET_SEC},
                "outputMode": {"type": "string", "enum": ["compact", "full"]},
                "maxChars": {"type": "integer", "minimum": 500, "maximum": 8800},
            },
            "required": ["commands"],
        },
    },
}

UI_TOOLS: list[dict[str, Any]] = [
    {
        "name": "open_token_saver",
        "title": "agy-tokensaver",
        "description": "Open the agy-tokensaver dashboard (savings, Auto-Handoff settings, session archive, updates).",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "_meta": {
            "ui": {"resourceUri": APP_URI},
            "ui/resourceUri": APP_URI,
            "com.google.antigravity/ui": {
                "entrypoints": [
                    {"type": "global", "title": "agy-tokensaver"},
                    {"type": "aux_pane", "title": "agy-tokensaver"},
                ]
            },
        },
    },
    {
        "name": "ts_get_state",
        "title": "Get Token Saver State",
        "description": "App-only: dashboard state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversationId": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "ts_save_settings",
        "title": "Save Token Saver Settings",
        "description": "App-only: save settings.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "profile": {"type": "string"},
                "auto_rollover": {"type": "boolean"},
                "mode": {"type": "string"},
                "compact_reads": {"type": "boolean"},
                "concise_output": {"type": "boolean"},
                "startup_snapshot": {"type": "boolean"},
                "threshold_mode": {"type": "string"},
                "threshold_k": {"type": "integer"},
                "min_turns": {"type": "integer"},
                "cooldown_min": {"type": "integer"},
                "model": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "ts_build_handoff",
        "title": "Build Handoff Packet",
        "description": "App-only: build handoff packet.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "archive": {"type": "boolean"},
            },
            "additionalProperties": True,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "ts_build_digest",
        "title": "Build Repo Digest",
        "description": "App-only: build repo digest.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "ts_rollover_now",
        "title": "Rollover Session Now",
        "description": "App-only: archive chat and start a fresh one.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "model": {"type": "string"},
                "spawn_chat": {"type": "boolean"},
            },
            "additionalProperties": True,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "ts_clear_history",
        "title": "Remove Archive Entry",
        "description": "App-only: delete archive entries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "ts_check_updates",
        "title": "Check GitHub Updates",
        "description": "App-only: check for updates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "force": {"type": "boolean"},
            },
            "additionalProperties": True,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "ts_apply_update",
        "title": "Install Update Now",
        "description": "App-only: install update.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
]


def get_exposed_tools() -> list[dict[str, Any]]:
    """Build current tool list applying host-compat editsFormat, --tools filter, and --schema-compat."""
    edits_fmt, _ = resolve_edits_format(client_info=_active_client_info)
    selected = _selected_fast_tools
    fast_list: list[dict[str, Any]] = []
    for name in ALL_FAST_TOOLS:
        if selected is not None and name not in selected:
            continue
        tdef = _build_apply_edits_tool(edits_fmt) if name == "apply_edits" else FAST_TOOL_DEFS[name]
        if _schema_compat_mode == "minimal":
            tdef = {**tdef, "inputSchema": sanitize_schema_to_minimal_subset(tdef["inputSchema"])}
        fast_list.append(tdef)
    return sorted([*UI_TOOLS, *fast_list], key=lambda t: t["name"])


# Kept for backwards compatibility with callers/tests importing TOOLS directly
TOOLS: list[dict[str, Any]] = get_exposed_tools()

RESOURCES: list[dict[str, Any]] = [
    {
        "uri": APP_URI,
        "name": "agy-tokensaver",
        "description": "Full-page agy-tokensaver quota analytics, Smart Auto-Rollover, session archive, and updates.",
        "mimeType": MIME_MCP_APP,
        "_meta": {
            "ui": {
                "prefersBorder": False,
            }
        },
    }
]


def _is_non_workspace_dir(dir_path: str) -> str | None:
    try:
        canon = os.path.realpath(os.path.abspath(dir_path))
    except OSError:
        canon = os.path.abspath(dir_path)
    if os.path.dirname(canon) == canon:
        return "the filesystem root"
    homes = {
        os.path.realpath(os.path.abspath(h.strip()))
        for h in (str(Path.home()), os.environ.get("HOME", ""), os.environ.get("USERPROFILE", ""))
        if h and h.strip()
    }
    if canon in homes:
        return "the home directory"
    return None


def _is_installed_plugin_dir(dir_path: str) -> bool:
    try:
        canon = os.path.realpath(os.path.abspath(dir_path))
    except OSError:
        canon = os.path.abspath(dir_path)
    posix = canon.replace("\\", "/")
    if re.search(r"(?:^|/)(?:\.gemini/(?:config/|antigravity[^/]*/)?plugins|\.agents/plugins)/[^/]+(?:/|$)", posix, re.I):
        return True
    for env_root in (os.environ.get("PLUGIN_ROOT", ""), os.environ.get("AGY_PLUGIN_ROOT", "")):
        if env_root.strip():
            try:
                er = os.path.realpath(os.path.abspath(env_root.strip()))
                if canon == er or canon.startswith(er + os.sep):
                    return True
            except OSError:
                pass
    return False


def _resolve_fast_tool_cwd(args: dict[str, Any]) -> tuple[bool, dict[str, Any] | str]:
    raw_cwd = args.get("cwd")
    if raw_cwd is not None and not isinstance(raw_cwd, str):
        return False, f"Error: 'cwd' must be a string path (received {type(raw_cwd).__name__})."
    trimmed = (raw_cwd or "").strip()
    is_home = trimmed == "~" or trimmed.startswith(("~/", "~\\"))
    if not trimmed:
        if _default_workspace_cwd:
            return True, {**args, "cwd": _default_workspace_cwd}
        cur = os.getcwd()
        if _is_installed_plugin_dir(cur):
            return False, (
                f"Error: 'cwd' is required because no workspace root was reported via MCP roots/list and "
                f"the server started inside the plugin directory ({cur}). Pass the active workspace root explicitly in 'cwd'."
            )
        nw = _is_non_workspace_dir(cur)
        if nw:
            return False, (
                f"Error: 'cwd' is required because no workspace root was reported via MCP roots/list and "
                f"the server started in {nw} ({cur}). Pass the active workspace root explicitly in 'cwd'."
            )
        return True, dict(args)
    if not os.path.isabs(trimmed) and not is_home:
        if _default_workspace_cwd:
            return True, {**args, "cwd": os.path.abspath(os.path.join(_default_workspace_cwd, trimmed))}
        cur = os.getcwd()
        if _is_installed_plugin_dir(cur) or _is_non_workspace_dir(cur):
            return False, (
                f"Error: 'cwd' must be an absolute workspace path because no workspace root was reported via MCP roots/list. "
                f"Received relative cwd: {trimmed!r}."
            )
    return True, {**args, "cwd": trimmed}


def _log_fast_tool_telemetry(tool_name: str, args: dict[str, Any], out_text: str, is_error: bool) -> None:
    if is_error:
        return
    try:
        ops = 1
        if tool_name == "batch_search" and isinstance(args.get("queries"), list):
            ops = len(args["queries"])
        elif tool_name == "apply_edits" and isinstance(args.get("edits"), list):
            ops = len(args["edits"]) + (1 if args.get("verify") else 0)
        elif tool_name == "quick_outline" and isinstance(args.get("paths"), list):
            ops = len(args["paths"])
        elif tool_name == "read_slices":
            ops = len(args.get("outlines") or []) + len(args.get("slices") or []) + len(args.get("lineRanges") or [])
        elif tool_name == "run_checks" and isinstance(args.get("commands"), list):
            ops = len(args["commands"])
        core.DATA_DIR.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": int(time.time()),
            "source": "fast_mcp",
            "category": tool_name,
            "action": "batched",
            "ops": max(1, ops),
            "kept_bytes": len(out_text.encode("utf-8")),
            "raw_bytes": len(out_text.encode("utf-8")),
            "est_tokens_saved": 0,
            "session_id": os.environ.get("ANTIGRAVITY_CONVERSATION_ID", ""),
        }
        with core.EVENTS_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _handle_tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name in _ALL_FAST_TOOL_SET:
        ok, resolved = _resolve_fast_tool_cwd(args)
        if not ok:
            return {"isError": True, "content": [{"type": "text", "text": str(resolved)}]}
        assert isinstance(resolved, dict)
        if name == "batch_search":
            res = run_batch_search(resolved)
        elif name == "apply_edits":
            res = run_apply_edits(resolved)
        elif name == "quick_outline":
            res = run_quick_outline(resolved)
        elif name == "read_slices":
            res = run_read_slices(resolved)
        else:
            res = run_checks(resolved)
        out_txt = str(res.get("text") or "")
        is_err = bool(res.get("isError"))
        _log_fast_tool_telemetry(name, resolved, out_txt, is_err)
        resp: dict[str, Any] = {"content": [{"type": "text", "text": out_txt}]}
        if is_err:
            resp["isError"] = True
        return resp

    conv_id = str(args.get("conversationId") or os.environ.get("ANTIGRAVITY_CONVERSATION_ID") or "")

    if name == "open_token_saver":
        st = core.build_state(conv_id)
        tot = st["savings"]["totals"]
        summary = (
            f"Token Saver Dashboard loaded: {tot['less_usage_pct']}% avg quota saved "
            f"(+{tot['more_capacity_pct']}% effective token capacity), "
            f"Auto-Rollover={st['config']['mode']} @ {st['adaptive']['cap_k']}k."
        )
        return {
            "content": [{"type": "text", "text": summary}],
            "structuredContent": st,
        }

    if name == "ts_get_state":
        st = core.build_state(conv_id)
        return {
            "content": [{"type": "text", "text": "ok"}],
            "structuredContent": st,
        }

    if name == "ts_save_settings":
        updated = core.save_config(args)
        st = core.build_state(conv_id)
        return {
            "content": [{"type": "text", "text": json.dumps(updated)}],
            "structuredContent": {"ok": True, "config": updated, "state": st},
        }

    if name == "ts_build_handoff":
        st = core.build_state(conv_id)
        ws = str(args.get("workspace") or st["workspace"])
        if args.get("archive"):
            res = core.perform_rollover(
                ws=ws,
                active=st.get("active"),
                kind="snapshot",
                model=st["config"].get("model", "inherit"),
                spawn_chat=False,
                cap_k=st["adaptive"]["cap_k"],
            )
            pkt = res.get("entry", {}).get("packet", "")
            est = res.get("entry", {}).get("est_packet_tokens", 0)
            return {
                "content": [{"type": "text", "text": pkt}],
                "structuredContent": {
                    "ok": res.get("ok", False),
                    "packet": pkt,
                    "est_tokens": est,
                    "state": core.build_state(conv_id),
                },
            }
        ho = core.build_handoff_packet(ws, (st.get("active") or {}).get("conversation_id", conv_id))
        return {
            "content": [{"type": "text", "text": ho["packet"]}],
            "structuredContent": {"ok": True, **ho},
        }

    if name == "ts_build_digest":
        st = core.build_state(conv_id)
        ws = str(args.get("workspace") or st["workspace"])
        dig = core.build_repo_digest(ws)
        return {
            "content": [{"type": "text", "text": dig["digest"]}],
            "structuredContent": {"ok": True, **dig},
        }

    if name == "ts_rollover_now":
        st = core.build_state(conv_id)
        ws = str(args.get("workspace") or st["workspace"])
        model = str(args.get("model") or st["config"].get("model") or "inherit")
        spawn = bool(args.get("spawn_chat", True))
        res = core.perform_rollover(
            ws=ws,
            active=st.get("active"),
            kind="manual" if spawn else "snapshot",
            model=model,
            spawn_chat=spawn,
            cap_k=st["adaptive"]["cap_k"],
        )
        return {
            "content": [{"type": "text", "text": "Rollover complete" if res.get("ok") else str(res.get("error"))}],
            "structuredContent": {**res, "state": core.build_state(conv_id)},
        }

    if name == "ts_clear_history":
        target_id = str(args.get("id") or "")
        with core._state_lock:
            hist = core.load_rollover_history()
            if target_id == "__all__":
                hist = []
            elif target_id:
                hist = [h for h in hist if h.get("id") != target_id]
            core._atomic_write_json(core.ROLLOVER_HISTORY_FILE, hist)
        return {
            "content": [{"type": "text", "text": "ok"}],
            "structuredContent": {"ok": True, "state": core.build_state(conv_id)},
        }

    if name == "ts_check_updates":
        upd = core.check_github_updates(force=bool(args.get("force", True)))
        return {
            "content": [{"type": "text", "text": f"v{upd['installed_version']} -> latest v{upd['latest_version']}"}],
            "structuredContent": {"ok": True, "updates": upd, "state": core.build_state(conv_id)},
        }

    if name == "ts_apply_update":
        res = core.apply_update()
        return {
            "content": [{"type": "text", "text": "Updated" if res.get("ok") else str(res.get("error"))}],
            "structuredContent": {**res, "state": core.build_state(conv_id)},
        }

    return {
        "isError": True,
        "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
    }


def _send(msg: dict[str, Any]) -> None:
    line = json.dumps(msg, separators=(",", ":"), sort_keys=True, ensure_ascii=False) + "\n"
    with _stdout_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def _send_result(req_id: Any, result: Any) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _send_error(req_id: Any, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _file_uri_to_path(uri: str) -> str | None:
    if not uri.lower().startswith("file:"):
        return None
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    if os.name == "nt" and re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return os.path.normpath(path) if path else None


def _dispatch(req: dict[str, Any]) -> None:
    global _active_client_info, _default_workspace_cwd
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params") or {}

    # Handle incoming response to our outgoing roots/list request
    if not method and "result" in req and req_id is not None:
        roots = (req.get("result") or {}).get("roots")
        if isinstance(roots, list):
            for r in roots:
                if isinstance(r, dict) and isinstance(r.get("uri"), str):
                    p = _file_uri_to_path(r["uri"])
                    if p and os.path.isdir(p):
                        _default_workspace_cwd = p
                        break
        return

    # Notifications have no id -> never send a response
    if req_id is None:
        if method == "notifications/roots/list_changed":
            _send({"jsonrpc": "2.0", "id": "ts-roots-refresh", "method": "roots/list"})
        return

    try:
        if method == "initialize":
            proto = str(params.get("protocolVersion") or "2025-03-26")
            ci = params.get("clientInfo")
            if isinstance(ci, dict):
                _active_client_info = ci
            caps = params.get("capabilities")
            _send_result(
                req_id,
                {
                    "protocolVersion": proto,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                    },
                    "serverInfo": {"name": "token-saver", "version": core.get_installed_version()},
                },
            )
            if isinstance(caps, dict) and isinstance(caps.get("roots"), dict):
                _send({"jsonrpc": "2.0", "id": "ts-roots-init", "method": "roots/list"})
            return

        if method == "ping":
            _send_result(req_id, {})
            return

        if method == "tools/list":
            _send_result(req_id, {"tools": get_exposed_tools()})
            return

        if method == "tools/call":
            tname = str(params.get("name") or "")
            targs = params.get("arguments") or {}
            if not isinstance(targs, dict):
                targs = {}
            _send_result(req_id, _handle_tool_call(tname, targs))
            return

        if method == "resources/list":
            _send_result(req_id, {"resources": RESOURCES})
            return

        if method == "resources/templates/list":
            _send_result(req_id, {"resourceTemplates": []})
            return

        if method == "resources/read":
            uri = str(params.get("uri") or "")
            if uri == APP_URI:
                html = render_dashboard_html()
                _send_result(
                    req_id,
                    {
                        "contents": [
                            {
                                "uri": APP_URI,
                                "mimeType": MIME_MCP_APP,
                                "text": html,
                                "_meta": {"ui": {"prefersBorder": False}},
                            }
                        ]
                    },
                )
            else:
                _send_error(req_id, -32002, f"Resource not found: {uri}")
            return

        _send_error(req_id, -32601, f"Method not found: {method}")
    except Exception as exc:
        _send_error(req_id, -32603, f"Internal error: {exc}")


def _read_messages():
    """Yield parsed JSON-RPC messages from stdin (supports newline JSON and Content-Length framing)."""
    buf = sys.stdin.buffer
    while True:
        line = buf.readline()
        if not line:
            break
        s = line.decode("utf-8", errors="replace").strip()
        if not s:
            continue
        if s.lower().startswith("content-length:"):
            length = int(s.split(":", 1)[1].strip())
            while True:
                hdr = buf.readline()
                if not hdr or hdr in (b"\r\n", b"\n"):
                    break
            body = buf.read(length)
            if not body:
                break
            try:
                yield json.loads(body.decode("utf-8", errors="replace"))
            except Exception:
                continue
        else:
            try:
                yield json.loads(s)
            except Exception:
                continue


def parse_server_args(argv: list[str]) -> tuple[bool, str | None]:
    """Configure optional --tools and --schema-compat flags. Returns (show_help, error)."""
    global _selected_fast_tools, _schema_compat_mode
    _selected_fast_tools = None
    _schema_compat_mode = (
        os.environ.get("AGY_TS_SCHEMA_COMPAT")
        or os.environ.get("AGY_FAST_SCHEMA_COMPAT")
        or "standard"
    ).strip().lower()
    if _schema_compat_mode not in ("standard", "minimal"):
        _schema_compat_mode = "standard"

    env_tools = (os.environ.get("AGY_TS_TOOLS") or os.environ.get("AGY_FAST_TOOLS") or "").strip()
    if env_tools:
        parts = [p.strip() for p in env_tools.split(",")]
        bad = [p for p in parts if p not in _ALL_FAST_TOOL_SET]
        if bad or not parts:
            return False, f"Unknown tool(s) in AGY_TS_TOOLS: {', '.join(bad)}. Valid: {', '.join(ALL_FAST_TOOLS)}."
        _selected_fast_tools = set(parts)

    i = 0
    while i < len(argv):
        a = argv[i]
        i += 1
        if a in ("-h", "--help"):
            return True, None
        if a == "--tools" or a.startswith("--tools="):
            raw = a.split("=", 1)[1] if "=" in a else (argv[i] if i < len(argv) else "")
            if "=" not in a:
                i += 1
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            bad = [p for p in parts if p not in _ALL_FAST_TOOL_SET]
            if not parts or bad:
                return False, f"Invalid --tools value {raw!r}. Valid: {', '.join(ALL_FAST_TOOLS)}."
            _selected_fast_tools = set(parts)
        elif a == "--schema-compat" or a.startswith("--schema-compat="):
            raw = a.split("=", 1)[1] if "=" in a else (argv[i] if i < len(argv) else "")
            if "=" not in a:
                i += 1
            val = raw.strip().lower()
            if val not in ("standard", "minimal"):
                return False, f"Invalid --schema-compat value {raw!r} (expected: standard | minimal)."
            _schema_compat_mode = val
        else:
            return False, f"Unknown argument {a!r}. Run with --help or --cli --help."
    return False, None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--cli":
        from fast_cli import run_cli

        return run_cli(args[1:])

    show_help, err = parse_server_args(args)
    if err:
        sys.stderr.write(f"Error: {err}\n")
        return 1
    if show_help:
        sys.stdout.write(
            "agy-tokensaver MCP App & Batched Accelerator Server\n\n"
            "Usage:\n"
            "  python3 mcp_app/server.py [--tools <list>] [--schema-compat standard|minimal]\n"
            "  python3 mcp_app/server.py --cli <search|outline|slice|edit|check> [args...]\n"
        )
        return 0

    stop_ev = threading.Event()
    watchdog = threading.Thread(target=core.auto_rollover_loop, args=(stop_ev,), daemon=True)
    watchdog.start()
    try:
        for msg in _read_messages():
            if isinstance(msg, dict):
                _dispatch(msg)
    except KeyboardInterrupt:
        pass
    finally:
        stop_ev.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
