"""Sub-50ms SessionStart workspace snapshot builder (`<= 1,500` UTF-8 bytes).

Eliminates cold-start exploration turns by injecting git state (branch,
porcelain status, recent commits), project manifests across root + subprojects
(package.json, Makefile, pom.xml, build.gradle, pyproject.toml, Cargo.toml,
go.mod), and a priority-sorted 2-level directory layout once per session.

Hardened with a 250ms scan circuit-breaker, 800ms git timeout, OS/home-dir skip
list (1-level listing on `$HOME` or filesystem roots), and nested repo support.

Incorporates engineering patterns adapted from INK-Q/agy-fast (MIT License).
"""

from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fast_utils import (
    DEFAULT_IGNORE_DIRS,
    exec_binary,
    slice_safe_chars,
    slice_utf8_bytes,
    truncate_with_byte_notice,
    utf8_len,
    with_non_interactive_git_env,
)

DEFAULT_SCAN_TIMEOUT_MS = 250
MAX_SNAPSHOT_BYTES = 1500

_HARDENED_GIT_ARGS = [
    "--no-optional-locks",
    "-c", "core.fsmonitor=false",
    "-c", "core.quotePath=false",
    "-c", "diff.external=",
    "-c", "gpg.program=false",
    "-c", "gpg.openpgp.program=false",
    "-c", "gpg.ssh.program=false",
    "-c", "gpg.x509.program=false",
    "--no-pager",
]

_COLLAPSE_DIRS = frozenset(
    {"tmp", "temp", "output", "outputs", "artifacts", "ssl", "playwright-report", "test-results", "coverage", "logs"}
)
_PRIORITY_SUBDIRS = frozenset(
    {"src", "app", "lib", "packages", "server", "web", "client", "api", "tests", "test", "scripts"}
)
_ALWAYS_SKIP_DIRS = frozenset(
    {
        "appdata", "application data", "application support", "local settings",
        "program files", "program files (x86)", "programdata", "$recycle.bin",
        "system volume information", "onedrive", "icloud drive", "mobile documents",
        "creative cloud files", "msocache", "perflogs", "system32", "syswow64", "winsxs",
    }
)
_HOME_ROOT_SKIP_DIRS = frozenset(
    {
        "desktop", "documents", "downloads", "pictures", "photos", "movies", "videos",
        "music", "library", "applications", "public", "favorites", "contacts", "links",
        "saved games", "searches", "3d objects", "my documents", "my music", "my pictures",
        "my videos", "nethood", "printhood", "recent", "sendto", "start menu", "templates",
        "cookies", "documents and settings", "all users", "default user", "recovery",
        "windows", "users", "intel", "dropbox", "google drive", "parallels",
        "virtualbox vms", "trash", "proc", "sys", "dev", "mnt", "media", "run",
        "volumes", "private", "system",
    }
)
_IGNORE_DIRS_LOWER = frozenset(d.lower() for d in DEFAULT_IGNORE_DIRS)
_SKIP_FILES = frozenset({".ds_store", "thumbs.db", "desktop.ini", "ntuser.dat", "ntuser.ini"})
_MANIFEST_SKIP_SUBDIRS = frozenset(
    {"docs", "doc", "assets", "scripts", "test", "tests", "fixtures", "examples", *_COLLAPSE_DIRS}
)


