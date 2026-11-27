"""Batched concurrent search tool (`batch_search`) for agy-tokensaver.

Supports up to 20 mixed queries per call (`text`, `regex`, `glob`, `fuzzy`, and
hardened read-only `git`), using ripgrep (`rg`) when installed with an automatic
pure-Python fallback, water-filling output budgets, and a strict <= 8,800 UTF-8
byte inline cap to eliminate disk spills.

Incorporates engineering patterns adapted from INK-Q/agy-fast (MIT License).
"""

from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from fast_utils import (
    DEFAULT_IGNORE_DIRS,
    DEFAULT_INLINE_MAX_BYTES,
    TOOL_VALIDATION_EXAMPLES,
    clamp_int,
    compute_water_fill_budgets,
    exec_binary,
    fit_sections_to_byte_budget,
    format_corrupted_array_error,
    glob_to_regex,
    is_likely_binary,
    is_ripgrep_available,
    parse_maybe_json_array,
    rank_fuzzy_paths,
    resolve_path,
    slice_safe_chars,
    to_posix_path,
    utf8_len,
    walk_files,
    with_non_interactive_git_env,
    with_validation_example,
)

MAX_CONTEXT_LINES = 10
DEFAULT_CALL_TIMEOUT_MS = 60_000
DEFAULT_WALK_TIMEOUT_MS = 12_000
DEFAULT_WALK_MAX_DIRS = 20_000
MAX_SEARCH_LINE_CHARS = 240

READONLY_GIT_SUBCOMMANDS = frozenset(
    {
        "status",
        "diff",
        "log",
        "show",
        "branch",
        "rev-parse",
        "ls-files",
        "blame",
        "shortlog",
        "describe",
        "tag",
        "remote",
    }
)
FORBIDDEN_GIT_GLOBAL_FLAGS = frozenset(
    {
        "-c",
        "--config",
        "--config-env",
        "--exec-path",
        "--ext-diff",
        "--textconv",
        "--output",
        "--exec",
        "--upload-pack",
        "--git-dir",
        "--work-tree",
    }
)
_BRANCH_BOOL_FLAGS = frozenset(
    {
        "--all",
        "--remotes",
        "--verbose",
        "--list",
        "--show-current",
        "--no-abbrev",
        "--no-color",
        "--ignore-case",
        "--omit-empty",
        "--no-column",
        "--quiet",
    }
)
_BRANCH_EQ_OPT_FLAGS = frozenset({"--abbrev", "--color", "--column"})
_BRANCH_COMMIT_FLAGS = frozenset({"--merged", "--no-merged", "--contains", "--no-contains", "--points-at"})
_BRANCH_VALUE_FLAGS = frozenset({"--contains", "--no-contains", "--points-at", "--sort", "--format"})
_BRANCH_SHORT_CHARS = frozenset("arvlqi")
_TAG_BOOL_FLAGS = frozenset({"--list", "--no-color", "--ignore-case", "--omit-empty", "--no-column"})
_TAG_EQ_OPT_FLAGS = frozenset({"--color", "--column"})
_TAG_COMMIT_FLAGS = frozenset({"--merged", "--no-merged", "--contains", "--no-contains", "--points-at"})
_TAG_VALUE_FLAGS = frozenset({"--contains", "--no-contains", "--points-at", "--sort", "--format"})
_TAG_SHORT_CHARS = frozenset("li")
_REMOTE_SUBCOMMANDS = frozenset({"show", "get-url"})
_SHORTLOG_VALUE_FLAGS = frozenset(
    {
        "--group",
        "--format",
        "--since",
        "--after",
        "--until",
        "--before",
        "--author",
        "--committer",
        "--grep",
        "--max-count",
        "--skip",
    }
)
_SHORTLOG_REV_FLAGS = frozenset({"--all", "--branches", "--tags", "--remotes", "--glob", "--stdin"})

FILE_TYPE_GLOBS: dict[str, list[str]] = {
    "ts": ["*.ts", "*.tsx", "*.mts", "*.cts"],
    "typescript": ["*.ts", "*.tsx", "*.mts", "*.cts"],
    "js": ["*.js", "*.jsx", "*.mjs", "*.cjs"],
    "javascript": ["*.js", "*.jsx", "*.mjs", "*.cjs"],
    "py": ["*.py", "*.pyi"],
    "python": ["*.py", "*.pyi"],
    "go": ["*.go"],
    "gd": ["*.gd"],
    "gdscript": ["*.gd"],
    "rust": ["*.rs"],
    "rs": ["*.rs"],
    "java": ["*.java"],
    "kt": ["*.kt", "*.kts"],
    "kotlin": ["*.kt", "*.kts"],
    "c": ["*.c", "*.h"],
    "cpp": ["*.cc", "*.cpp", "*.cxx", "*.hpp", "*.hh", "*.hxx", "*.h"],
    "cs": ["*.cs"],
    "csharp": ["*.cs"],
    "swift": ["*.swift"],
    "rb": ["*.rb"],
    "ruby": ["*.rb"],
    "php": ["*.php"],
    "css": ["*.css", "*.scss", "*.sass", "*.less"],
    "html": ["*.html", "*.htm"],
    "vue": ["*.vue"],
    "svelte": ["*.svelte"],
    "md": ["*.md", "*.mdx", "*.markdown"],
    "markdown": ["*.md", "*.mdx", "*.markdown"],
    "json": ["*.json", "*.jsonc"],
    "yaml": ["*.yml", "*.yaml"],
    "yml": ["*.yml", "*.yaml"],
    "toml": ["*.toml"],
    "xml": ["*.xml"],
    "sql": ["*.sql"],
    "sh": ["*.sh", "*.bash", "*.zsh"],
    "shell": ["*.sh", "*.bash", "*.zsh"],
}


def tokenize_args(text: str) -> list[str]:
    tokens: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    esc = False
    for ch in text:
        if esc:
            cur.append(ch)
            esc = False
            continue
        if ch == "\\" and quote != "'":
            esc = True
            continue
        if quote:
            if ch == quote:
                quote = None
            else:
                cur.append(ch)
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch.isspace():
            if cur:
                tokens.append("".join(cur))
                cur = []
            continue
        cur.append(ch)
    if quote:
        raise ValueError("Unterminated quote in git query")
    if cur:
        tokens.append("".join(cur))
    return tokens


