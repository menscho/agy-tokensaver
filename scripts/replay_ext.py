#!/usr/bin/env python3
"""
Replay handlers for tools that transcript_replay.py does not cover itself.

transcript_replay.py calls, for every recorded tool call it has no built-in handler for:

    handle(name, args, content, env=..., sid=..., step=..., tmp=...)
        -> (saved_bytes, tag, retry_count) | None

Each handler runs the REAL hook script of this checkout on the recorded arguments and computes the
post-hook tool result size from the recorded content (never from a guess about the model):

  grep_search  -> grep_hook.py. When it rewrites a single-file SearchPath to <dir> + Includes, the
                  recorded "No results found" would have been the real matches. The post-hook
                  result is rebuilt by running the same search over the file on disk (Antigravity's
                  JSON-lines format) plus the overwrite notice, so this category is usually NEGATIVE
                  bytes (the model now gets data). The turns the model spent on fallbacks
                  (view_file / Select-String / python one-liners) are NOT credited.
  find_by_name -> find_hook.py. When it injects Excludes, recorded result lines under an excluded
                  directory are dropped and the overwrite notice is charged.

Tools with no hook (webview_browser, list_dir, manage_task, search_web, ...) return None.
"""

from __future__ import annotations

import collections
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
NOTICE_BYTES = 94          # "A pre-tool hook changed the arguments ..." notice (one key)
NOTICE_KEY_BYTES = 12      # each additional rewritten key named in the notice

STATS: collections.Counter = collections.Counter()   # for ad-hoc analysis scripts


def _b(s: str) -> int:
    return len(s.encode("utf-8"))


def _run_hook(script: str, payload: dict, env: dict | None) -> dict | None:
    try:
        p = subprocess.run([sys.executable, "-S", str(SCRIPTS / script)], input=json.dumps(payload).encode("utf-8"),
                           capture_output=True, env=env, cwd=str(ROOT), timeout=20)
        return json.loads(p.stdout.decode("utf-8", errors="replace"))
    except Exception:
        return None


def _notice(ov: dict) -> int:
    return NOTICE_BYTES + NOTICE_KEY_BYTES * max(0, len(ov) - 1)


def _header(content: str) -> str:
    """Leading 'Created At:' / 'Completed At:' lines of a recorded tool result."""
    out = []
    for ln in content.split("\n"):
        if ln.startswith(("Created At:", "Completed At:")):
            out.append(ln)
        else:
            break
    return "\n".join(out) + ("\n" if out else "")


# ----------------------------------------------------------------------------- grep_search
def _go_json(obj: dict) -> str:
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def grep_file(path: str, args: dict) -> str | None:
    """Antigravity-style grep_search output for one file (None if the file can't be read)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    q = str(args.get("Query") or "")
    if not q:
        return None
    ci = bool(args.get("CaseInsensitive"))
    if args.get("IsRegex"):
        try:
            rx = re.compile(q, re.I if ci else 0)
        except re.error:
            return None
        hit = lambda ln: rx.search(ln) is not None  # noqa: E731
    else:
        qq = q.lower() if ci else q
        hit = (lambda ln: qq in ln.lower()) if ci else (lambda ln: qq in ln)  # noqa: E731
    win = path.replace("/", "\\")
    lines = text.split("\n")
    rows = [i for i, ln in enumerate(lines, 1) if hit(ln.rstrip("\r"))]
    if not rows:
        return "No results found"
    if args.get("MatchPerLine") is False:
        return _go_json({"File": win})
    return "\n".join(_go_json({"File": win, "LineNumber": i, "LineContent": lines[i - 1].rstrip("\r")}) for i in rows)


def _grep(args: dict, content: str, env: dict | None):
    res = _run_hook("grep_hook.py", {"toolCall": {"name": "grep_search", "args": args}}, env)
    ov = (res or {}).get("overwrite") or {}
    if not ov.get("SearchPath"):
        return None
    body = grep_file(str(args.get("SearchPath") or ""), args)
    if body is None:
        return (0, "grep:file_fix", 0)
    STATS["grep_fix_calls"] += 1
    if "No results found" in content and body != "No results found":
        STATS["grep_false_negatives_fixed"] += 1
    after = _b(_header(content) + body) + _notice(ov)
    return (_b(content) - after, "grep:file_fix", 0)


# ----------------------------------------------------------------------------- find_by_name
def _find(args: dict, content: str, env: dict | None):
    ti = {k: (str(v) if not isinstance(v, (int, float, list)) else v) for k, v in args.items()}
    res = _run_hook("find_hook.py", {"toolInput": ti}, env)
    if not res:
        return None
    if res.get("decision") == "deny":
        return (_b(content) - _b(res.get("reason") or ""), "find:block", 1)
    ex = set((res.get("overwrite") or {}).get("Excludes") or [])
    if not ex:
        return None
    hdr = _header(content)
    rest = content[len(hdr):].split("\n")
    kept, dropped = [], 0
    for ln in rest:
        if ln.startswith("Found ") and ln.endswith(" results"):
            continue
        if ln and any(part in ex for part in re.split(r"[\\/]", ln)):
            dropped += 1
            continue
        kept.append(ln)
    body = "\n".join(kept)
    m = re.search(r"^Found (\d+) results$", content, re.M)
    if m:
        body = f"Found {int(m.group(1)) - dropped} results\n" + body
    after = _b(hdr + body) + _notice(res["overwrite"])
    return (_b(content) - after, "find:excludes", 0)


def handle(name: str, args: dict, content: str, env: dict | None = None, sid: str | None = None,
           step: int | None = None, tmp: Path | None = None):
    if not isinstance(args, dict):
        return None
    if name == "grep_search":
        return _grep(args, content, env)
    if name == "find_by_name":
        return _find(args, content, env)
    return None
