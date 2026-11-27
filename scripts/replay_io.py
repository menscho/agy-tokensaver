"""Session discovery, protobuf/transcript parsing, VirtualFS, and prompt-overhead accounting."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / 'scripts'
GEMINI = Path(os.environ.get('AGY_TS_GEMINI_HOME') or (Path.home() / '.gemini'))
APP_DIRS = [GEMINI / 'antigravity', GEMINI / 'antigravity-cli']

# ----------------------------------------------------------------------------- token sources
def _rv(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if x < 0x80:
            return r, i


def _pb_fields(b: bytes) -> list[tuple[int, int, object]] | None:
    out: list[tuple[int, int, object]] = []
    i = 0
    try:
        while i < len(b):
            k, i = _rv(b, i)
            f, w = k >> 3, k & 7
            if f == 0:
                return None
            if w == 0:
                v, i = _rv(b, i)
                out.append((f, w, v))
            elif w == 1:
                i += 8
            elif w == 5:
                i += 4
            elif w == 2:
                ln, i = _rv(b, i)
                out.append((f, w, b[i:i + ln]))
                i += ln
            else:
                return None
        if i != len(b):
            return None
    except IndexError:
        return None
    return out


def db_turns(db: Path) -> list[tuple[int, int, int]]:
    """Per-generation (input, cache_read, output) from conversations/<id>.db gen_metadata."""
    res: list[tuple[int, int, int]] = []
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        rows = con.execute("select idx, data from gen_metadata order by idx").fetchall()
        con.close()
    except Exception:
        return res
    for _idx, data in rows:
        top = _pb_fields(bytes(data or b"")) or []
        usage: dict[int, int] = {}
        for f, w, v in top:
            if f == 1 and w == 2 and isinstance(v, bytes):
                for f2, w2, v2 in _pb_fields(v) or []:
                    if f2 == 4 and w2 == 2 and isinstance(v2, bytes):
                        for f3, w3, v3 in _pb_fields(v2) or []:
                            if w3 == 0 and isinstance(v3, int):
                                usage[f3] = int(v3)
                        break
                break
        inp, cr, out = usage.get(2, 0), usage.get(5, 0), usage.get(3, 0)
        if inp or cr or out:
            res.append((inp, cr, out))
    return res


# ----------------------------------------------------------------------------- transcript
def _tool_calls(o: dict) -> list[dict]:
    tc = o.get("tool_calls")
    if isinstance(tc, str):
        try:
            tc = ast.literal_eval(tc)
        except Exception:
            try:
                tc = json.loads(tc)
            except Exception:
                tc = []
    return [c for c in (tc or []) if isinstance(c, dict)]


def load_session(app: Path, sid: str) -> dict | None:
    logs = app / "brain" / sid / ".system_generated" / "logs"
    tx = logs / "transcript_full.jsonl"
    if not tx.is_file():
        tx = logs / "transcript.jsonl"
    recs: list[dict] = []
    if tx.is_file():
        with tx.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    recs.append(json.loads(line))
                except Exception:
                    pass
    planner = [r for r in recs if r.get("type") == "PLANNER_RESPONSE"]
    tx_tok = [(int(r.get("input_tokens") or 0), int(r.get("cache_read_tokens") or 0), int(r.get("output_tokens") or 0)) for r in planner]
    if any(a or b or c for a, b, c in tx_tok):
        turns = [t for t in tx_tok if any(t)]
        src = "transcript"
    else:
        turns = db_turns(app / "conversations" / f"{sid}.db")
        src = "db"
    if not turns:
        return None

    # Group tool results per planner turn: results following planner k feed turn k+1
    events: list[dict] = []   # {turn, name, args, content, type}
    checkpoints: list[int] = []
    k = -1
    pending: list[dict] = []
    for r in recs:
        t = r.get("type")
        if t == "PLANNER_RESPONSE":
            k += 1
            pending = _tool_calls(r)
            continue
        if t == "USER_INPUT":
            continue
        if t == "CHECKPOINT":
            checkpoints.append(max(k, 0))
            continue
        call = pending.pop(0) if pending else None
        if call is None:
            continue
        events.append({
            "turn": max(k, 0),
            "step": int(r.get("step_index") or 0),
            "name": call.get("name") or "",
            "args": call.get("args") or {},
            "content": str(r.get("content") or ""),
        })
    return {
        "id": sid,
        "app": app.name,
        "app_path": app,
        "src": src,
        "turns": turns,
        "n_planner": len(planner),
        "events": events,
        "checkpoints": checkpoints,
    }


def discover_sessions() -> list[dict]:
    out = []
    for app in APP_DIRS:
        ids: set[str] = set()
        if (app / "brain").is_dir():
            ids |= {p.name for p in (app / "brain").iterdir() if p.is_dir()}
        if (app / "conversations").is_dir():
            ids |= {p.stem for p in (app / "conversations").glob("*.db")}
        for sid in sorted(ids):
            s = load_session(app, sid)
            if s:
                out.append(s)
    return out


# ----------------------------------------------------------------------------- hook replay
def _b(s: str) -> int:
    return len(s.encode("utf-8"))


def _run_hook(script: str, payload: dict, env: dict, stdin_text: str | None = None, args: list[str] | None = None) -> tuple[int, str]:
    data = (json.dumps(payload) if stdin_text is None else stdin_text).encode("utf-8")
    p = subprocess.run([sys.executable, "-S", str(SCRIPTS / script), *(args or [])], input=data, capture_output=True, env=env, cwd=str(ROOT))
    return p.returncode, p.stdout.decode("utf-8", errors="replace")


NUM_LINE = re.compile(r"^(\d+): ?(.*)$")


class VirtualFS:
    """Reconstructs files seen through read_file/view_file results into a temp dir."""

    def __init__(self, base: Path):
        self.base = base
        self.files: dict[str, dict] = {}

    def vpath(self, real: str) -> Path:
        h = hashlib.sha1(real.lower().encode()).hexdigest()[:10]
        name = re.split(r"[\\/]", real.rstrip("\\/"))[-1] or "file"
        d = self.base / h
        d.mkdir(parents=True, exist_ok=True)
        return d / name

    @staticmethod
    def parse_read(content: str) -> dict:
        info: dict = {"lines": {}, "total_lines": None, "total_bytes": None, "binary": False}
        m = re.search(r"Total Lines: (\d+)", content)
        if m:
            info["total_lines"] = int(m.group(1))
        m = re.search(r"Total Bytes: (\d+)", content)
        if m:
            info["total_bytes"] = int(m.group(1))
        m = re.search(r"Partial read \(lines (\d+)-(\d+) of (\d+)\)\. Showing bytes \d+-\d+ of (\d+)", content)
        if m:
            info["total_lines"] = int(m.group(3))
            if m.group(1) == "1" and m.group(2) == m.group(3):
                info["total_bytes"] = int(m.group(4))
        m = re.search(r"Showing lines (\d+)\s*(?:-|to)\s*(\d+)(?: of (\d+))?", content)
        if m and m.group(3):
            info["total_lines"] = int(m.group(3))
        if "Showing binary file" in content:
            info["binary"] = True
        for ln in content.split("\n"):
            mm = NUM_LINE.match(ln)
            if mm:
                info["lines"][int(mm.group(1))] = mm.group(2)
        return info

    def materialize(self, real: str, info: dict, dirty: bool) -> Path:
        vp = self.vpath(real)
        st = self.files.get(real)
        tl = info["total_lines"] or (max(info["lines"]) if info["lines"] else 0)
        tb = info["total_bytes"]
        if st is not None and not dirty and (tb is None or st["bytes"] == tb) and (not tl or st["n"] == tl):
            st["known"].update(info["lines"])
            return vp
        known = dict(info["lines"])
        n = max(tl, max(known) if known else 0)
        kb = sum(_b(v) + 1 for v in known.values())
        unknown = max(0, n - len(known))
        if tb is None:
            avg = (kb / len(known)) if known else 40
            tb = int(kb + unknown * avg)
        fill = max(1, int((tb - kb) / unknown) - 1) if unknown else 0
        filler = ("x" * fill)
        body = "\n".join(known.get(i, filler) for i in range(1, n + 1)) + ("\n" if n else "")
        if info["binary"] and not body:
            body = "\x89PNG\r\n" + "\0" * 2048
        vp.write_bytes(body.encode("utf-8"))
        self.files[real] = {"n": n, "bytes": tb, "known": known, "avg": (tb / n) if n else 40}
        return vp

    def slice_bytes(self, real: str, s: int, e: int) -> int:
        st = self.files.get(real)
        if not st:
            return 0
        tot = 0
        for i in range(s, e + 1):
            v = st["known"].get(i)
            tot += (_b(f"{i}: {v}") + 1) if v is not None else int(st["avg"] + len(str(i)) + 2)
        return tot


def _frontmatter(txt: str) -> dict[str, str]:
    """Minimal YAML frontmatter reader: scalar keys + folded/literal (>-, |) multi-line values."""
    m = re.match(r"---\s*\r?\n(.*?)\r?\n---", txt, re.S)
    if not m:
        return {}
    out: dict[str, str] = {}
    key = None
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if km and not line.startswith((" ", "\t")):
            key, val = km.group(1), km.group(2).strip()
            out[key] = "" if val in (">", ">-", "|", "|-") else val.strip("'\"")
        elif key and line.strip():
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def plugin_overhead_bytes(include_opt_in_rules: bool | None = None) -> int:
    """Always-loaded plugin prompt cost, in the format Antigravity actually renders (decoded from
    the real system prompt in conversations/*.db):
      rules/AGENTS.md verbatim (only when include_opt_in_rules is True; Default mode has 0 rules)
      + per skill:    "- {name} ({install_dir}\\skills\\{dir}\\SKILL.md): {description}\n"
      + per subagent: "- {name}: {description}\n"   (model/tools fields are NOT in the prompt)
    """
    tot = 0
    if include_opt_in_rules is None:
        include_opt_in_rules = os.environ.get("AGY_TS_INCLUDE_RULES", "0") == "1"
    rules = ROOT / "rules" / "AGENTS.md"
    if include_opt_in_rules and rules.is_file():
        tot += rules.stat().st_size
    install_dir = str(Path.home() / ".gemini" / "config" / "plugins" / "token-saver")
    for md in sorted((ROOT / "skills").glob("*/SKILL.md")):
        try:
            fm = _frontmatter(md.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if fm.get("disable-model-invocation", "").lower() == "true":
            continue
        name = fm.get("name") or md.parent.name
        tot += _b(f"- {name} ({install_dir}\\skills\\{md.parent.name}\\SKILL.md): {fm.get('description', '')}\n")
    for md in sorted((ROOT / "agents").glob("*.md")):
        try:
            fm = _frontmatter(md.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        tot += _b(f"- {fm.get('name') or md.stem}: {fm.get('description', '')}\n")
    return tot


replay_ext: Any = None
try:  # optional replay handlers for additional tools (see scripts/replay_ext.py)
    import replay_ext as _replay_ext  # noqa: E402
    replay_ext = _replay_ext
except Exception:  # pragma: no cover
    replay_ext = None


def _cmd_output(content: str) -> tuple[int, str]:
    rc = 0
    m = re.search(r"(?:The command|Command) exited with code (-?\d+)", content)
    if m:
        rc = int(m.group(1))
    # IDE: "...exited with code N.\nOutput:\n<body>"   CLI: "...\nCommand output:\n<body>"
    m2 = re.search(r"(?:^|\n)(?:Command o|O)utput:\n", content)
    body = content[m2.end():] if m2 else ""
    return rc, body