def validate_readonly_git_args(pattern: str) -> list[str]:
    raw_tokens = tokenize_args(pattern.strip())
    tokens = raw_tokens[1:] if raw_tokens and raw_tokens[0] == "git" else raw_tokens
    if not tokens:
        raise ValueError("Empty git query. Example: 'status --short' or 'log -n 5 --oneline'")
    subcmd = tokens[0]
    if subcmd.startswith("-"):
        raise ValueError(f"Git query must start with a read-only subcommand (got '{subcmd}')")
    if subcmd not in READONLY_GIT_SUBCOMMANDS:
        raise ValueError(
            f"Git subcommand '{subcmd}' is not allowed in batch_search (read-only only: {', '.join(sorted(READONLY_GIT_SUBCOMMANDS))})"
        )
    rest = tokens[1:]
    for arg in rest:
        if arg in ("&&", "||", ";", "|"):
            raise ValueError(
                f"Shell operator '{arg}' is not supported in git queries; split chained commands into separate batch_search queries"
            )
        flag = arg.split("=", 1)[0]
        if (
            flag in FORBIDDEN_GIT_GLOBAL_FLAGS
            or arg.startswith("--output=")
            or flag == "--ext"
            or flag.startswith("--ext-")
            or flag.startswith("--textc")
            or (arg.startswith("-c") and len(arg) > 2)
        ):
            raise ValueError(f"Flag '{arg}' is not allowed in read-only git query")

    if subcmd == "status":
        for arg in rest:
            if arg == "--":
                break
            if arg.startswith("--"):
                if arg.split("=", 1)[0].startswith("--v"):
                    raise ValueError(
                        f"Flag '{arg}' is not allowed in read-only git status query (use 'diff' instead)"
                    )
            elif arg.startswith("-") and len(arg) > 1:
                for ch in arg[1:]:
                    if ch in ("u", "M"):
                        break
                    if ch == "v":
                        raise ValueError(
                            f"Flag '{arg}' is not allowed in read-only git status query (use 'diff' instead)"
                        )
    elif subcmd == "branch":
        has_list = False
        expect_val = False
        expect_commit = False
        for arg in rest:
            if expect_val:
                expect_val = False
                continue
            if expect_commit:
                expect_commit = False
                if arg.startswith("-"):
                    raise ValueError(f"Expected commit ref after commit filter flag (got '{arg}')")
                continue
            if arg == "--":
                if not has_list:
                    raise ValueError("Positional branch arguments are only allowed with --list in read-only mode")
                break
            if arg.startswith("--"):
                flag = arg.split("=", 1)[0]
                if "=" in arg:
                    if flag not in _BRANCH_VALUE_FLAGS and flag not in _BRANCH_EQ_OPT_FLAGS and flag not in _BRANCH_COMMIT_FLAGS:
                        raise ValueError(f"Flag '{arg}' is not allowed in read-only git branch query")
                elif flag in _BRANCH_COMMIT_FLAGS:
                    expect_commit = True
                    has_list = True
                elif flag in _BRANCH_VALUE_FLAGS:
                    expect_val = True
                elif flag not in _BRANCH_BOOL_FLAGS and flag not in _BRANCH_EQ_OPT_FLAGS:
                    raise ValueError(f"Flag '{arg}' is not allowed in read-only git branch query")
                if flag == "--list" or flag in _BRANCH_COMMIT_FLAGS:
                    has_list = True
            elif arg.startswith("-") and len(arg) > 1:
                for ch in arg[1:]:
                    if ch not in _BRANCH_SHORT_CHARS:
                        raise ValueError(f"Flag '{arg}' is not allowed in read-only git branch query")
                    if ch == "l":
                        has_list = True
            elif not has_list:
                raise ValueError(
                    f"Positional branch argument '{arg}' is only allowed with --list in read-only mode"
                )
    elif subcmd == "tag":
        allows_pos = False
        expect_val = False
        expect_commit = False
        for arg in rest:
            if expect_val:
                expect_val = False
                continue
            if expect_commit:
                expect_commit = False
                if arg.startswith("-"):
                    raise ValueError(f"Expected commit ref after commit filter flag (got '{arg}')")
                continue
            if arg == "--":
                if not allows_pos:
                    raise ValueError("Positional tag arguments are only allowed with --list in read-only mode")
                break
            if arg.startswith("--"):
                flag = arg.split("=", 1)[0]
                if "=" in arg:
                    if flag not in _TAG_VALUE_FLAGS and flag not in _TAG_EQ_OPT_FLAGS and flag not in _TAG_COMMIT_FLAGS:
                        raise ValueError(f"Flag '{arg}' is not allowed in read-only git tag query")
                elif flag in _TAG_COMMIT_FLAGS:
                    expect_commit = True
                    allows_pos = True
                elif flag in _TAG_VALUE_FLAGS:
                    expect_val = True
                elif flag not in _TAG_BOOL_FLAGS and flag not in _TAG_EQ_OPT_FLAGS:
                    raise ValueError(f"Flag '{arg}' is not allowed in read-only git tag query")
                if flag == "--list" or flag in _TAG_COMMIT_FLAGS:
                    allows_pos = True
            elif arg.startswith("-") and len(arg) > 1:
                if re.match(r"^-n\d*$", arg):
                    allows_pos = True
                else:
                    for ch in arg[1:]:
                        if ch not in _TAG_SHORT_CHARS:
                            raise ValueError(f"Flag '{arg}' is not allowed in read-only git tag query")
                        if ch == "l":
                            allows_pos = True
            elif not allows_pos:
                raise ValueError(
                    f"Positional tag argument '{arg}' is only allowed with --list in read-only mode"
                )
    elif subcmd == "remote":
        remote_sub: str | None = None
        for arg in rest:
            if remote_sub is None:
                if arg in ("-v", "--verbose"):
                    continue
                if arg.startswith("-"):
                    raise ValueError(f"Flag '{arg}' is not allowed in read-only git remote query")
                if arg not in _REMOTE_SUBCOMMANDS:
                    raise ValueError(f"Mutating git remote subcommand '{arg}' is not allowed")
                remote_sub = arg
            elif arg.startswith("-"):
                ok_flag = (remote_sub == "show" and arg == "-n") or (
                    remote_sub == "get-url" and arg in ("--push", "--all")
                )
                if not ok_flag:
                    raise ValueError(f"Flag '{arg}' is not allowed for read-only git remote {remote_sub}")
            elif any(tok in arg for tok in ("://", ":", "/", "\\")):
                raise ValueError(f"Remote target '{arg}' must be a configured remote name, not a URL or path")

    return ["--no-pager", subcmd, *rest]


def _harden_git_args(validated: list[str]) -> list[str]:
    subcmd = validated[1]
    rest = validated[2:]
    sub_args = list(rest)
    if subcmd in ("diff", "log", "show"):
        sub_args = ["--no-ext-diff", "--no-textconv", *rest]
    elif subcmd == "blame":
        sub_args = ["--no-textconv", *rest]
    elif subcmd == "remote":
        idx = next((i for i, a in enumerate(rest) if not a.startswith("-")), -1)
        if idx != -1 and rest[idx] == "show" and "-n" not in rest:
            sub_args = [*rest[: idx + 1], "-n", *rest[idx + 1 :]]
    elif subcmd == "shortlog":
        has_rev = False
        expect_val = False
        dd_idx = -1
        for i, a in enumerate(rest):
            if expect_val:
                expect_val = False
                continue
            if a == "--":
                dd_idx = i
                break
            if a.startswith("-"):
                flag = a.split("=", 1)[0]
                if flag in _SHORTLOG_REV_FLAGS:
                    has_rev = True
                elif "=" not in a and flag in _SHORTLOG_VALUE_FLAGS:
                    expect_val = True
                continue
            has_rev = True
        if not has_rev:
            sub_args = (
                [*rest, "HEAD"]
                if dd_idx == -1
                else [*rest[:dd_idx], "HEAD", *rest[dd_idx:]]
            )
        sub_args = ["--no-ext-diff", "--no-textconv", *sub_args]

    return [
        "--no-optional-locks",
        "-c", "core.fsmonitor=false",
        "-c", "core.quotePath=false",
        "-c", "diff.external=",
        "-c", "gpg.program=false",
        "-c", "gpg.openpgp.program=false",
        "-c", "gpg.ssh.program=false",
        "-c", "gpg.x509.program=false",
        "--no-pager",
        subcmd,
        *sub_args,
    ]


