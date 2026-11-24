#!/usr/bin/env python3
"""
Compact session handoff packet generator for /handoff and agy-tokensaver.
Extracts workspace git state (one merged status+diffstat list), recent commits, files the
conversation edited and read, the recent goal, and the last assistant conclusion from the exact
target conversation transcript (IDE ~/.gemini/antigravity or CLI ~/.gemini/antigravity-cli) into a
dense (~300-600 token) continuation prompt. The packet is the real cold-start cost of a rollover,
so every line has to earn its tokens.

Read-only: git runs with --no-optional-locks (never rewrites the user's index), transcripts are only
read. The packet is written to <tmp>/agy-tokensaver-<uid>/handoff.md.
"""

from __future__ import annotations

import argparse
import ast
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

GEMINI_HOME = Path(os.environ.get("AGY_TS_GEMINI_HOME") or (Path.home() / ".gemini"))
APP_DIRS = (GEMINI_HOME / "antigravity", GEMINI_HOME / "antigravity-cli")
EDIT_TOOLS = ("edit_file", "write_file", "write_to_file", "replace_file_content", "multi_replace_file_content")
READ_TOOLS = ("read_file", "view_file")
_LINK = re.compile(r"\[([^\]]+)\]\((?:file|https?)://[^)\s]+\)")
MAX_CHANGED = 20
MAX_EDITED = 12
MAX_READ = 8


def run_git(ws: Path, args: list[str]) -> str:
    try:
        p = subprocess.run(["git", "--no-optional-locks", "-C", str(ws), *args],
                           text=True, capture_output=True, timeout=4, encoding="utf-8", errors="replace")
        return p.stdout.rstrip() if p.returncode == 0 else ""
    except Exception:
        return ""


def _clean_user_text(raw: str) -> str:
    m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", raw, re.S)
    txt = (m.group(1) if m else raw).strip()
    return " ".join(txt.split())


def resolve_transcript(conv_id: str = "") -> Path | None:
    cid = (conv_id or os.environ.get("ANTIGRAVITY_CONVERSATION_ID") or "").strip()
    if cid:
        for app in APP_DIRS:
            exact = app / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
            if exact.is_file():
                return exact
    txs = []
    for app in APP_DIRS:
        if not (app / "brain").is_dir():
            continue
        for p in (app / "brain").glob("*/.system_generated/logs/transcript.jsonl"):
            try:
                txs.append((p.stat().st_mtime, p))
            except OSError:
                continue
    if not txs:
        return None
    txs.sort(reverse=True)
    return txs[0][1]


def _tool_calls(raw: object) -> list[dict]:
    """Transcripts store tool_calls as a list OR as a Python-repr string; arg values are often
    JSON-encoded strings ('"C:\\\\x"')."""
    tc = raw
    if isinstance(tc, str):
        try:
            tc = ast.literal_eval(tc)
        except Exception:
            try:
                tc = json.loads(tc)
            except Exception:
                tc = []
    return [c for c in tc if isinstance(c, dict)] if isinstance(tc, list) else []


def _arg(args: object, *keys: str) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return ""
    if not isinstance(args, dict):
        return ""
    for k in keys:
        v = args.get(k)
        if v is None or v == "":
            continue
        s = str(v)
        if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
            try:
                s = str(json.loads(s))
            except Exception:
                s = s[1:-1]
        return s
    return ""


