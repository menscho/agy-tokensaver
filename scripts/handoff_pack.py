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


def _find_git_root(ws: Path, paths: list[str]) -> Path | None:
    """Resolve the active git repository root from ws or, if ws is not a git repo, from recently touched files."""
    if run_git(ws, ["rev-parse", "--is-inside-work-tree"]) == "true":
        top = run_git(ws, ["rev-parse", "--show-toplevel"])
        return Path(top).resolve() if top else ws
    seen_dirs: set[Path] = set()
    for p in reversed(paths):
        try:
            fp = Path(p)
            d = fp if fp.is_dir() else fp.parent
            if not d.is_dir() or d in seen_dirs:
                continue
            seen_dirs.add(d)
            top = run_git(d, ["rev-parse", "--show-toplevel"])
            if top:
                return Path(top).resolve()
        except Exception:
            continue
    return None


def _full_transcript_last_assistant(tx: Path) -> str:
    """When compact transcript.jsonl truncated the last assistant response, recover full text from transcript_full.jsonl."""
    full_tx = tx.with_name("transcript_full.jsonl")
    if not full_tx.is_file():
        return ""
    last_ac = ""
    try:
        sz = full_tx.stat().st_size
        with full_tx.open("rb") as fh:
            if sz > 512 * 1024:
                fh.seek(sz - 512 * 1024)
                fh.readline()
            for raw in fh:
                if b'"PLANNER_RESPONSE"' not in raw:
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8", errors="replace"))
                except Exception:
                    continue
                if obj.get("type") == "PLANNER_RESPONSE":
                    ac = str(obj.get("content") or "").strip()
                    if ac:
                        last_ac = ac
    except OSError:
        return ""
    return last_ac