def _resolve_file_type_globs(file_type: Any) -> list[str] | None:
    if file_type is None:
        return None
    if not isinstance(file_type, str):
        raise ValueError("fileType must be a string")
    key = file_type.strip().lower().lstrip(".")
    if not key:
        return None
    if key in FILE_TYPE_GLOBS:
        return FILE_TYPE_GLOBS[key]
    if re.fullmatch(r"[a-z0-9]+", key):
        return [f"*.{key}"]
    raise ValueError(f"Unsupported fileType '{file_type}'")


def _file_type_matcher(file_type: Any):
    globs = _resolve_file_type_globs(file_type) or []
    res = [glob_to_regex(g) for g in globs]
    if not res:
        return lambda _f: True
    return lambda f: any(r.match(os.path.basename(f)) for r in res)


def _relative_file(base_cwd: str, file_path: str) -> str:
    abs_p = os.path.abspath(os.path.join(base_cwd, file_path))
    try:
        rel = to_posix_path(os.path.relpath(abs_p, base_cwd))
        return rel if rel and rel != "." else os.path.basename(abs_p)
    except ValueError:
        return to_posix_path(abs_p)


def _pattern_targets_hidden(pattern: str) -> bool:
    norm = re.sub(r"^(?:\./)+", "", pattern.replace("\\", "/"))
    return bool(re.search(r"(?:^|[/,{])\.[^./]", norm))


def _glob_targets_dir(glob_str: str, dirname: str) -> bool:
    norm = re.sub(r"(^|[,{])(?:\./)+", r"\1", glob_str.replace("\\", "/"))
    esc = re.escape(dirname)
    return bool(re.search(rf"(?:^|[/,{{]){esc}(?:$|[/}},])", norm))


@dataclass
class SearchLine:
    line_number: int
    text: str
    is_match: bool
    match_start: int | None = None
    match_end: int | None = None


@dataclass
class FileMatches:
    file: str
    lines: list[SearchLine] = field(default_factory=list)
    count: int = 0