def extract_transcript_context(conv_id: str = "") -> tuple[str, str, list[str], str, list[str], list[str], str]:
    tx = resolve_transcript(conv_id)
    if tx is None:
        return "", "", [], "", [], [], ""

    resolved_cid = tx.parents[2].name
    last_user = ""
    last_assistant = ""
    last_verify = ""
    edited: dict[str, int] = {}
    read: dict[str, int] = {}
    errors: list[str] = []
    seen_err: set[str] = set()
    order = 0

    try:
        with tx.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"USER_INPUT"' in line:
                    try:
                        obj = json.loads(line)
                        if obj.get("type") == "USER_INPUT" and obj.get("content"):
                            c = _clean_user_text(str(obj["content"]))
                            if c and not c.startswith("/handoff") and not c.startswith("# Session Continuation Handoff"):
                                last_user = c[:400]
                    except Exception:
                        pass
                elif '"PLANNER_RESPONSE"' in line:
                    try:
                        obj = json.loads(line)
                        if obj.get("type") != "PLANNER_RESPONSE":
                            continue
                        ac = str(obj.get("content") or "").strip()
                        if ac:
                            ac = _LINK.sub(lambda m: m.group(1), ac)   # [x](file:///long/path) -> x
                            last_assistant = " ".join(ac.split())[:450]
                        for tc in _tool_calls(obj.get("tool_calls")):
                            name = str(tc.get("name") or "")
                            args = tc.get("args") or tc.get("arguments") or {}
                            order += 1
                            if name in EDIT_TOOLS:
                                fp = _arg(args, "TargetFile", "Path", "AbsolutePath", "path")
                                if fp:
                                    edited[fp] = order
                            elif name in READ_TOOLS:
                                fp = _arg(args, "AbsolutePath", "Path", "path")
                                if fp:
                                    read[fp] = order
                            elif name == "run_command":
                                cmdline = _arg(args, "CommandLine", "command")
                                if cmdline and any(k in cmdline for k in ("pytest", "cargo test", "npm test", "go test", "run_tests.sh", "test_")):
                                    last_verify = " ".join(cmdline.split())[:110]
                    except Exception:
                        pass
                elif any(tok in line for tok in ("Error:", "FAIL:", "AssertionError", "Command exited with code")):
                    try:
                        obj = json.loads(line)
                        c = str(obj.get("content") or "")
                        if "Command exited with code 0" in c:
                            continue
                        for el in c.splitlines():
                            el_s = el.strip()
                            if re.search(r"(?:Error:|FAIL:|AssertionError|Traceback|Exception:)", el_s) and len(el_s) > 10:
                                key = el_s[:110]
                                if key not in seen_err:
                                    seen_err.add(key)
                                    errors.append(key)
                                break
                    except Exception:
                        pass
    except OSError:
        pass
    ed = [p for p, _ in sorted(edited.items(), key=lambda kv: kv[1])][-MAX_EDITED:]
    ed_l = {p.lower() for p in ed}
    rd = [p for p, _ in sorted(read.items(), key=lambda kv: kv[1]) if p.lower() not in ed_l][-MAX_READ:]
    return last_user, last_assistant, ed, resolved_cid, rd, errors[-3:], last_verify


def _short(p: str, ws: Path) -> str:
    """Workspace-relative path when inside the workspace (saves tokens on every line)."""
    try:
        rp = Path(p).resolve()
        return rp.relative_to(ws).as_posix()
    except Exception:
        return p.replace("\\", "/")


def changed_files(ws: Path) -> list[str]:
    """One line per changed path: `XY path (+a/-b)` - merges `status --porcelain` and `diff --numstat`."""
    status = run_git(ws, ["status", "--porcelain", "--untracked-files=normal"])
    if not status:
        return []
    num: dict[str, str] = {}
    for line in (run_git(ws, ["diff", "HEAD", "--numstat"]) or run_git(ws, ["diff", "--numstat"])).splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            num[parts[2]] = f"+{parts[0]}/-{parts[1]}" if parts[0] != "-" else "binary"
    out = []
    lines = status.splitlines()
    for line in lines[:MAX_CHANGED]:
        code, path = line[:2].strip() or "?", line[3:].strip().strip('"')
        tail = path.split(" -> ")[-1]
        out.append(f"{code} {path}" + (f" ({num[tail]})" if tail in num else ""))
    if len(lines) > MAX_CHANGED:
        out.append(f"... +{len(lines) - MAX_CHANGED} more (git status)")
    return out


