"""Shared stdlib-only helpers for agy-tokensaver Python hooks (python3 -S safe)."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any

MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
ALLOW_JSON = '{"decision":"allow"}'
_ABS_RE = re.compile(r"^(?:/|[A-Za-z]:[/\\]|\\\\)")
_HOME_RE = re.compile(r"^~([/\\].*)?$")
_INT_STR_RE = re.compile(r"^-?\d+$")
_SAFE_CID_RE = re.compile(r"[^A-Za-z0-9_-]")
_OFF_RE = re.compile(r"^(?:0|off|false|disabled)$", re.I)
_MODE_OFF_RE = re.compile(r'"mode"\s*:\s*"off"', re.I)


def script_plugin_root(script_file: str) -> str:
    pr = os.environ.get("PLUGIN_ROOT", "")
    if pr:
        return pr
    return os.path.dirname(os.path.dirname(os.path.abspath(script_file)))


def data_dir(plugin_root: str) -> str:
    pd = os.environ.get("PLUGIN_DATA", "")
    if pd:
        return pd
    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
    if home:
        agy_pd = os.path.join(home, ".gemini", "antigravity", "plugin_data")
        if os.path.isdir(agy_pd):
            return os.path.join(agy_pd, "token-saver")
    return os.path.join(plugin_root, ".data")


def safe_cid(cid: str) -> str:
    return _SAFE_CID_RE.sub("_", cid)


def read_stdin_capped(max_bytes: int = MAX_PAYLOAD_BYTES) -> bytes | None:
    try:
        raw = sys.stdin.buffer.read(max_bytes + 1)
    except Exception:
        return None
    if not raw or len(raw) > max_bytes:
        return None
    return raw


def decode_hook_input(raw: bytes | None) -> dict[str, Any] | None:
    if not raw or len(raw) > MAX_PAYLOAD_BYTES:
        return None
    try:
        doc = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return doc if isinstance(doc, dict) else None


def decode_line(line: str | bytes) -> dict[str, Any] | None:
    if not line:
        return None
    try:
        txt = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
        doc = json.loads(txt)
    except Exception:
        return None
    return doc if isinstance(doc, dict) else None


def tool_input(doc: dict[str, Any]) -> dict[str, Any]:
    for k in ("toolInput", "tool_input"):
        v = doc.get(k)
        if isinstance(v, dict):
            return v
    tc = doc.get("toolCall")
    if isinstance(tc, dict) and isinstance(tc.get("args"), dict):
        return tc["args"]
    return {}


def get_str(h: dict[str, Any], *keys: str) -> str | None:
    if not isinstance(h, dict):
        return None
    for k in keys:
        v = h.get(k)
        if isinstance(v, str):
            return v
    return None


def get_int(h: dict[str, Any], *keys: str) -> int | None:
    if not isinstance(h, dict):
        return None
    for k in keys:
        if k not in h:
            continue
        v = h[k]
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            return v
        if isinstance(v, str) and _INT_STR_RE.match(v):
            try:
                return int(v)
            except Exception:
                continue
    return None


def get_bool(h: dict[str, Any], *keys: str) -> bool:
    if not isinstance(h, dict):
        return False
    for k in keys:
        if k in h and isinstance(h[k], bool):
            return bool(h[k])
    return False


def hooks_disabled(plugin_root: str) -> bool:
    env_v = os.environ.get("AGY_TS_HOOKS")
    if env_v is not None and _OFF_RE.match(env_v):
        return True
    cfg = os.path.join(data_dir(plugin_root), "hud_config.json")
    try:
        with open(cfg, "rb") as fh:
            buf = fh.read(4096).decode("utf-8", errors="replace")
        return bool(_MODE_OFF_RE.search(buf))
    except OSError:
        return False


def read_compact_reads(plugin_root: str) -> bool:
    env_v = os.environ.get("AGY_TS_COMPACT_READS")
    if env_v is not None:
        return bool(re.match(r"^(?:1|true|on|yes)$", env_v, re.I))
    cfg = os.path.join(data_dir(plugin_root), "hud_config.json")
    try:
        with open(cfg, "rb") as fh:
            buf = fh.read(4096).decode("utf-8", errors="replace")
        return bool(re.search(r'"compact_reads"\s*:\s*true\b', buf, re.I))
    except OSError:
        return False


def resolve_path(path: str, doc: dict[str, Any], ti: dict[str, Any]) -> str:
    m = _HOME_RE.match(path)
    if m:
        home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or ""
        if home:
            return home + (m.group(1) or "")
    elif not _ABS_RE.match(path):
        cwd = get_str(doc, "cwd") or get_str(ti, "cwd") or ""
        if cwd:
            return f"{cwd}/{path}"
    return path


def path_key(path: str) -> str:
    pk = path.replace("\\", "/")
    if os.name == "nt" or re.match(r"^(?:[A-Za-z]:|//)", pk):
        pk = pk.lower()
    return pk


def emit_json(obj: Any) -> None:
    try:
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except Exception:
        raw = json.dumps(obj, ensure_ascii=True, separators=(",", ":")).encode("utf-8", errors="replace")
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def emit_allow(overwrite: dict[str, Any] | None = None) -> None:
    if overwrite is None:
        sys.stdout.buffer.write(b'{"decision":"allow"}')
        sys.stdout.buffer.flush()
    else:
        emit_json({"decision": "allow", "overwrite": overwrite})


def emit_deny(reason: str) -> None:
    emit_json({"decision": "deny", "reason": reason})


def esc_sh(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def log_event(
    plugin_root: str,
    source: str,
    category: str,
    action: str,
    raw_bytes: int,
    kept_bytes: int,
    est_saved: int,
    target: str,
    session_id: str = "",
    file_bytes: int | None = None,
) -> None:
    try:
        dd = data_dir(plugin_root)
        os.makedirs(dd, exist_ok=True)
        rec: dict[str, Any] = {"ts": int(time.time())}
        if session_id or source == "read_guard":
            rec["session_id"] = session_id
        rec.update(
            {
                "source": source,
                "category": category,
                "action": action,
                "raw_bytes": int(raw_bytes),
                "kept_bytes": int(kept_bytes),
                "est_tokens_saved": int(est_saved),
            }
        )
        if file_bytes is not None:
            rec["file_bytes"] = int(file_bytes)
        rec["target"] = target
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(os.path.join(dd, "events.jsonl"), "ab") as fh:
            fh.write(line.encode("utf-8", errors="replace"))
    except Exception:
        pass


def check_loop_guard(doc: dict[str, Any], plugin_root: str, sig: str, deny_msg: str) -> str | None:
    cid = get_str(doc, "conversationId") or ""
    step = get_int(doc, "stepIdx")
    if not cid or step is None or not sig:
        return None
    try:
        dd = data_dir(plugin_root)
        rdir = os.path.join(dd, "reads")
        scid = safe_cid(cid)
        eseq = 0
        es_path = os.path.join(rdir, f"{scid}.editseq")
        if os.path.isfile(es_path):
            with open(es_path, "r", encoding="utf-8", errors="replace") as ef:
                eseq = int((ef.readline() or "0").strip() or "0")
        clean_sig = re.sub(r"[\r\n\t]+", " ", sig)
        lf = os.path.join(rdir, f"{scid}.loops")
        rows: list[str] = []
        cnt, last_b = 0, -999
        if os.path.isfile(lf):
            with open(lf, "r", encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    ln = ln.rstrip("\r\n")
                    parts = ln.split("\t", 3)
                    if len(parts) < 4:
                        continue
                    try:
                        st_n, eq = int(parts[0]), int(parts[1])
                    except ValueError:
                        continue
                    fl, s = parts[2], parts[3]
                    if step - st_n <= 40:
                        rows.append(ln)
                    if s == clean_sig and eq == eseq and step >= st_n and (step - st_n) <= 12:
                        if fl == "ok":
                            cnt += 1
                        elif fl == "blk" and st_n > last_b:
                            last_b = st_n
        os.makedirs(rdir, exist_ok=True)
        if cnt >= 2 and (step - last_b) > 2:
            rows.append(f"{step}\t{eseq}\tblk\t{clean_sig}")
            rows = rows[-24:]
            with open(lf, "w", encoding="utf-8") as wf:
                wf.write("\n".join(rows) + "\n")
            return deny_msg
        rows.append(f"{step}\t{eseq}\tok\t{clean_sig}")
        rows = rows[-24:]
        with open(lf, "w", encoding="utf-8") as wf:
            wf.write("\n".join(rows) + "\n")
    except Exception:
        return None
    return None