def _format_line_snippet(line: SearchLine, max_chars: int = MAX_SEARCH_LINE_CHARS) -> str:
    text = line.text
    if len(text) <= max_chars:
        return text
    if not line.is_match or line.match_start is None:
        return f"{slice_safe_chars(text, max_chars)}..."
    n = len(text)
    m_start = max(0, min(n, line.match_start))
    m_end = max(m_start, min(n, line.match_end if line.match_end is not None else m_start))
    if m_start < max_chars and m_end <= max_chars:
        return f"{slice_safe_chars(text, max_chars)}..."
    match_len = m_end - m_start
    lead = (
        min(int(max_chars * 0.1), m_start)
        if match_len >= max_chars
        else min(min(80, int(max_chars * 0.35)), (max_chars - match_len) // 2)
    )
    win_start = max(0, m_start - lead)
    win_end = min(n, win_start + max_chars)
    if win_end == n:
        win_start = max(0, win_end - max_chars)
    prefix = "..." if win_start > 0 else ""
    suffix = "..." if win_end < n else ""
    return f"{prefix}{text[win_start:win_end]}{suffix}"


def _format_file_matches(files: list[FileMatches], total_matches: int, truncated: bool) -> str:
    if not files:
        return "(no matches)"
    blocks: list[str] = []
    for fm in files:
        lns = [
            f"  {ln.line_number}{':' if ln.is_match else '-'} {_format_line_snippet(ln)}"
            for ln in fm.lines
        ]
        blocks.append(f"{fm.file}\n" + "\n".join(lns))
    footer = f"\n... [truncated at {total_matches} matches]" if truncated else ""
    return "\n".join(blocks) + footer


def _run_text_regex_rg(
    query: dict[str, Any],
    search_root: str,
    base_cwd: str,
    call_deadline: float,
) -> tuple[list[FileMatches], int, bool, bool]:
    max_results = clamp_int(query.get("maxResults"), 1, 200, 40)
    context_lines = clamp_int(query.get("contextLines"), 0, MAX_CONTEXT_LINES, 0)
    output_mode = str(query.get("outputMode") or "content")
    summary = output_mode in ("count", "files_with_matches")
    multiline = bool(query.get("multiline"))
    type_globs = _resolve_file_type_globs(query.get("fileType"))
    matches_type = _file_type_matcher(query.get("fileType"))
    is_file = os.path.isfile(search_root)

    if is_file:
        if type_globs and not matches_type(search_root):
            return [], 0, False, False
        if os.path.getsize(search_root) > 2 * 1024 * 1024:
            return [], 0, False, False
        qglob = query.get("glob")
        if isinstance(qglob, str) and qglob:
            neg = qglob.startswith("!")
            raw_g = qglob[1:] if neg else qglob
            gre = glob_to_regex(raw_g)
            rel_p = _relative_file(base_cwd, search_root)
            hit = bool(gre.match(rel_p) or gre.match(os.path.basename(search_root)))
            if hit if neg else not hit:
                return [], 0, False, False
        with open(search_root, "rb") as fh:
            if is_likely_binary(fh.read(8192)):
                return [], 0, False, False

    per_file_cap = max_results if is_file else min(max_results, 50)
    args = ["--no-config", "--no-messages", "--max-filesize", "2M", "--crlf"]
    if multiline:
        args.extend(["--multiline", "--multiline-dotall"])
    if type_globs:
        for g in type_globs:
            args.extend(["--type-add", f"agyfilter:{g}"])
        args.extend(["--type", "agyfilter"])
    if query.get("type") == "text":
        args.append("--fixed-strings")
    if query.get("caseSensitive") is False:
        args.append("--ignore-case")
    qglob = query.get("glob")
    if isinstance(qglob, str) and qglob:
        args.append(f"--glob={qglob}")

    try:
        rel_native = os.path.relpath(search_root, base_cwd)
        rel_root = to_posix_path(rel_native)
        inside = rel_root == "" or (rel_root != ".." and not rel_root.startswith("../") and not os.path.isabs(rel_root))
    except ValueError:
        rel_native = search_root
        rel_root = to_posix_path(search_root)
        inside = False

    rg_target = ("." if not rel_native or rel_native == "." else (f".{os.sep}-" if rel_native == "-" else rel_native)) if inside else search_root
    root_segs = (rel_root.split("/") if rel_root and rel_root != "." else []) if inside else [s for s in to_posix_path(search_root).split("/") if s]
    pos_glob = qglob if (isinstance(qglob, str) and qglob and not qglob.startswith("!")) else None
    for d in ("node_modules", ".git", "dist", "build", "target", "coverage", "__pycache__", ".venv", ".next"):
        if d not in root_segs and not (pos_glob and _glob_targets_dir(pos_glob, d)):
            args.append(f"--glob=!**/{d}/**")

    rg_timed_out = False

    def _run_rg(run_args: list[str], allow_partial: bool = False):
        nonlocal rg_timed_out
        rem_ms = int((call_deadline - time.monotonic()) * 1000)
        if rem_ms <= 0:
            rg_timed_out = True
            return None
        res = exec_binary("rg", run_args, cwd=base_cwd, timeout_ms=max(250, min(15_000, rem_ms)), max_output_bytes=2 * 1024 * 1024)
        if res.timed_out:
            rg_timed_out = True
        if res.error:
            raise RuntimeError(f"rg spawn error: {res.error}")
        if res.exit_code == 2 and not (allow_partial and not res.stderr.strip()):
            raise ValueError(res.stderr.trim() if hasattr(res.stderr, "trim") else (res.stderr.strip() or f"rg failed for pattern: {query['pattern']}"))
        return res

    pattern = str(query["pattern"])
    if summary:
        mode_args = (
            ["--count-matches" if multiline else "--count", "--with-filename", "--null"]
            if output_mode == "count"
            else ["--files-with-matches", "--null"]
        )
        res = _run_rg([*args, *mode_args, "--", pattern, rg_target])
        if res is None:
            return [], 0, True, True
        files: list[FileMatches] = []
        out = res.stdout
        pos = 0
        while pos < len(out):
            nul = out.find("\0", pos)
            if nul == -1:
                break
            raw_file = out[pos:nul]
            cnt = 0
            if output_mode == "count":
                nl = out.find("\n", nul + 1)
                if nl == -1:
                    break
                try:
                    cnt = int(out[nul + 1 : nl].strip())
                except ValueError:
                    cnt = 0
                pos = nl + 1
            else:
                pos = nul + 1
            if raw_file and matches_type(raw_file):
                files.append(FileMatches(file=_relative_file(base_cwd, raw_file), count=cnt))
        files.sort(key=lambda fm: fm.file)
        trunc = len(files) > max_results or res.truncated or res.timed_out or res.exit_code is None
        return files[:max_results], 0, trunc, rg_timed_out

    max_count_args = [] if multiline else ["--max-count", str(per_file_cap + 1)]
    chosen: list[tuple[str, str, int]] = []
    truncated = False
    if is_file:
        chosen = [(search_root, _relative_file(base_cwd, search_root), 10**9)]
    else:
        counts = _run_rg([*args, *max_count_args, "--count", "--with-filename", "--null", "--", pattern, rg_target])
        if counts is None:
            return [], 0, True, True
        counted: list[tuple[str, str, int]] = []
        for rec in counts.stdout.split("\n")[:-1]:
            sep = rec.rfind("\0")
            if sep <= 0:
                continue
            try:
                cnt = int(rec[sep + 1 :])
            except ValueError:
                continue
            if cnt <= 0:
                continue
            raw_f = rec[:sep]
            if not matches_type(raw_f):
                continue
            counted.append((raw_f, _relative_file(base_cwd, raw_f), cnt))
        counted.sort(key=lambda x: x[1])
        covered = 0
        for item in counted:
            if covered >= max_results:
                break
            chosen.append(item)
            covered += item[2] if multiline else min(item[2], per_file_cap)
        truncated = counts.truncated or counts.timed_out or counts.exit_code is None or len(chosen) < len(counted)
        if not chosen:
            return [], 0, truncated, rg_timed_out

    chunks: list[list[str]] = []
    cur_chunk: list[str] = []
    cur_chars = 0
    for raw_f, _rel_f, _cnt in chosen:
        try:
            r = os.path.relpath(os.path.abspath(os.path.join(base_cwd, raw_f)), base_cwd)
            arg_p = r if (r and r != ".." and not r.startswith(f"..{os.sep}") and not os.path.isabs(r)) else raw_f
        except ValueError:
            arg_p = raw_f
        if arg_p == "-":
            arg_p = f".{os.sep}-"
        est = len(arg_p) + 3
        if cur_chunk and cur_chars + est > 20_000:
            chunks.append(cur_chunk)
            cur_chunk = []
            cur_chars = 0
        cur_chunk.append(arg_p)
        cur_chars += est
    if cur_chunk:
        chunks.append(cur_chunk)

    by_file: dict[str, dict[int, SearchLine]] = {}
    for ch in chunks:
        ctx_args = ["-C", str(context_lines)] if context_lines > 0 else []
        res = _run_rg([*args, *max_count_args, "--json", *ctx_args, "--", pattern, *ch], allow_partial=not is_file)
        if res is None:
            truncated = True
            break
        if res.truncated or res.timed_out or res.exit_code is None:
            truncated = True
        for raw_ln in res.stdout.split("\n"):
            if not raw_ln.strip():
                continue
            try:
                ev = json.loads(raw_ln)
            except Exception:
                continue
            ev_type = ev.get("type")
            if ev_type not in ("match", "context"):
                continue
            data = ev.get("data") or {}
            path_obj = data.get("path") or {}
            raw_f = path_obj.get("text")
            if not raw_f:
                continue
            lnum = data.get("line_number")
            if not isinstance(lnum, int):
                continue
            lines_obj = data.get("lines") or {}
            raw_text = str(lines_obj.get("text") or "")
            rel_f = _relative_file(base_cwd, raw_f)
            lmap = by_file.setdefault(rel_f, {})
            is_match = ev_type == "match"
            sm_ranges: list[tuple[int, int]] = []
            if is_match:
                raw_b = raw_text.encode("utf-8")
                for sm in data.get("submatches") or []:
                    st_b = sm.get("start")
                    en_b = sm.get("end")
                    if isinstance(st_b, int):
                        st_c = len(raw_b[:st_b].decode("utf-8", errors="ignore"))
                        en_c = len(raw_b[:en_b].decode("utf-8", errors="ignore")) if isinstance(en_b, int) else st_c
                        sm_ranges.append((st_c, max(st_c, en_c)))
            line_text = re.sub(r"\r?\n$", "", raw_text)
            segs = re.split(r"\r?\n", line_text) if (is_match and multiline) else [line_text]
            seg_start = 0
            for off, seg in enumerate(segs):
                seg_end = seg_start + len(seg)
                ln_no = lnum + off
                existing = lmap.get(ln_no)
                if not (existing and existing.is_match):
                    m_st = m_en = None
                    if is_match and sm_ranges:
                        for rs, re_ in sm_ranges:
                            if (rs == re_ and seg_start <= rs <= seg_end) or (rs <= seg_end and re_ > seg_start):
                                m_st = max(0, min(rs - seg_start, len(seg)))
                                m_en = max(m_st, min(re_ - seg_start, len(seg)))
                                break
                    lmap[ln_no] = SearchLine(ln_no, seg, is_match, m_st, m_en)
                seg_start = seg_end
                if seg_start < len(raw_text) and raw_text[seg_start : seg_start + 2] == "\r\n":
                    seg_start += 2
                elif seg_start < len(raw_text) and raw_text[seg_start] == "\n":
                    seg_start += 1

    out_files: list[FileMatches] = []
    total_matches = 0
    for _raw_f, rel_f, cnt in chosen:
        if total_matches >= max_results:
            truncated = True
            break
        file_cap = max_results if multiline else min(cnt, per_file_cap)
        budget = min(file_cap, max_results - total_matches)
        sorted_lns = sorted(by_file.get(rel_f, {}).values(), key=lambda x: x.line_number)
        kept_lns: list[SearchLine] = []
        kept = 0
        last_match_ln = -1
        for sl in sorted_lns:
            if sl.is_match:
                if kept == budget:
                    truncated = True
                    if last_match_ln != -1 and sl.line_number <= last_match_ln + context_lines:
                        kept_lns.append(SearchLine(sl.line_number, sl.text, False))
                    continue
                kept += 1
                last_match_ln = sl.line_number
                kept_lns.append(sl)
            elif kept < budget or sl.line_number <= last_match_ln + context_lines:
                kept_lns.append(sl)
        if kept > 0:
            out_files.append(FileMatches(rel_f, kept_lns))
            total_matches += kept
    return out_files, total_matches, truncated, rg_timed_out


def _run_text_regex_python(
    query: dict[str, Any],
    search_root: str,
    base_cwd: str,
    call_deadline: float,
    walk_timeout_ms: int,
    walk_max_dirs: int,
) -> tuple[list[FileMatches], int, bool, bool]:
    max_results = clamp_int(query.get("maxResults"), 1, 200, 40)
    context_lines = clamp_int(query.get("contextLines"), 0, MAX_CONTEXT_LINES, 0)
    output_mode = str(query.get("outputMode") or "content")
    summary = output_mode in ("count", "files_with_matches")
    multiline = bool(query.get("multiline"))
    case_sensitive = query.get("caseSensitive") is not False
    pattern = str(query["pattern"])

    re_flags = (0 if case_sensitive else re.I) | (re.M | re.S if multiline else 0)
    if query.get("type") == "regex":
        try:
            regex = re.compile(pattern, re_flags)
        except re.error as exc:
            raise ValueError(f"Invalid regex '{pattern}': {exc}") from exc
    else:
        regex = re.compile(re.escape(pattern), re_flags)

    matches_type = _file_type_matcher(query.get("fileType"))
    qglob = query.get("glob")
    glob_neg = isinstance(qglob, str) and qglob.startswith("!")
    raw_glob = (qglob[1:] if glob_neg else qglob) if (isinstance(qglob, str) and qglob) else None
    glob_re = glob_to_regex(raw_glob) if raw_glob else None

    def _glob_ok(rel_p: str) -> bool:
        if not matches_type(rel_p):
            return False
        if not glob_re:
            return True
        hit = bool(glob_re.match(rel_p))
        if not hit and raw_glob and "/" not in raw_glob:
            hit = bool(glob_re.match(rel_p.rsplit("/", 1)[-1]))
        return (not hit) if glob_neg else hit

    is_file = os.path.isfile(search_root)
    per_file_cap = max_results if is_file else min(max_results, 50)
    disc_trunc = False
    budget_hit = False
    targets: list[tuple[str, str]] = []
    if is_file:
        rel_p = _relative_file(base_cwd, search_root)
        if _glob_ok(rel_p):
            targets = [(search_root, rel_p)]
    else:
        rem_ms = int((call_deadline - time.monotonic()) * 1000)
        if rem_ms <= 0:
            return [], 0, True, True
        inc_hidden = bool(raw_glob and not glob_neg and _pattern_targets_hidden(raw_glob))
        ign = set(DEFAULT_IGNORE_DIRS)
        if raw_glob and not glob_neg:
            for d in list(ign):
                if _glob_targets_dir(raw_glob, d):
                    ign.discard(d)
        walked = walk_files(
            search_root,
            max_files=8000,
            include_hidden=inc_hidden,
            ignore_dirs=ign,
            file_filter=_glob_ok,
            timeout_ms=min(walk_timeout_ms, rem_ms),
            max_dirs=walk_max_dirs,
        )
        disc_trunc = walked.truncated
        budget_hit = walked.timed_out or walked.dirs_truncated
        targets = sorted(
            ((os.path.join(search_root, f), _relative_file(base_cwd, os.path.join(search_root, f))) for f in walked.files),
            key=lambda x: x[1],
        )

    pat_bytes = pattern.encode("utf-8") if (query.get("type") == "text" and case_sensitive and not multiline and pattern) else None
    out_files: list[FileMatches] = []
    total_matches = 0
    truncated = False

    for abs_f, rel_f in targets:
        if time.monotonic() >= call_deadline:
            budget_hit = True
            truncated = True
            break
        try:
            if os.path.getsize(abs_f) > 2 * 1024 * 1024:
                continue
            with open(abs_f, "rb") as fh:
                buf = fh.read()
        except OSError:
            continue
        if is_likely_binary(buf):
            continue
        if pat_bytes is not None and pat_bytes not in buf:
            continue
        content = buf.decode("utf-8", errors="replace")
        if multiline:
            if summary:
                cnt = 0
                for _m in regex.finditer(content):
                    cnt += 1
                    if output_mode == "files_with_matches":
                        break
                if cnt:
                    if len(out_files) >= max_results:
                        truncated = True
                        break
                    out_files.append(FileMatches(rel_f, count=cnt))
                continue
            lines = re.split(r"\r?\n", content)
            if lines and lines[-1] == "":
                lines.pop()
            line_starts = [0]
            for idx_c, ch in enumerate(content):
                if ch == "\n":
                    line_starts.append(idx_c + 1)
            matched_info: list[tuple[int, int, int]] = []
            seen_ln: set[int] = set()
            for m in regex.finditer(content):
                st, en = m.start(), m.end()
                lo = 0
                hi = len(line_starts) - 1
                while lo <= hi:
                    mid = (lo + hi) // 2
                    if line_starts[mid] <= st:
                        lo = mid + 1
                    else:
                        hi = mid - 1
                ln_idx = max(0, hi)
                end_pos = max(st, en - 1)
                while ln_idx < len(lines):
                    ls = line_starts[ln_idx]
                    le = ls + len(lines[ln_idx])
                    if ln_idx not in seen_ln:
                        seen_ln.add(ln_idx)
                        ms = max(0, min(st - ls, len(lines[ln_idx])))
                        me = max(ms, min(en - ls, len(lines[ln_idx])))
                        matched_info.append((ln_idx, ms, me))
                        if len(matched_info) >= max_results + 1:
                            break
                    if le >= end_pos:
                        break
                    ln_idx += 1
                if len(matched_info) >= max_results + 1:
                    break
            if not matched_info:
                continue
        else:
            lines = re.split(r"\r?\n", content)
            if lines and lines[-1] == "":
                lines.pop()
            matched_info = []
            file_trunc = False
            for idx, ln in enumerate(lines):
                mt = regex.search(ln)
                if mt:
                    if not summary and len(matched_info) >= per_file_cap:
                        file_trunc = True
                        break
                    matched_info.append((idx, mt.start(), mt.end()))
                    if output_mode == "files_with_matches":
                        break
            if not matched_info:
                continue
            if summary:
                if len(out_files) >= max_results:
                    truncated = True
                    break
                out_files.append(FileMatches(rel_f, count=len(matched_info)))
                continue
            if file_trunc:
                truncated = True

        rem = max_results - total_matches
        if len(matched_info) > rem:
            kept = matched_info[:rem]
            truncated = True
        else:
            kept = matched_info
        lmap: dict[int, SearchLine] = {}
        for m_idx, ms, me in kept:
            for j in range(max(0, m_idx - context_lines), min(len(lines), m_idx + context_lines + 1)):
                ln_no = j + 1
                if j == m_idx:
                    lmap[ln_no] = SearchLine(ln_no, lines[j], True, ms, me)
                elif ln_no not in lmap:
                    lmap[ln_no] = SearchLine(ln_no, lines[j], False)
        out_files.append(FileMatches(rel_f, sorted(lmap.values(), key=lambda x: x.line_number)))
        total_matches += len(kept)
        if total_matches >= max_results:
            truncated = True
            break

    return out_files, total_matches, truncated or disc_trunc, budget_hit


def _make_glob_term_matcher(gre: re.Pattern[str], match_base: bool) -> Callable[[str, str], bool]:
    def _match(rel: str, base: str) -> bool:
        return bool(gre.match(rel) or (match_base and gre.match(base)))

    return _match


def _make_substr_matcher(needle: str) -> Callable[[str, str], bool]:
    def _match(rel: str, _base: str) -> bool:
        return needle in rel.lower()

    return _match


def _build_glob_fallback(pattern: str) -> tuple[Callable[[str, str], bool], str, str] | None:
    if "{" not in pattern and "|" in pattern:
        terms = [p.strip() for p in pattern.split("|") if p.strip()]
        if len(terms) >= 2:
            fns: list[Callable[[str, str], bool]] = []
            for t in terms:
                if re.search(r"[*?\[\]{}]", t):
                    try:
                        gre = glob_to_regex(t)
                        fns.append(_make_glob_term_matcher(gre, "/" not in t))
                        continue
                    except ValueError:
                        pass
                fns.append(_make_substr_matcher(t.lower()))
            all_bare = all(not re.search(r"[*?\[\]{}/]", t) for t in terms)
            label = f"**/*{{{','.join(terms)}}}*" if all_bare else f"{{{','.join(terms)}}}"

            def _any_match(rel: str, base: str) -> bool:
                return any(fn(rel, base) for fn in fns)

            return (
                _any_match,
                f'[note: exact glob matched 0 files; auto-expanded regex \'|\' to "{label}" (glob uses * and {{a,b}}, not |)]',
                "(no files matched; hint: glob uses * and {a,b}, not regex |)",
            )
    if re.fullmatch(r"[A-Za-z0-9_-]+", pattern):
        needle = pattern.lower()
        return (
            _make_substr_matcher(needle),
            f'[note: exact glob "{pattern}" matched 0 files; showing case-insensitive path matches for "*{pattern}*"]',
            "(no files matched)",
        )
    return None


def _format_glob_walk(
    walked,
    search_root: str,
    base_cwd: str,
    max_results: int,
    sort_by: str | None,
    note_prefix: str = "",
) -> str:
    try:
        rel_prefix = to_posix_path(os.path.relpath(search_root, base_cwd))
    except ValueError:
        rel_prefix = ""
    sorted_files = sorted(walked.files)
    if sort_by == "mtime":
        mtimes: dict[str, float] = {}
        for f in sorted_files:
            try:
                mtimes[f] = os.lstat(os.path.join(search_root, f)).st_mtime
            except OSError:
                mtimes[f] = float("-inf")
        sorted_files.sort(key=lambda f: (-mtimes[f], f))
    items = [
        f if (not rel_prefix or rel_prefix == ".") else f"{rel_prefix}/{f}"
        for f in sorted_files[:max_results]
    ]
    trunc = walked.truncated or len(sorted_files) > max_results
    header = f"{note_prefix}\n" if note_prefix else ""
    if not trunc:
        return header + "\n".join(items)
    omitted = sorted_files[max_results:]
    dir_counts: dict[str, int] = {}
    for f in omitted:
        sl = f.find("/")
        bucket = "(root)" if sl == -1 else f"{f[:sl]}/"
        dir_counts[bucket] = dir_counts.get(bucket, 0) + 1
    top_dirs = [
        f"{d} ({c})" for d, c in sorted(dir_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
    ]
    total_lbl = f"{len(walked.files)}+" if walked.truncated else str(len(walked.files))
    dir_sum = f"; omitted dirs: {', '.join(top_dirs)}" if top_dirs else ""
    b_note = "; scan budget reached — narrow 'path' or 'glob'" if (walked.timed_out or walked.dirs_truncated) else ""
    return f"{header}" + "\n".join(items) + f"\n... [truncated at {max_results} of {total_lbl} files{dir_sum}{b_note}]"


def _run_glob_query(
    query: dict[str, Any],
    search_root: str,
    base_cwd: str,
    is_file: bool,
    call_deadline: float,
    walk_timeout_ms: int,
    walk_max_dirs: int,
) -> str:
    max_results = clamp_int(query.get("maxResults"), 1, 300, 80)
    pattern = str(query.get("pattern") or "").strip() or "**/*"
    matcher = glob_to_regex(pattern)
    match_base = "/" not in pattern
    inc_hidden = _pattern_targets_hidden(pattern)

    if is_file:
        rel = _relative_file(base_cwd, search_root)
        base = os.path.basename(search_root)
        if matcher.match(rel) or (match_base and matcher.match(base)):
            return rel
        fb = _build_glob_fallback(pattern)
        if fb:
            return f"{fb[1]}\n{rel}" if fb[0](rel, base) else fb[2]
        return "(no files matched)"

    rem_ms = int((call_deadline - time.monotonic()) * 1000)
    if rem_ms <= 0:
        return "(no files matched before scan budget reached — narrow 'path' or 'glob')"
    scan_limit = max(max_results + 1, 2000)
    walked = walk_files(
        search_root,
        max_files=scan_limit,
        include_hidden=inc_hidden,
        timeout_ms=min(walk_timeout_ms, rem_ms),
        max_dirs=walk_max_dirs,
        file_filter=lambda rel_p: bool(
            matcher.match(rel_p) or (match_base and matcher.match(rel_p.rsplit("/", 1)[-1]))
        ),
    )
    if walked.files:
        return _format_glob_walk(walked, search_root, base_cwd, max_results, query.get("sortBy"))
    if walked.timed_out or walked.dirs_truncated:
        return "(no files matched before scan budget reached — narrow 'path' or 'glob')"
    fb = _build_glob_fallback(pattern)
    if fb:
        rem_ms2 = int((call_deadline - time.monotonic()) * 1000)
        if rem_ms2 <= 0:
            return "(no files matched before scan budget reached — narrow 'path' or 'glob')"
        fb_walked = walk_files(
            search_root,
            max_files=scan_limit,
            include_hidden=inc_hidden,
            timeout_ms=min(walk_timeout_ms, rem_ms2),
            max_dirs=walk_max_dirs,
            file_filter=lambda rel_p: fb[0](rel_p, rel_p.rsplit("/", 1)[-1]),
        )
        if fb_walked.files:
            return _format_glob_walk(fb_walked, search_root, base_cwd, max_results, query.get("sortBy"), fb[1])
        return fb[2]
    return "(no files matched)"


def _run_fuzzy_query(
    query: dict[str, Any],
    search_root: str,
    base_cwd: str,
    is_file: bool,
    call_deadline: float,
    walk_timeout_ms: int,
    walk_max_dirs: int,
) -> str:
    pattern = str(query.get("pattern") or "").strip()
    if not pattern:
        raise ValueError("Fuzzy pattern must not be blank")
    matches_type = _file_type_matcher(query.get("fileType"))
    max_results = clamp_int(query.get("maxResults"), 1, 200, 30)
    if is_file:
        cands = [_relative_file(base_cwd, search_root)] if matches_type(search_root) else []
        trunc = False
        b_hit = False
    else:
        rem_ms = int((call_deadline - time.monotonic()) * 1000)
        if rem_ms <= 0:
            return "(no files matched before scan budget reached — narrow 'path')"
        walked = walk_files(
            search_root,
            max_files=8000,
            file_filter=matches_type,
            timeout_ms=min(walk_timeout_ms, rem_ms),
            max_dirs=walk_max_dirs,
        )
        cands = [_relative_file(base_cwd, os.path.join(search_root, f)) for f in walked.files if matches_type(f)]
        trunc = walked.truncated
        b_hit = walked.timed_out or walked.dirs_truncated
    matches, total = rank_fuzzy_paths(pattern, cands, max_results)
    if not matches:
        return "(no files matched before scan budget reached — narrow 'path')" if b_hit else "(no files matched)"
    body = "\n".join(m[0] for m in matches)
    b_note = "; scan budget reached — narrow 'path'" if b_hit else ""
    footer = f"\n... [truncated; showing {len(matches)} files{b_note}]" if (trunc or total > max_results) else ""
    return body + footer


def _run_git_query(query: dict[str, Any], search_root: str, call_deadline: float) -> str:
    validated = validate_readonly_git_args(str(query["pattern"]))
    exec_args = _harden_git_args(validated)
    rem_ms = int((call_deadline - time.monotonic()) * 1000)
    if rem_ms <= 0:
        raise RuntimeError("git query timed out")
    res = exec_binary(
        "git",
        exec_args,
        cwd=search_root,
        timeout_ms=max(250, min(10_000, rem_ms)),
        max_output_bytes=512 * 1024,
        env=with_non_interactive_git_env(),
    )
    if res.error:
        raise RuntimeError(f"git execution error: {res.error}")
    if res.timed_out:
        raise RuntimeError("git query timed out")
    if res.exit_code != 0:
        raise RuntimeError((res.stderr or res.stdout).strip() or f"git exited with code {res.exit_code}")
    return res.stdout.rstrip() or "(empty output)"


def _format_budgeted_content(
    header: str,
    files: list[FileMatches],
    total_matches: int,
    max_res_trunc: bool,
    char_budget: int,
    byte_budget: int,
) -> str | None:
    """File-coverage-first budgeted renderer so every matched file gets at least its first match line."""
    h_chars = len(header)
    h_bytes = utf8_len(header)
    if char_budget <= h_chars + 40 or byte_budget <= h_bytes + 40:
        return None
    states: list[tuple[str, list[str]]] = []
    for fm in files:
        m_lines = [
            f"  {ln.line_number}: {_format_line_snippet(ln)}" for ln in fm.lines if ln.is_match
        ]
        if m_lines:
            states.append((fm.file, m_lines))
    if not states:
        return None
    parts = [header]
    used_c = h_chars
    used_b = h_bytes
    shown_files = 0
    shown_matches = 0
    reserve_c = 90
    reserve_b = 90
    for fpath, m_lines in states:
        block = f"\n{fpath}\n{m_lines[0]}"
        if len(m_lines) > 1:
            block += f"\n  ... (+{len(m_lines) - 1} more)"
        bc = len(block)
        bb = utf8_len(block)
        if used_c + bc + reserve_c > char_budget or used_b + bb + reserve_b > byte_budget:
            break
        parts.append(block)
        used_c += bc
        used_b += bb
        shown_files += 1
        shown_matches += 1
    if shown_files == 0:
        return None
    if shown_files < len(states):
        parts.append(f'\n... [+{len(states) - shown_files} more files; use outputMode: "files_with_matches"]')
    tot_lbl = f"{total_matches}+" if max_res_trunc else str(total_matches)
    parts.append(f"\n... [truncated: showed {shown_matches} of {tot_lbl} matches across {shown_files} of {len(states)} files]")
    res = "".join(parts)
    return res if (len(res) <= char_budget and utf8_len(res) <= byte_budget) else None


def _execute_single_query(
    query: dict[str, Any],
    index: int,
    base_cwd: str,
    use_rg: bool,
    call_deadline: float,
    walk_timeout_ms: int,
    walk_max_dirs: int,
) -> tuple[str, str, tuple[list[FileMatches], int, bool] | None]:
    qtype = query.get("type")
    qpat = query.get("pattern")
    qpath = query.get("path")
    header = f"## Query {index + 1} [{qtype}] {qpat}" + (f" (in {qpath})" if qpath else "")
    try:
        if not isinstance(qpat, str) or not qpat:
            raise ValueError(
                with_validation_example("Query 'pattern' must be a non-empty string", TOOL_VALIDATION_EXAMPLES["batch_search"])
            )
        if query.get("multiline") is not None and not isinstance(query.get("multiline"), bool):
            raise ValueError(
                with_validation_example("multiline must be a boolean", TOOL_VALIDATION_EXAMPLES["batch_search"])
            )
        om = query.get("outputMode")
        if om is not None and om not in ("content", "files_with_matches", "count"):
            raise ValueError(
                with_validation_example("Invalid outputMode", TOOL_VALIDATION_EXAMPLES["batch_search"])
            )
        sb = query.get("sortBy")
        if sb is not None and sb not in ("path", "mtime"):
            raise ValueError(
                with_validation_example("Invalid sortBy", TOOL_VALIDATION_EXAMPLES["batch_search"])
            )
        search_root = resolve_path(str(qpath), base_cwd) if qpath else base_cwd
        if not os.path.exists(search_root):
            raise ValueError(f"Path does not exist: {qpath or base_cwd}")
        is_file = os.path.isfile(search_root)

        if qtype == "git":
            git_cwd = os.path.dirname(search_root) if is_file else search_root
            return header, f"{header}\n{_run_git_query(query, git_cwd, call_deadline)}", None
        if qtype == "glob":
            out = _run_glob_query(query, search_root, base_cwd, is_file, call_deadline, walk_timeout_ms, walk_max_dirs)
            return header, f"{header}\n{out}", None
        if qtype == "fuzzy":
            out = _run_fuzzy_query(query, search_root, base_cwd, is_file, call_deadline, walk_timeout_ms, walk_max_dirs)
            return header, f"{header}\n{out}", None
        if qtype in ("text", "regex"):
            if use_rg:
                try:
                    files, total, trunc, b_hit = _run_text_regex_rg(query, search_root, base_cwd, call_deadline)
                except RuntimeError as exc:
                    if str(exc).startswith("rg spawn error:"):
                        files, total, trunc, b_hit = _run_text_regex_python(
                            query, search_root, base_cwd, call_deadline, walk_timeout_ms, walk_max_dirs
                        )
                    else:
                        raise
            else:
                files, total, trunc, b_hit = _run_text_regex_python(
                    query, search_root, base_cwd, call_deadline, walk_timeout_ms, walk_max_dirs
                )
            b_note = "; scan budget reached — narrow 'path' or 'glob'" if b_hit else ""
            if om in ("count", "files_with_matches"):
                unit = "matches" if query.get("multiline") else "matching lines"
                body = "\n".join(
                    f"{fm.file}: {fm.count} {unit}" if om == "count" else fm.file for fm in files
                ) or ("(no matches before scan budget reached — narrow 'path' or 'glob')" if b_hit else "(no matches)")
                footer = f"\n... [truncated; showing {len(files)} files{b_note}]" if (trunc and files) else ""
                return header, f"{header}\n{body}{footer}", None
            if not files and b_hit:
                body = "(no matches before scan budget reached — narrow 'path' or 'glob')"
            else:
                body = _format_file_matches(files, total, trunc) + (
                    "\n... [scan budget reached — narrow 'path' or 'glob']" if (b_hit and files) else ""
                )
            return header, f"{header}\n{body}", ((files, total, trunc) if files else None)
        raise ValueError(
            with_validation_example(f"Unsupported query type: {qtype}", TOOL_VALIDATION_EXAMPLES["batch_search"])
        )
    except Exception as exc:
        return header, f"{header}\nERROR: {exc}", None


def run_batch_search(
    args: dict[str, Any],
    force_python_fallback: bool = False,
    call_timeout_ms: int = DEFAULT_CALL_TIMEOUT_MS,
    walk_timeout_ms: int = DEFAULT_WALK_TIMEOUT_MS,
    walk_max_dirs: int = DEFAULT_WALK_MAX_DIRS,
) -> dict[str, Any]:
    raw_q = args.get("queries")
    ok, kind, payload = parse_maybe_json_array(raw_q)
    if not ok:
        if kind == "corrupted_literal" or (kind == "wrong_type" and (isinstance(raw_q, bool) or raw_q is None)):
            return {
                "isError": True,
                "text": format_corrupted_array_error(
                    "Validation failed",
                    "batch_search",
                    [("queries", payload if kind == "corrupted_literal" else raw_q, "an array of query objects")],
                    "split into smaller queries or use a single compound read-only run_command.",
                ),
            }
        return {
            "isError": True,
            "text": with_validation_example(
                "Error: 'queries' must be a non-empty array of search queries.",
                TOOL_VALIDATION_EXAMPLES["batch_search"],
            ),
        }
    queries = payload
    if not queries:
        return {
            "isError": True,
            "text": with_validation_example(
                "Error: 'queries' must be a non-empty array of search queries.",
                TOOL_VALIDATION_EXAMPLES["batch_search"],
            ),
        }
    if len(queries) > 20:
        return {"isError": True, "text": "Error: 'queries' supports at most 20 queries per call."}
    corrupted = [
        (f"queries[{i}]", q, "a query object ({ type, pattern, ... })")
        for i, q in enumerate(queries)
        if not isinstance(q, dict)
    ]
    if corrupted:
        return {
            "isError": True,
            "text": format_corrupted_array_error(
                "Validation failed",
                "batch_search",
                corrupted,
                "split into smaller queries or use a single compound read-only run_command.",
            ),
        }

    base_cwd = resolve_path(str(args.get("cwd") or os.getcwd()))
    if not os.path.exists(base_cwd):
        return {"isError": True, "text": f"Error: cwd does not exist: {base_cwd}"}
    if not os.path.isdir(base_cwd):
        return {"isError": True, "text": f"Error: cwd is not a directory: {base_cwd}"}

    max_chars = clamp_int(args.get("maxChars"), 500, DEFAULT_INLINE_MAX_BYTES, 8_500)
    max_bytes = DEFAULT_INLINE_MAX_BYTES
    needs_text = any(q.get("type") in ("text", "regex") for q in queries)
    use_rg = needs_text and not force_python_fallback and is_ripgrep_available()
    deadline = time.monotonic() + max(1, call_timeout_ms) / 1000.0

    max_workers = min(8, len(queries))
    if max_workers == 1:
        executed = [_execute_single_query(queries[0], 0, base_cwd, use_rg, deadline, walk_timeout_ms, walk_max_dirs)]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = [
                pool.submit(_execute_single_query, q, idx, base_cwd, use_rg, deadline, walk_timeout_ms, walk_max_dirs)
                for idx, q in enumerate(queries)
            ]
            executed = [f.result() for f in futs]

    sep = "\n\n"
    raw_secs = [e[1] for e in executed]
    joined = sep.join(raw_secs)
    sep_chars = len(sep) * max(0, len(raw_secs) - 1)
    sep_bytes = utf8_len(sep) * max(0, len(raw_secs) - 1)

    final_secs = raw_secs
    if len(joined) > max_chars or utf8_len(joined) > max_bytes:
        raw_cl = [len(s) for s in raw_secs]
        raw_bl = [utf8_len(s) for s in raw_secs]
        b_budgets = (
            compute_water_fill_budgets(raw_bl, max(0, max_bytes - sep_bytes))
            if utf8_len(joined) > max_bytes
            else [max_bytes] * len(raw_secs)
        )
        c_budgets = compute_water_fill_budgets(raw_cl, max(0, max_chars - sep_chars))
        final_secs = []
        for idx, (hdr, txt, cres) in enumerate(executed):
            if cres and (raw_cl[idx] > c_budgets[idx] or raw_bl[idx] > b_budgets[idx]):
                b_txt = _format_budgeted_content(hdr, cres[0], cres[1], cres[2], c_budgets[idx], b_budgets[idx])
                final_secs.append(b_txt if b_txt else txt)
            else:
                final_secs.append(txt)

    return {
        "isError": False,
        "text": fit_sections_to_byte_budget(final_secs, max_chars, max_bytes, sep),
    }
