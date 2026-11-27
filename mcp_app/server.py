#!/usr/bin/env python3
"""
Token Saver Global MCP App Server (pure Python 3 stdlib, stdio JSON-RPC 2.0).

Exposes:
- Resource `ui://token-saver/dashboard.html` (MIME: `text/html;profile=mcp-app`)
  with the official `@modelcontextprotocol/ext-apps` SDK bundled inline.
- Tool `open_token_saver` with `com.google.antigravity/ui` `global` entrypoint
  (opens full-page from the left sidebar — never as a chat side pane).
- App-only tools (`visibility: ["app"]`, invisible to LLM -> 0 prompt tokens):
  `ts_get_state`, `ts_save_settings`, `ts_build_handoff`, `ts_build_digest`,
  `ts_rollover_now`, `ts_clear_history`.
- Background daemon thread running the Smart Adaptive Auto-Rollover watchdog.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import core  # noqa: E402

APP_URI = "ui://token-saver/dashboard.html"
MIME_MCP_APP = "text/html;profile=mcp-app"
SDK_PATH = HERE / "vendor" / "ext-apps-with-deps.js"
HTML_PATH = HERE / "dashboard.html"

_sdk_iife_cache: str | None = None
_stdout_lock = threading.Lock()


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


TOOLS: list[dict[str, Any]] = [
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


def _handle_tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
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


def _dispatch(req: dict[str, Any]) -> None:
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params") or {}

    # Notifications have no id -> never send a response
    if req_id is None:
        return

    try:
        if method == "initialize":
            proto = str(params.get("protocolVersion") or "2025-03-26")
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
            return

        if method == "ping":
            _send_result(req_id, {})
            return

        if method == "tools/list":
            _send_result(req_id, {"tools": sorted(TOOLS, key=lambda t: t["name"])})
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


def main() -> int:
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
