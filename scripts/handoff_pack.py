#!/usr/bin/env python3
"""
Compact session handoff packet generator for /handoff and agy-tokensaver.
Extracts workspace git state, recent commits, touched files, recent goal, and
the last assistant conclusion from the exact target conversation transcript
into a compact (~400-700 token) continuation prompt.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def run_git(ws: Path, args: list[str]) -> str:
    try:
        p = subprocess.run(["git", "-C", str(ws), *args], text=True, capture_output=True, timeout=4)
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def _clean_user_text(raw: str) -> str:
    m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", raw, re.S)
    txt = (m.group(1) if m else raw).strip()
    return " ".join(txt.split())


def resolve_transcript(conv_id: str = "") -> Path | None:
    brain = Path.home() / ".gemini" / "antigravity" / "brain"
    if not brain.is_dir():
        return None
    cid = (conv_id or os.environ.get("ANTIGRAVITY_CONVERSATION_ID") or "").strip()
    if cid:
        exact = brain / cid / ".system_generated" / "logs" / "transcript.jsonl"
        if exact.is_file():
            return exact
    txs = []
    for p in brain.glob("*/.system_generated/logs/transcript.jsonl"):
        try:
            txs.append((p.stat().st_mtime, p))
        except OSError:
            continue
    if not txs:
        return None
    txs.sort(reverse=True)
    return txs[0][1]


def extract_transcript_context(conv_id: str = "") -> tuple[str, str, list[str], str]:
    tx = resolve_transcript(conv_id)
    if tx is None:
        return "", "", [], ""

    resolved_cid = tx.parents[2].name
    last_user = ""
    last_assistant = ""
    touched: list[str] = []
    seen: set[str] = set()

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
                            last_assistant = " ".join(ac.split())[:450]
                        for tc in obj.get("tool_calls") or []:
                            if not isinstance(tc, dict):
                                continue
                            name = tc.get("name", "")
                            if name in ("edit_file", "write_file", "write_to_file"):
                                args = tc.get("args") or tc.get("arguments") or {}
                                if isinstance(args, str):
                                    try:
                                        args = json.loads(args)
                                    except Exception:
                                        args = {}
                                fp = args.get("Path") or args.get("TargetFile") or ""
                                if fp and fp not in seen:
                                    seen.add(fp)
                                    touched.append(fp)
                    except Exception:
                        pass
    except OSError:
        pass
    return last_user, last_assistant, touched[-15:], resolved_cid


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a compact session continuation handoff packet.")
    ap.add_argument("workspace", nargs="?", default=".", help="Workspace directory path")
    ap.add_argument("--conversation-id", "-c", default="", help="Exact Antigravity conversation ID to pack")
    args = ap.parse_args()

    ws = Path(args.workspace).resolve()
    branch = run_git(ws, ["rev-parse", "--abbrev-ref", "HEAD"])
    status = run_git(ws, ["status", "--short"])
    diff_stat = run_git(ws, ["diff", "--stat"])
    commits = run_git(ws, ["log", "-n", "5", "--oneline"])
    last_user, last_assistant, touched, resolved_cid = extract_transcript_context(args.conversation_id)

    lines = [
        "# Session Continuation Handoff",
        f"- **Workspace:** `{ws}`" + (f" (branch `{branch}`)" if branch else ""),
    ]
    if resolved_cid:
        lines.append(f"- **Source Conversation:** `{resolved_cid[:8]}`")
    if last_user:
        lines.append(f"- **Last Goal:** {last_user[:320]}")
    if last_assistant:
        lines.append(f"- **Last Assistant State:** {last_assistant[:420]}")
    if commits:
        lines.append("\n## Recent Commits")
        lines.extend(f"- `{c}`" for c in commits.splitlines()[:5])
    if status:
        lines.append("\n## Working Tree (`git status --short`)")
        lines.extend(f"- `{s}`" for s in status.splitlines()[:25])
    if diff_stat:
        lines.append("\n## Uncommitted Diff Stat")
        lines.append("```text")
        lines.extend(diff_stat.splitlines()[:25])
        lines.append("```")
    if touched:
        lines.append("\n## Recently Edited Files (from transcript)")
        lines.extend(f"- `{p}`" for p in touched)

    lines.append("\n## Instructions for New Session")
    lines.append("Continue from the state above. Read only the exact line slices needed from the files listed above before making further edits.")

    packet = "\n".join(lines) + "\n"
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

    print(packet)
    print(f"[Saved compact handoff (~{len(packet)//4} tokens) to {out_file}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