def _clean_assistant_summary(ac: str, max_chars: int = 2400) -> str:
    """Compact assistant markdown cleanly while keeping headings, bullets, and metrics intact."""
    ac = _LINK.sub(lambda m: m.group(1), ac).strip()
    if len(ac) <= 420 and "\n" not in ac:
        return " ".join(ac.split())
    out_lines: list[str] = []
    prev_blank = False
    for raw_ln in ac.splitlines():
        ln = raw_ln.rstrip()
        if not ln.strip():
            if not prev_blank and out_lines:
                out_lines.append("")
                prev_blank = True
            continue
        prev_blank = False
        out_lines.append(ln)
    cleaned = "\n".join(out_lines).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    # Preserve BOTH head (goal/setup) AND tail (final conclusions/metrics/tables) so the fresh chat
    # rarely needs to re-read the prior transcript to see how the last reply ended.
    head_lim = int(max_chars * 0.56)
    tail_lim = max_chars - head_lim - 32
    head_cut = cleaned[:head_lim].rsplit("\n", 1)[0].rstrip()
    if len(head_cut) < head_lim // 2:
        head_cut = cleaned[:head_lim].rstrip()
    tail_raw = cleaned[-tail_lim:]
    tail_nl = tail_raw.find("\n")
    tail_cut = tail_raw[tail_nl + 1 :].lstrip() if (0 <= tail_nl < tail_lim // 2) else tail_raw.lstrip()
    return f"{head_cut}\n... (middle omitted) ...\n{tail_cut}"


def _extract_artifacts_summary(tx: Path, budget: int = 1100) -> list[str]:
    """Extract active checklist items & headings from conversation artifacts (<brain>/<cid>/*.md)."""
    try:
        brain_conv = tx.parents[2]
    except IndexError:
        return []
    if not brain_conv.is_dir():
        return []
    md_files: list[tuple[float, Path]] = []
    try:
        for entry in brain_conv.iterdir():
            if entry.is_file() and entry.suffix.lower() == ".md" and not entry.name.startswith("."):
                try:
                    md_files.append((entry.stat().st_mtime, entry))
                except OSError:
                    continue
    except OSError:
        return []
    if not md_files:
        return []
    md_files.sort(reverse=True)
    out: list[str] = []
    used = 0
    for _mt, fp in md_files[:3]:
        try:
            txt = fp.read_text(encoding="utf-8", errors="replace")[:16384]
        except OSError:
            continue
        picks: list[str] = []
        for ln in txt.splitlines():
            s = ln.strip()
            if re.match(r"^[-*]\s+\[[ /xX]\]\s+", s) or re.match(r"^#{1,3}\s+", s):
                picks.append(s[:140])
            if len(picks) >= 12:
                break
        if not picks:
            continue
        block = f"- `{fp.name}`: " + " | ".join(picks[:8])
        if used + len(block) > budget:
            break
        out.append(block)
        used += len(block)
    return out


def _key_diff_hunks(ws: Path, budget: int = 1200) -> list[str]:
    """Compact structural diff map (`diff --git` + `@@ ... @@` headers) so the fresh chat sees edited functions."""
    raw = run_git(ws, ["diff", "HEAD", "-U0", "--no-color"]) or run_git(ws, ["diff", "-U0", "--no-color"])
    if not raw:
        return []
    out: list[str] = []
    cur_file = ""
    hunks: list[str] = []
    used = 0

    def flush() -> bool:
        nonlocal used
        if not cur_file or not hunks:
            return True
        entry = f"- `{cur_file}`: " + "; ".join(hunks[:5])
        if used + len(entry) > budget:
            return False
        out.append(entry)
        used += len(entry)
        return True

    for ln in raw.splitlines():
        if ln.startswith("diff --git "):
            if not flush():
                return out
            parts = ln.split(" b/", 1)
            cur_file = parts[1].strip() if len(parts) == 2 else ""
            hunks = []
        elif ln.startswith("@@ ") and cur_file:
            m = re.match(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+(?:,\d+)?)\s+@@\s*(.*)$", ln)
            if m:
                loc, ctx = m.group(1), m.group(2).strip()[:60]
                hunks.append(f"+L{loc}" + (f" ({ctx})" if ctx else ""))
    flush()
    return out


def extract_transcript_context(
    conv_id: str = "", exclude_prompt: str = ""
) -> dict[str, object]:
    tx = resolve_transcript(conv_id)
    empty: dict[str, object] = {
        "first_user": "",
        "last_user": "",
        "last_assistant": "",
        "edited": [],
        "resolved_cid": "",
        "read": [],
        "read_ranges": {},
        "errors": [],
        "last_verify": "",
        "plan_steps": [],
        "tx_path": None,
        "planner_turns": 0,
    }
    if tx is None:
        return empty

    resolved_cid = tx.parents[2].name
    excl_norm = " ".join(exclude_prompt.split()).strip().lower() if exclude_prompt else ""
    first_user = ""
    last_user = ""
    last_assistant = ""
    last_assistant_truncated = False
    last_verify = ""
    edited: dict[str, int] = {}
    read: dict[str, int] = {}
    read_ranges: dict[str, list[tuple[int | None, int | None]]] = {}
    errors: list[str] = []
    seen_err: set[str] = set()
    plan_steps: list[str] = []
    order = 0
    planner_turns = 0

    try:
        with tx.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"USER_INPUT"' in line:
                    try:
                        obj = json.loads(line)
                        if obj.get("type") == "USER_INPUT" and obj.get("content"):
                            c = _clean_user_text(str(obj["content"]))
                            if c and not c.startswith("/handoff") and not c.startswith("# Session Continuation Handoff"):
                                if excl_norm and c.lower() == excl_norm:
                                    continue
                                if not first_user:
                                    first_user = c[:360]
                                last_user = c[:400]
                    except Exception:
                        pass
                elif '"PLANNER_RESPONSE"' in line:
                    try:
                        obj = json.loads(line)
                        if obj.get("type") != "PLANNER_RESPONSE":
                            continue
                        planner_turns += 1
                        ac = str(obj.get("content") or "").strip()
                        if ac:
                            last_assistant = ac
                            tf = obj.get("truncated_fields") or []
                            last_assistant_truncated = isinstance(tf, list) and "content" in tf
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
                                    sl_s = _arg(args, "StartLine", "start_line")
                                    el_s = _arg(args, "EndLine", "end_line")
                                    sl = int(sl_s) if sl_s.isdigit() else None
                                    el = int(el_s) if el_s.isdigit() else None
                                    if sl is not None or el is not None:
                                        rr = read_ranges.setdefault(fp, [])
                                        pair = (sl, el)
                                        if pair not in rr:
                                            rr.append(pair)
                                            if len(rr) > 3:
                                                del rr[0]
                            elif name == "run_command":
                                cmdline = _arg(args, "CommandLine", "command")
                                if cmdline and any(k in cmdline for k in ("pytest", "cargo test", "npm test", "go test", "run_tests.sh", "run_all.sh", "test_")):
                                    last_verify = " ".join(cmdline.split())[:110]
                            elif name == "update_steps":
                                raw_steps = args.get("steps") if isinstance(args, dict) else None
                                if isinstance(raw_steps, list):
                                    parsed_steps = []
                                    for st in raw_steps[:10]:
                                        if isinstance(st, dict):
                                            desc = str(st.get("description") or st.get("step") or st.get("title") or "").strip()
                                            stat = str(st.get("status") or "").strip()
                                            if desc:
                                                parsed_steps.append(f"[{stat}] {desc[:100]}" if stat else desc[:100])
                                    if parsed_steps:
                                        plan_steps = parsed_steps
                    except Exception:
                        pass
                elif "Command exited with code " in line and "Command exited with code 0" not in line:
                    try:
                        obj = json.loads(line)
                        c = str(obj.get("content") or "")
                        if "Command exited with code 0" in c:
                            continue
                        for el in c.splitlines():
                            el_s = el.strip()
                            # Skip source-code line-number echoes like `124: print("FAIL: ...")`
                            if re.match(r"^\d+:\s", el_s):
                                continue
                            if re.search(r"(?:Error:|FAIL:|AssertionError|Traceback|Exception:|SyntaxError|TypeError)", el_s) and len(el_s) > 10:
                                key = el_s[:120]
                                if key not in seen_err:
                                    seen_err.add(key)
                                    errors.append(key)
                                break
                    except Exception:
                        pass
    except OSError:
        pass

    if last_assistant_truncated:
        recovered = _full_transcript_last_assistant(tx)
        if len(recovered) > len(last_assistant):
            last_assistant = recovered

    ed = [p for p, _ in sorted(edited.items(), key=lambda kv: kv[1])][-MAX_EDITED:]
    ed_l = {p.lower() for p in ed}
    rd = [p for p, _ in sorted(read.items(), key=lambda kv: kv[1]) if p.lower() not in ed_l][-MAX_READ:]
    return {
        "first_user": first_user,
        "last_user": last_user,
        "last_assistant": _clean_assistant_summary(last_assistant) if last_assistant else "",
        "edited": ed,
        "resolved_cid": resolved_cid,
        "read": rd,
        "read_ranges": read_ranges,
        "errors": errors[-4:],
        "last_verify": last_verify,
        "plan_steps": plan_steps,
        "tx_path": tx,
        "planner_turns": planner_turns,
    }


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


def _edited_symbols(edited: list[str], ws: Path, budget: int = 2200) -> list[str]:
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
            syms = [ln.strip() for ln in r.stdout.splitlines()[1:10] if ln.strip()]
            if not syms:
                continue
            entry = f"- `{_short(p, ws)}`: " + "; ".join(syms[:7])
            if used + len(entry) > budget:
                break
            out.append(entry)
            used += len(entry)
            if len(out) >= 6:
                break
        except Exception:
            continue
    return out


def _format_read_entry(p: str, ws: Path, read_ranges: dict[str, list[tuple[int | None, int | None]]]) -> str:
    sp = _short(p, ws)
    rr = read_ranges.get(p) or []
    if not rr:
        return sp
    spans = []
    for sl, el in rr:
        if sl is not None and el is not None:
            spans.append(f"L{sl}-L{el}")
        elif sl is not None:
            spans.append(f"L{sl}+")
        elif el is not None:
            spans.append(f"L1-L{el}")
    return f"{sp} ({', '.join(spans)})" if spans else sp


def build_packet(ws: Path, conv_id: str = "", exclude_prompt: str = "", max_tokens: int = 3500) -> str:
    ctx = extract_transcript_context(conv_id, exclude_prompt=exclude_prompt)
    first_user = str(ctx["first_user"])
    last_user = str(ctx["last_user"])
    last_assistant = str(ctx["last_assistant"])
    edited = list(ctx["edited"]) if isinstance(ctx["edited"], list) else []
    resolved_cid = str(ctx["resolved_cid"])
    read = list(ctx["read"]) if isinstance(ctx["read"], list) else []
    read_ranges = ctx["read_ranges"] if isinstance(ctx["read_ranges"], dict) else {}
    errors = list(ctx["errors"]) if isinstance(ctx["errors"], list) else []
    last_verify = str(ctx["last_verify"])
    plan_steps = list(ctx["plan_steps"]) if isinstance(ctx["plan_steps"], list) else []
    tx_path = ctx["tx_path"] if isinstance(ctx["tx_path"], Path) else None
    planner_turns = int(ctx["planner_turns"]) if isinstance(ctx["planner_turns"], int) else 0

    git_ws = _find_git_root(ws, edited + read) or ws
    display_ws = git_ws if (ws.resolve() == Path.home().resolve() and git_ws != ws) else ws

    branch = run_git(git_ws, ["rev-parse", "--abbrev-ref", "HEAD"])
    commits = run_git(git_ws, ["log", "-n", "5", "--format=%h %s"])
    changed = changed_files(git_ws)

    lines = [
        "# Session Continuation Handoff",
        f"Workspace: `{display_ws}`" + (f" (branch `{branch}`)" if branch else "")
        + (f" - from conversation `{resolved_cid[:8]}`" if resolved_cid else ""),
    ]
    if git_ws != display_ws and branch:
        lines.append(f"Git repo: `{git_ws}`")
    if first_user and last_user and first_user.lower() != last_user.lower():
        lines.append(f"Original goal: {first_user[:320]}")
    if last_user:
        lines.append(f"Goal: {last_user[:360]}")
    if last_assistant:
        if "\n" in last_assistant:
            lines.append("\n## Last Assistant State\n" + last_assistant)
        else:
            lines.append(f"Last state: {last_assistant}")
    if commits:
        lines.append("Recent commits: " + "; ".join(c[:72] for c in commits.splitlines()[:5]))
    if last_verify:
        lines.append(f"Last verification: `{last_verify}`")
    micros = _update_microskills(git_ws, last_verify)
    if micros:
        lines.append("\n## Verified Repo Commands")
        lines.extend(f"- `{m}`" for m in micros)

    if plan_steps:
        lines.append("\n## Active Plan Steps")
        lines.extend(f"- {s}" for s in plan_steps[:10])
    if tx_path is not None:
        arts = _extract_artifacts_summary(tx_path)
        if arts:
            lines.append("\n## Active Artifacts")
            lines.extend(arts)

    if errors:
        lines.append("\n## Recent errors / ruled-out")
        lines.extend(f"- {e}" for e in errors)
    if changed:
        lines.append("\n## Uncommitted changes")
        lines.extend(f"- {c}" for c in changed)
        if planner_turns >= 5 or len(changed) >= 3:
            hunk_lines = _key_diff_hunks(git_ws)
            if hunk_lines:
                lines.append("\n## Key Uncommitted Diff Hunks")
                lines.extend(hunk_lines)
    if edited:
        lines.append("\n## Edited in that conversation")
        lines.extend(f"- {_short(p, display_ws)}" for p in edited)
        sym_lines = _edited_symbols(edited, display_ws)
        if sym_lines:
            lines.append("\n## Edited File Symbols")
            lines.extend(sym_lines)
    if read:
        lines.append("\n## Also consulted (re-read only if needed)")
        lines.append(", ".join(_format_read_entry(p, display_ws, read_ranges) for p in read))
    if tx_path is not None and planner_turns >= 5:
        lines.append(f"\nPrior transcript (only if deeper history needed): `{tx_path}`")
    lines.append("\nContinue from this state. Read only the exact line ranges you need from the files above before editing.")
    packet = "\n".join(lines) + "\n"
    max_chars = max(1600, int(max_tokens) * 4)
    if len(packet) > max_chars:
        packet = packet[: max_chars - 64].rsplit("\n", 1)[0] + "\n... (handoff trimmed to token budget)\n"
    return packet


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a compact session continuation handoff packet.")
    ap.add_argument("workspace", nargs="?", default=".", help="Workspace directory path")
    ap.add_argument("--conversation-id", "-c", default="", help="Exact Antigravity conversation ID to pack")
    ap.add_argument("--exclude-prompt", default="", help="Optional next user prompt to avoid duplicating in Goal")
    ap.add_argument("--max-tokens", type=int, default=3500, help="Hard upper token budget for handoff packet")
    ap.add_argument("--no-save", action="store_true", help="Print only; do not write handoff.md")
    args = ap.parse_args()

    ws = Path(args.workspace).resolve()
    packet = build_packet(ws, args.conversation_id, exclude_prompt=args.exclude_prompt, max_tokens=args.max_tokens)
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