def _find_git_entry(workspace_dir: str) -> str | None:
    cur = os.path.abspath(workspace_dir)
    while True:
        cand = os.path.join(cur, ".git")
        if os.path.lexists(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _read_branch_from_git_entry(git_entry: str | None) -> str | None:
    if not git_entry:
        return None
    try:
        git_dir = git_entry
        if os.path.isfile(git_entry):
            with open(git_entry, "r", encoding="utf-8", errors="replace") as fh:
                m = re.match(r"^gitdir:\s*(.+)$", fh.read().strip(), re.I | re.M)
            if not m:
                return None
            git_dir = os.path.abspath(os.path.join(os.path.dirname(git_entry), m.group(1).strip()))
        with open(os.path.join(git_dir, "HEAD"), "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read().strip()
        rm = re.match(r"^ref:\s*refs/heads/(.+)$", raw)
        return rm.group(1).strip() if rm else "HEAD"
    except OSError:
        return None


def parse_porcelain_branch_header(header_line: str) -> tuple[str, bool]:
    body = header_line[3:].strip()
    m = re.match(r"^(?:No commits yet|Initial commit) on (.+)$", body)
    if m:
        return (m.group(1).split("...", 1)[0].strip() or "HEAD"), True
    if re.match(r"^HEAD\s*\(no branch\)$", body, re.I):
        return "HEAD", False
    return (body.split("...", 1)[0].strip() or "HEAD"), False


def _to_workspace_relative_status_line(line: str, depth: int) -> str | None:
    if depth <= 0 or len(line) < 4:
        return line
    contains_ws = False

    def _strip(p: str) -> str:
        nonlocal contains_ws
        quoted = p.startswith('"')
        segs = (p[1:] if quoted else p).split("/")
        last = segs[-1] if segs else ""
        is_dir_entry = last in ("", '"')
        name_cnt = len(segs) - 1 if is_dir_entry else len(segs)
        if name_cnt <= depth:
            contains_ws = is_dir_entry
            return p
        return ('"' if quoted else "") + "/".join(segs[depth:])

    code = line[:2]
    raw_body = line[3:]
    if code[0] in ("R", "C") and " -> " in raw_body:
        rest = " -> ".join(_strip(p) for p in raw_body.split(" -> "))
    else:
        rest = _strip(raw_body)
    if contains_ws and code in ("??", "!!"):
        return None
    return f"{line[:3]}{rest}"


def _get_git_summary(workspace_dir: str, timeout_ms: int) -> str | None:
    has_env = bool(os.environ.get("GIT_DIR") or os.environ.get("GIT_WORK_TREE"))
    git_entry = None if has_env else _find_git_entry(workspace_dir)
    if not has_env and not git_entry:
        return None

    nesting_depth = 0
    eff_entry = git_entry
    if git_entry and os.path.dirname(git_entry) != os.path.abspath(workspace_dir):
        try:
            real_ws = os.path.realpath(workspace_dir)
            real_entry = _find_git_entry(real_ws) or git_entry
            rel = os.path.relpath(real_ws, os.path.dirname(real_entry))
            if rel and not rel.startswith("..") and not os.path.isabs(rel) and rel != ".":
                nesting_depth = len([s for s in rel.split(os.sep) if s])
                eff_entry = real_entry
        except (OSError, ValueError):
            pass

    is_nested = nesting_depth > 0
    git_env = with_non_interactive_git_env()
    status_args = [*_HARDENED_GIT_ARGS, "status", "--porcelain=v1", "--branch", *(["--", "."] if is_nested else [])]
    log_args = [*_HARDENED_GIT_ARGS, "log", "-n", "3", "--format=%h %s"]

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_status = pool.submit(exec_binary, "git", status_args, workspace_dir, timeout_ms, 32_768, git_env)
        f_log = pool.submit(exec_binary, "git", log_args, workspace_dir, timeout_ms, 8192, git_env)
        f_head = (
            pool.submit(
                exec_binary,
                "git",
                [*_HARDENED_GIT_ARGS, "rev-parse", "--verify", "--quiet", "HEAD:./"],
                workspace_dir,
                timeout_ms,
                256,
                git_env,
            )
            if is_nested
            else None
        )
        status_res = f_status.result()
        log_res = f_log.result()
        head_res = f_head.result() if f_head else None

    status_ok = not status_res.error and not status_res.timed_out and status_res.exit_code == 0
    log_ok = not log_res.error and not log_res.timed_out and log_res.exit_code == 0
    head_missing = bool(head_res and not head_res.error and not head_res.timed_out and head_res.exit_code != 0)
    fallback_br = _read_branch_from_git_entry(eff_entry) if not status_ok else None

    if not status_ok and not log_ok and not (status_res.timed_out and fallback_br):
        return None
    if is_nested and not status_ok and head_missing:
        return None

    branch = "HEAD"
    is_unborn = False
    status_lines: list[str] = []
    if status_ok:
        raw_lines = re.split(r"\r?\n", status_res.stdout)
        if status_res.truncated and len(raw_lines) > 1:
            raw_lines.pop()
        hdr_idx = next((i for i, ln in enumerate(raw_lines) if ln.startswith("## ")), -1)
        if hdr_idx != -1:
            branch, is_unborn = parse_porcelain_branch_header(raw_lines.pop(hdr_idx))
        else:
            branch = _read_branch_from_git_entry(eff_entry) or "HEAD"
        if is_nested:
            rel_lines = [_to_workspace_relative_status_line(ln, nesting_depth) for ln in raw_lines]
            ws_untracked = any(ln is None for ln in rel_lines)
            has_tracked = any(ln is not None and ln.strip() and not re.match(r"^(?:\?\?|!!) ", ln) for ln in rel_lines)
            if (head_missing or ws_untracked) and not has_tracked:
                return None
            raw_lines = [ln for ln in rel_lines if ln is not None]
        for ln in raw_lines:
            t = ln.strip()
            if not t:
                continue
            if t.startswith("?? "):
                top_seg = t[3:].lstrip('"').split("/", 1)[0].rstrip("/")
                if top_seg in DEFAULT_IGNORE_DIRS:
                    continue
            status_lines.append(t)
    else:
        branch = fallback_br or "HEAD"

    log_lines = [ln.strip() for ln in re.split(r"\r?\n", log_res.stdout) if ln.strip()] if log_ok else []
    notes: list[str] = []
    if is_unborn:
        notes.append("unborn, no commits yet")
    if is_nested:
        notes.append(f"repo root: {'/'.join(['..'] * nesting_depth)}")
    parts = [f"Git: branch `{branch}`" + (f" ({'; '.join(notes)})" if notes else "")]
    if not status_ok:
        parts.append("Working tree: unknown (git status timed out)" if status_res.timed_out else "Working tree: unknown (git status failed)")
    elif not status_lines:
        parts.append("Working tree: clean")
    else:
        cnt_lbl = f"{len(status_lines)}+" if status_res.truncated else str(len(status_lines))
        prefix = f"Modified ({cnt_lbl}): "
        shown: list[str] = []
        cur_len = len(prefix)
        for i, raw in enumerate(status_lines[:8]):
            item = f"{slice_safe_chars(raw, 97)}..." if len(raw) > 100 else raw
            sep_len = 2 if shown else 0
            rem = len(status_lines) - (i + 1)
            more_res = len(f" (+{rem} more)") if (rem > 0 or status_res.truncated) else 0
            if shown and cur_len + sep_len + len(item) + more_res > 260:
                break
            shown.append(item)
            cur_len += sep_len + len(item)
        more = (
            f" (+{len(status_lines) - len(shown)}{'+' if status_res.truncated else ''} more)"
            if len(shown) < len(status_lines)
            else (" (+more)" if status_res.truncated else "")
        )
        parts.append(f"{prefix}{', '.join(shown)}{more}")
    if log_lines:
        parts.append("Recent commits:\n" + "\n".join(f"  - {slice_safe_chars(ln, 72)}" for ln in log_lines))
    return "\n".join(parts)


def is_home_or_system_root(workspace_dir: str) -> bool:
    resolved = os.path.abspath(workspace_dir)
    if os.path.dirname(resolved) == resolved:
        return True
    low = resolved.replace("\\", "/").rstrip("/").lower()
    if low in ("/users", "/home") or re.match(r"^[a-z]:/(?:users|documents and settings)$", low):
        return True
    homes = {
        os.path.abspath(h.strip())
        for h in (str(Path.home()), os.environ.get("HOME", ""), os.environ.get("USERPROFILE", ""))
        if h and h.strip()
    }
    case_i = os.name == "nt" or os.uname().sysname == "Darwin" if hasattr(os, "uname") else os.name == "nt"

    def norm(p: str) -> str:
        return p.lower() if case_i else p

    rkey = norm(resolved)
    if any(norm(h) == rkey for h in homes):
        return True
    try:
        real_ws = os.path.realpath(resolved)
        if os.path.dirname(real_ws) == real_ws:
            return True
        rwk = norm(real_ws)
        for h in homes:
            try:
                if norm(os.path.realpath(h)) in (rkey, rwk):
                    return True
            except OSError:
                continue
    except OSError:
        pass
    return False


def _is_always_skipped_dir(name: str) -> bool:
    low = name.lower()
    return low in _ALWAYS_SKIP_DIRS or low.startswith(("onedrive - ", "$recycle"))


def _should_skip_entry(name: str, is_dir: bool, is_link: bool, is_home: bool) -> bool:
    low = name.lower()
    if low in _SKIP_FILES or low.startswith("ntuser.dat"):
        return True
    if is_dir or is_link:
        if _is_always_skipped_dir(name):
            return True
        if is_home and low in _HOME_ROOT_SKIP_DIRS:
            return True
    if is_dir and (name.startswith(".") or low in _IGNORE_DIRS_LOWER):
        return True
    return False


def _short_name(name: str, max_len: int = 28) -> str:
    return f"{slice_safe_chars(name, max_len - 3)}..." if len(name) > max_len else name


def _get_two_level_tree(
    workspace_dir: str,
    top_entries: list[os.DirEntry[str]] | None,
    is_home: bool,
    deadline: float,
) -> str | None:
    if top_entries is None:
        return None
    filtered = [
        e for e in top_entries
        if not _should_skip_entry(e.name, e.is_dir(follow_symlinks=False), e.is_symlink(), is_home)
    ]
    filtered.sort(
        key=lambda e: (
            0 if e.is_dir(follow_symlinks=False) else 1,
            1 if (e.is_dir(follow_symlinks=False) and e.name.lower() in _COLLAPSE_DIRS) else 0,
            e.name.lower(),
        )
    )
    if not filtered:
        return None
    max_top = 20
    shown = filtered[:max_top]
    lines: list[str] = []
    if is_home:
        for e in shown:
            lines.append(f"  {e.name}/" if e.is_dir(follow_symlinks=False) else f"  {_short_name(e.name)}")
        if len(filtered) > max_top:
            lines.append(f"  ... (+{len(filtered) - max_top} more top-level entries)")
        return "Layout (1 level):\n" + "\n".join(lines)

    timed_out_dirs = 0
    for e in shown:
        if not e.is_dir(follow_symlinks=False):
            lines.append(f"  {_short_name(e.name)}")
            continue
        if time.monotonic() >= deadline:
            timed_out_dirs += 1
            lines.append(f"  {e.name}/")
            continue
        try:
            with os.scandir(e.path) as it:
                subs = list(it)
        except OSError:
            lines.append(f"  {e.name}/")
            continue
        valid = []
        for s in subs:
            low = s.name.lower()
            if low in _SKIP_FILES or low.startswith("ntuser.dat"):
                continue
            s_dir = s.is_dir(follow_symlinks=False)
            if s_dir and (s.name.startswith(".") or low in _IGNORE_DIRS_LOWER or _is_always_skipped_dir(s.name) or low.startswith(("dist-", "build-"))):
                continue
            if s.is_symlink() and _is_always_skipped_dir(s.name):
                continue
            valid.append((s.name, s_dir))
        valid.sort(key=lambda it: (0 if it[1] else 1, 0 if (it[1] and it[0] in _PRIORITY_SUBDIRS) else 1, it[0].lower()))
        if e.name.lower() in _COLLAPSE_DIRS:
            lines.append(f"  {e.name}/ ({len(valid)} entries)")
        elif not valid:
            lines.append(f"  {e.name}/")
        else:
            ch = [f"{_short_name(n)}/" if d else _short_name(n) for n, d in valid[:6]]
            more = f", +{len(valid) - 6}" if len(valid) > 6 else ""
            lines.append(f"  {e.name}/ ({', '.join(ch)}{more})")

    if len(filtered) > max_top:
        lines.append(f"  ... (+{len(filtered) - max_top} more top-level entries)")
    if timed_out_dirs > 0:
        lines.append(f"  ... ({timed_out_dirs} dir{'s' if timed_out_dirs != 1 else ''} skipped; scan timed out)")
    return "Layout (2 levels):\n" + "\n".join(lines)


def _detect_manifests_in_dir(dir_path: str, prefix: str, known_files: set[str] | None = None) -> list[str]:
    hints: list[str] = []

    def has(name: str) -> bool:
        return (known_files is None) or (name in known_files)

    if has("package.json"):
        p = os.path.join(dir_path, "package.json")
        if os.path.isfile(p):
            try:
                pkg = json.loads(Path(p).read_text(encoding="utf-8", errors="replace"))
                if isinstance(pkg, dict):
                    nm = str(pkg.get("name") or "").strip()
                    scripts = list(pkg["scripts"].keys())[:12] if isinstance(pkg.get("scripts"), dict) else []
                    lbl = f"{prefix}package.json ({nm})" if nm else f"{prefix}package.json"
                    hints.append(f"{lbl} scripts: {', '.join(scripts)}" if scripts else lbl)
            except Exception:
                pass

    if not prefix:
        for mk in ("Makefile", "makefile"):
            if has(mk) and os.path.isfile(os.path.join(dir_path, mk)):
                try:
                    raw = Path(os.path.join(dir_path, mk)).read_text(encoding="utf-8", errors="replace")
                    tgts: list[str] = []
                    for ln in raw.splitlines():
                        m = re.match(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", ln)
                        if m and not m.group(1).startswith(".") and m.group(1) not in tgts:
                            tgts.append(m.group(1))
                            if len(tgts) >= 10:
                                break
                    hints.append(f"{mk} targets: {', '.join(tgts)}" if tgts else mk)
                except OSError:
                    pass
                break

    if has("pom.xml") and os.path.isfile(os.path.join(dir_path, "pom.xml")):
        try:
            raw = Path(os.path.join(dir_path, "pom.xml")).read_text(encoding="utf-8", errors="replace")
            stripped = re.sub(r"<parent>[\s\S]*?</parent>", "", raw, flags=re.I)
            m = re.search(r"<artifactId>\s*([^<\s]+)\s*</artifactId>", stripped, re.I)
            hints.append(f"{prefix}pom.xml ({m.group(1)}, maven)" if m else f"{prefix}pom.xml (maven)")
        except OSError:
            pass

    for gr in ("build.gradle.kts", "build.gradle"):
        if has(gr) and os.path.isfile(os.path.join(dir_path, gr)):
            hints.append(f"{prefix}{gr} (gradle)")
            break

    if has("pyproject.toml") and os.path.isfile(os.path.join(dir_path, "pyproject.toml")):
        try:
            raw = Path(os.path.join(dir_path, "pyproject.toml")).read_text(encoding="utf-8", errors="replace")
            tools = [t for t in ("pytest", "ruff", "mypy", "poetry", "uv") if f"[tool.{t}" in raw]
            hints.append(f"{prefix}pyproject.toml ({', '.join(tools)})" if tools else f"{prefix}pyproject.toml")
        except OSError:
            pass

    if has("Cargo.toml") and os.path.isfile(os.path.join(dir_path, "Cargo.toml")):
        try:
            raw = Path(os.path.join(dir_path, "Cargo.toml")).read_text(encoding="utf-8", errors="replace")
            hints.append(f"{prefix}Cargo.toml (workspace)" if "[workspace]" in raw else f"{prefix}Cargo.toml")
        except OSError:
            pass

    if has("go.mod") and os.path.isfile(os.path.join(dir_path, "go.mod")):
        try:
            raw = Path(os.path.join(dir_path, "go.mod")).read_text(encoding="utf-8", errors="replace")
            m = re.search(r"^module\s+(\S+)", raw, re.M)
            hints.append(f"{prefix}go.mod ({m.group(1)})" if m else f"{prefix}go.mod")
        except OSError:
            pass

    return sorted(hints)


def _get_manifest_summary(
    workspace_dir: str,
    top_entries: list[os.DirEntry[str]] | None,
    is_home: bool,
    deadline: float,
) -> str | None:
    known_root = {e.name for e in top_entries if not e.is_dir(follow_symlinks=False)} if top_entries else None
    root_hints = _detect_manifests_in_dir(workspace_dir, "", known_root)
    sub_hints: list[str] = []
    if not is_home and top_entries and time.monotonic() < deadline:
        subdirs = sorted(
            (
                e
                for e in top_entries
                if e.is_dir(follow_symlinks=False)
                and not _should_skip_entry(e.name, True, False, False)
                and e.name.lower() not in _MANIFEST_SKIP_SUBDIRS
            ),
            key=lambda e: e.name.lower(),
        )[:8]
        for d in subdirs:
            if time.monotonic() >= deadline:
                break
            sub_hints.extend(_detect_manifests_in_dir(d.path, f"{d.name}/"))
    all_hints = root_hints + sorted(sub_hints)
    if not all_hints:
        return None
    return "Project:\n" + "\n".join(f"  - {h}" for h in all_hints[:6])


def _fit_sec_bytes(section: str, budget: int, marker: str) -> str:
    if budget <= 0:
        return ""
    if utf8_len(section) <= budget:
        return section
    mb = utf8_len(marker)
    if budget <= mb + 1:
        return slice_utf8_bytes(section, budget)
    trimmed = slice_utf8_bytes(section, budget - mb - 1)
    nl = trimmed.rfind("\n")
    if nl > 0:
        trimmed = trimmed[:nl]
    res = f"{trimmed}\n{marker}"
    return res if utf8_len(res) <= budget else slice_utf8_bytes(res, budget)


def build_workspace_snapshot(
    workspace_dir: str,
    max_bytes: int = MAX_SNAPSHOT_BYTES,
    git_timeout_ms: int = 800,
    scan_timeout_ms: int = DEFAULT_SCAN_TIMEOUT_MS,
) -> str | None:
    if not workspace_dir or not os.path.isdir(workspace_dir):
        return None
    deadline = time.monotonic() + max(1, scan_timeout_ms) / 1000.0
    is_home = is_home_or_system_root(workspace_dir)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_git = pool.submit(_get_git_summary, workspace_dir, git_timeout_ms)
        try:
            with os.scandir(workspace_dir) as it:
                top_entries: list[os.DirEntry[str]] | None = list(it)
        except OSError:
            top_entries = None
        tree_sum = _get_two_level_tree(workspace_dir, top_entries, is_home, deadline)
        proj_sum = _get_manifest_summary(workspace_dir, top_entries, is_home, deadline)
        git_sum = f_git.result()

    header = f"[agy-tokensaver startup snapshot] Workspace: {workspace_dir}"
    sections = [s for s in (header, git_sum, proj_sum, tree_sum) if s]
    if len(sections) <= 1:
        return None
    full = "\n".join(sections)
    if utf8_len(full) <= max_bytes:
        return full

    suffix = "... [snapshot capped]"
    hb = utf8_len(header)
    if max_bytes <= hb + utf8_len(suffix) + 4:
        return truncate_with_byte_notice(full, max_bytes)

    non_empty = sum(1 for s in (git_sum, proj_sum, tree_sum) if s)
    avail = max_bytes - hb - non_empty
    budget_left = avail
    proj_part = git_part = tree_part = ""

    if proj_sum and budget_left > 0:
        other = bool(git_sum or tree_sum)
        pcap = min(utf8_len(proj_sum), max(80, int(avail * 0.25))) if other else budget_left
        proj_part = _fit_sec_bytes(proj_sum, min(budget_left, pcap), "  ... [project capped]")
        budget_left -= utf8_len(proj_part)
    if git_sum and budget_left > 0:
        gcap = min(utf8_len(git_sum), max(100, int(budget_left * 0.48))) if tree_sum else budget_left
        git_part = _fit_sec_bytes(git_sum, min(budget_left, gcap), "  ... [git capped]")
        budget_left -= utf8_len(git_part)
    if tree_sum and budget_left > 0:
        tree_part = _fit_sec_bytes(tree_sum, budget_left, suffix)

    assembled = "\n".join(s for s in (header, git_part, proj_part, tree_part) if s)
    return assembled if utf8_len(assembled) <= max_bytes else _fit_sec_bytes(assembled, max_bytes, suffix)