def _update_microskills(ws: Path, last_verify: str) -> list[str]:
    """TokenEconomizer (arXiv:2610.22732) cross-session per-repo verified command memory (max 3 cmds)."""
    dd = Path(os.environ.get("PLUGIN_DATA") or (Path.home() / ".gemini" / "antigravity" / "plugin_data" / "token-saver"))
    mf = dd / "microskills.json"
    key = str(ws)
    data: dict[str, dict[str, int]] = {}
    try:
        if mf.is_file():
            raw = json.loads(mf.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
    except Exception:
        data = {}
    repo_cmds = dict(data.get(key) or {})
    if last_verify and len(last_verify) <= 100 and not any(c in last_verify for c in ("\n", "\r", "`")):
        repo_cmds[last_verify] = int(repo_cmds.get(last_verify, 0)) + 1
        # Keep top 6 commands per repo
        top6 = dict(sorted(repo_cmds.items(), key=lambda kv: -kv[1])[:6])
        data[key] = top6
        try:
            dd.mkdir(parents=True, exist_ok=True)
            mf.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass
        repo_cmds = top6
    # Promote only commands confirmed >=2 times (prevents one-off noise)
    return [cmd for cmd, cnt in sorted(repo_cmds.items(), key=lambda kv: -kv[1]) if cnt >= 2][:3]


def _edited_symbols(edited: list[str], ws: Path, budget: int = 1200) -> list[str]:
    """Layer A deterministic artifact map (Factory.ai + Aider): compact L<start>-L<end> outlines for top edited files."""
    insp = Path(__file__).resolve().parent / "ts_inspect.py"
    if not insp.is_file():
        return []
    code_exts = {".py", ".ts", ".tsx", ".js", ".mjs", ".rs", ".go", ".pl", ".sh", ".java", ".kt", ".cs", ".cpp", ".c"}
    out: list[str] = []
    used = 0
    for p in reversed(edited):
        fp = Path(p)
        if not fp.is_file() or fp.suffix.lower() not in code_exts or fp.stat().st_size > 200_000:
            continue
        try:
            r = subprocess.run([sys.executable, str(insp), "outline", str(fp)], text=True, capture_output=True, timeout=2)
            if r.returncode != 0:
                continue
            syms = [ln.strip() for ln in r.stdout.splitlines()[1:9] if ln.strip()]
            if not syms:
                continue
            entry = f"- `{_short(p, ws)}`: " + "; ".join(syms[:6])
            if used + len(entry) > budget:
                break
            out.append(entry)
            used += len(entry)
            if len(out) >= 4:
                break
        except Exception:
            continue
    return out


def build_packet(ws: Path, conv_id: str = "") -> str:
    branch = run_git(ws, ["rev-parse", "--abbrev-ref", "HEAD"])
    commits = run_git(ws, ["log", "-n", "5", "--format=%h %s"])
    changed = changed_files(ws)
    last_user, last_assistant, edited, resolved_cid, read, errors, last_verify = extract_transcript_context(conv_id)

    lines = [
        "# Session Continuation Handoff",
        f"Workspace: `{ws}`" + (f" (branch `{branch}`)" if branch else "")
        + (f" - from conversation `{resolved_cid[:8]}`" if resolved_cid else ""),
    ]
    if last_user:
        lines.append(f"Goal: {last_user[:320]}")
    if last_assistant:
        lines.append(f"Last state: {last_assistant[:420]}")
    if commits:
        lines.append("Recent commits: " + "; ".join(c[:72] for c in commits.splitlines()[:5]))
    if last_verify:
        lines.append(f"Last verification: `{last_verify}`")
    micros = _update_microskills(ws, last_verify)
    if micros:
        lines.append("\n## Verified Repo Commands")
        lines.extend(f"- `{m}`" for m in micros)
    if errors:
        lines.append("\n## Recent errors / ruled-out")
        lines.extend(f"- {e}" for e in errors)
    if changed:
        lines.append("\n## Uncommitted changes")
        lines.extend(f"- {c}" for c in changed)
    if edited:
        lines.append("\n## Edited in that conversation")
        lines.extend(f"- {_short(p, ws)}" for p in edited)
        sym_lines = _edited_symbols(edited, ws)
        if sym_lines:
            lines.append("\n## Edited File Symbols")
            lines.extend(sym_lines)
    if read:
        lines.append("\n## Also consulted (re-read only if needed)")
        lines.append(", ".join(_short(p, ws) for p in read))
    lines.append("\nContinue from this state. Read only the exact line ranges you need from the files above before editing.")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a compact session continuation handoff packet.")
    ap.add_argument("workspace", nargs="?", default=".", help="Workspace directory path")
    ap.add_argument("--conversation-id", "-c", default="", help="Exact Antigravity conversation ID to pack")
    ap.add_argument("--no-save", action="store_true", help="Print only; do not write handoff.md")
    args = ap.parse_args()

    ws = Path(args.workspace).resolve()
    packet = build_packet(ws, args.conversation_id)
    print(packet)
    if args.no_save:
        return 0
    uid = str(os.getuid()) if hasattr(os, "getuid") else re.sub(r"[^A-Za-z0-9_-]", "_", getpass.getuser() or "user")
    out_dir = Path(tempfile.gettempdir()) / f"agy-tokensaver-{uid}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            os.chmod(out_dir, 0o700)
        except OSError:
            pass
    out_file = out_dir / "handoff.md"
    out_file.write_text(packet, encoding="utf-8")
    print(f"[Saved compact handoff (~{len(packet)//4} tokens) to {out_file}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
