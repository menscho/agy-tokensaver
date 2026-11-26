#!/usr/bin/env python3
"""Execute a seamless Antigravity conversation handoff via ConversationService (or agentapi).

Runs in an agent shell command (where ANTIGRAVITY_LS_ADDRESS and ANTIGRAVITY_CSRF_TOKEN
are provided by Antigravity), creates the fresh continuation conversation with the
compact handoff packet + user's exact request already running, optionally archives the
old conversation, and records rollover telemetry/history.

Pure Python 3.9+ stdlib, zero external dependencies.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from hook_common import data_dir, script_plugin_root

COLD_START_TOK = 17_000
_CONT_RE = re.compile(r"\s*\(cont\.\s*(\d+)\)\s*$")


def _atomic_write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def _load_json_dict(path: str) -> dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            d = json.loads(fh.read())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _load_json_list(path: str) -> list[dict[str, Any]]:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            d = json.loads(fh.read())
        return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []
    except Exception:
        return []


def _next_title(old_title: str) -> str:
    t = " ".join((old_title or "").split()).strip()
    if not t:
        return ""
    m = _CONT_RE.search(t)
    if m:
        n = int(m.group(1)) + 1
        base = t[: m.start()].strip()
        return f"{base[:64]} (cont. {n})"
    return f"{t[:64]} (cont. 2)"


def _conv_rpc(ls_addr: str, csrf: str, method: str, body: dict[str, Any], timeout: float = 12.0) -> dict[str, Any]:
    req = urllib.request.Request(
        f"http://{ls_addr}/product.v1.ConversationService/{method}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "x-antigravity-csrf-token": csrf,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    parsed = json.loads(raw) if raw else {}
    return parsed if isinstance(parsed, dict) else {}


def _spawn_via_agentapi(prompt: str, model: str, title: str) -> str:
    exe = os.environ.get("ANTIGRAVITY_AGENTAPI_EXE", "")
    cmd: list[str]
    if exe and os.path.isfile(exe):
        cmd = [exe, "agentapi", "new-conversation"]
    else:
        shim = os.path.join(
            os.path.expanduser("~"),
            ".gemini",
            "antigravity",
            "bin",
            "agentapi.exe" if os.name == "nt" else "agentapi",
        )
        if not os.path.isfile(shim):
            return ""
        cmd = [shim, "new-conversation"]
    if model and model not in ("inherit", "auto", "unknown"):
        cmd.append(f"--model={model}")
    if title:
        cmd.append(f"--title={title}")
    cmd.append(prompt)
    p = subprocess.run(cmd, text=True, capture_output=True, timeout=15)
    if p.returncode != 0:
        return ""
    try:
        parsed = json.loads((p.stdout or "").strip() or "{}")
        resp_conv = (parsed.get("response") or {}).get("newConversation") or {}
        return str(
            resp_conv.get("conversationId")
            or resp_conv.get("conversation_id")
            or parsed.get("conversationId")
            or parsed.get("conversation_id")
            or parsed.get("id")
            or ""
        )
    except Exception:
        return ""


def execute_handoff(
    *,
    cid: str,
    ws: str,
    full_prompt: str,
    pkt_tokens: int,
    user_input: str = "",
    cur_ctx: int = 0,
    turns: int = 0,
    cap_k: int = 110,
    trigger: str = "manual",
    target_model: str = "",
    auto_archive: bool = True,
    kind: str = "auto",
    delegated: bool = False,
    dd: str = "",
) -> dict[str, Any]:
    ls_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "").strip()
    csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "").strip()
    if not ls_addr:
        return {"ok": False, "error": "ANTIGRAVITY_LS_ADDRESS is not set"}
    if not full_prompt.strip():
        return {"ok": False, "error": "Empty handoff prompt"}

    project_id = os.environ.get("ANTIGRAVITY_PROJECT_ID", "").strip()
    env_id = ""
    old_title = ""
    group_id = ""
    if csrf and cid:
        try:
            meta_resp = _conv_rpc(ls_addr, csrf, "GetConversation", {"conversationId": cid}, timeout=6.0)
            meta_raw = meta_resp.get("metadata")
            meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}
            if meta.get("parentConversationId"):
                return {"ok": False, "error": "Skipping subagent/child conversation"}
            if meta.get("projectId"):
                project_id = str(meta["projectId"])
            if meta.get("environmentId"):
                env_id = str(meta["environmentId"])
            ann_raw = meta_resp.get("annotations")
            ann: dict[str, Any] = ann_raw if isinstance(ann_raw, dict) else {}
            st_raw = meta_resp.get("state")
            st_obj: dict[str, Any] = st_raw if isinstance(st_raw, dict) else {}
            old_title = str(ann.get("title") or st_obj.get("summary") or "")
            group_id = str(ann.get("conversationGroupId") or "")
        except Exception:
            pass

    new_title = _next_title(old_title)
    new_id = ""
    rpc_err = ""
    if csrf:
        body: dict[str, Any] = {
            "prompt": [{"text": full_prompt}],
            "tags": ["token-saver-handoff"],
        }
        if project_id:
            body["projectId"] = project_id
            if env_id:
                body["environmentId"] = env_id
        if new_title:
            body["title"] = new_title
        if target_model and target_model not in ("auto", "inherit", "unknown"):
            body["model"] = target_model
        try:
            created = _conv_rpc(ls_addr, csrf, "CreateConversation", body, timeout=12.0)
            conv_raw = created.get("conversation")
            conv_obj: dict[str, Any] = conv_raw if isinstance(conv_raw, dict) else {}
            new_id = str(conv_obj.get("id") or "")
        except Exception as exc:
            rpc_err = str(exc)

    if not new_id:
        try:
            new_id = _spawn_via_agentapi(full_prompt, target_model, new_title)
        except Exception as exc:
            if not rpc_err:
                rpc_err = str(exc)

    if not new_id:
        return {"ok": False, "error": rpc_err or "Could not create continuation conversation"}

    archived_old = False
    if csrf:
        if group_id:
            try:
                _conv_rpc(
                    ls_addr,
                    csrf,
                    "UpdateConversation",
                    {"conversationId": new_id, "annotations": {"conversationGroupId": group_id}},
                    timeout=4.0,
                )
            except Exception:
                pass
        if auto_archive and cid:
            try:
                _conv_rpc(
                    ls_addr,
                    csrf,
                    "UpdateConversation",
                    {"conversationId": cid, "annotations": {"archived": True}},
                    timeout=4.0,
                )
                archived_old = True
            except Exception:
                pass

    now_i = int(time.time())
    # Honest savings estimate: Path 2 delegated mode does not avoid Turn-0 cold rebuilds,
    # only future cache-drop rebuilds & per-turn cache reads when context is large (>=90k).
    if delegated:
        est_in_saved = max(0, cur_ctx - 25_000) if cur_ctx >= 90_000 else 0
    elif trigger == "cold_cache":
        est_in_saved = max(0, cur_ctx - COLD_START_TOK)
    else:
        est_in_saved = max(0, cur_ctx - 23_000)
    est_cr_saved = max(0, cur_ctx - 23_000) * 12
    if dd:
        try:
            rstate_path = os.path.join(dd, "rollover_state.json")
            rstate = _load_json_dict(rstate_path)
            rolled_convs = dict(rstate.get("rolled_convs") or {})
            if cid:
                rolled_convs[cid] = now_i
            if len(rolled_convs) > 200:
                rolled_convs = dict(sorted(rolled_convs.items(), key=lambda kv: kv[1], reverse=True)[:150])
            rstate["rolled_convs"] = rolled_convs
            rstate.setdefault("snapshotted_convs", {})
            rstate["last_rollover_ts"] = now_i
            _atomic_write_json(rstate_path, rstate)

            hist_path = os.path.join(dd, "rollover_history.json")
            hist = _load_json_list(hist_path)
            hist.insert(
                0,
                {
                    "id": f"ro-{now_i}",
                    "ts": now_i,
                    "iso_time": datetime.datetime.fromtimestamp(now_i).strftime("%Y-%m-%d %H:%M"),
                    "kind": kind,
                    "trigger": trigger,
                    "delegated": delegated,
                    "from_conv": cid[:8] if cid else "manual",
                    "to_conv": new_id[:8],
                    "to_conv_full": new_id,
                    "workspace": ws,
                    "goal": (old_title or user_input or "Session Continuation")[:160],
                    "peak_tokens": cur_ctx,
                    "turns": turns,
                    "cap_k": cap_k,
                    "est_input_saved": est_in_saved,
                    "est_cache_saved": est_cr_saved,
                    "packet": full_prompt,
                    "est_packet_tokens": pkt_tokens,
                },
            )
            _atomic_write_json(hist_path, hist[:50])

            _atomic_write_json(
                os.path.join(dd, "last_auto_event.json"),
                {
                    "ts": now_i,
                    "from_conv": cid[:8] if cid else "manual",
                    "from_conv_full": cid,
                    "to_conv": new_id[:8],
                    "to_conv_full": new_id,
                    "archived_old": archived_old,
                    "delegated": delegated,
                    "at_tokens": cur_ctx,
                    "cap_k": cap_k,
                    "trigger": trigger,
                    "mode": "smart_auto" if kind == "auto" else "manual",
                    "ok": True,
                    "error": "",
                },
            )
        except Exception:
            pass

    return {
        "ok": True,
        "new_id": new_id,
        "title": new_title,
        "archived_old": archived_old,
        "est_input_saved": est_in_saved,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Execute Antigravity conversation handoff.")
    ap.add_argument("--pending", default="", help="Path to pending handoff JSON written by pre_invocation_handoff.py")
    ap.add_argument("--workspace", default="", help="Workspace directory (standalone mode)")
    ap.add_argument("--prompt", default="", help="Next user request to include (standalone mode)")
    ap.add_argument("--model", default="", help="Optional model override")
    ap.add_argument("--no-archive", action="store_true", help="Do not archive the source conversation")
    args = ap.parse_args()

    plugin_root = script_plugin_root(__file__)
    dd = data_dir(plugin_root)

    if args.pending:
        pfile = Path(args.pending)
        if not pfile.is_file():
            print("AUTO_HANDOFF_ERR: pending payload missing", file=sys.stderr)
            return 1
        try:
            payload = json.loads(pfile.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            print(f"AUTO_HANDOFF_ERR: invalid pending payload ({exc})", file=sys.stderr)
            return 1
        cid = str(payload.get("cid") or os.environ.get("ANTIGRAVITY_CONVERSATION_ID") or "")
        res = execute_handoff(
            cid=cid,
            ws=str(payload.get("ws") or os.getcwd()),
            full_prompt=str(payload.get("full_prompt") or ""),
            pkt_tokens=int(payload.get("pkt_tokens") or 0),
            user_input=str(payload.get("user_input") or ""),
            cur_ctx=int(payload.get("cur_ctx") or 0),
            turns=int(payload.get("turns") or 0),
            cap_k=int(payload.get("cap_k") or 110),
            trigger=str(payload.get("trigger") or "auto"),
            target_model=str(args.model or payload.get("target_model") or ""),
            auto_archive=bool(payload.get("auto_archive", True)) and not args.no_archive,
            kind="auto",
            delegated=bool(payload.get("delegated", True)),
            dd=dd,
        )
        try:
            pfile.unlink()
        except OSError:
            pass
    else:
        import handoff_pack

        cid = os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "").strip()
        ws = args.workspace or os.getcwd()
        ws_path = Path(ws).resolve() if os.path.isdir(ws) else Path.home()
        packet = handoff_pack.build_packet(ws_path, cid).rstrip()
        full_prompt = f"{packet}\n\n## Next User Request\n{args.prompt.strip()}\n" if args.prompt.strip() else packet + "\n"
        res = execute_handoff(
            cid=cid,
            ws=str(ws_path),
            full_prompt=full_prompt,
            pkt_tokens=len(packet) // 4,
            user_input=args.prompt.strip(),
            target_model=args.model,
            auto_archive=not args.no_archive,
            kind="manual",
            dd=dd,
        )

    if not res.get("ok"):
        print(f"AUTO_HANDOFF_ERR: {res.get('error', 'unknown error')}", file=sys.stderr)
        return 1

    new_id = str(res["new_id"])
    arch_note = " Old chat archived." if res.get("archived_old") else ""
    print(f"AUTO_HANDOFF_OK: **Continued in fresh chat -> [Open continued chat](conversation://{new_id})**.{arch_note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
