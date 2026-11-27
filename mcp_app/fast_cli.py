"""Direct CLI mode (`--cli`) for Swarm workers, scripts, and non-MCP environments.

Invoked via:
  python3 mcp_app/server.py --cli <search|outline|slice|edit|check> [flags...]

Incorporates engineering patterns adapted from INK-Q/agy-fast (MIT License).
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, TextIO

from fast_checks import run_checks
from fast_edits import run_apply_edits
from fast_outline import run_quick_outline, run_read_slices
from fast_search import run_batch_search

TOOL_ALIASES: dict[str, str] = {
    "search": "batch_search",
    "batch_search": "batch_search",
    "outline": "quick_outline",
    "quick_outline": "quick_outline",
    "slice": "read_slices",
    "slices": "read_slices",
    "read_slices": "read_slices",
    "edit": "apply_edits",
    "apply_edits": "apply_edits",
    "check": "run_checks",
    "checks": "run_checks",
    "run_checks": "run_checks",
}

_SEARCH_TYPES = frozenset({"text", "regex", "glob", "fuzzy", "git"})
_VALUE_FLAGS = frozenset(
    {
        "--cwd", "--max-chars", "--json", "--text", "--regex", "--glob", "--fuzzy", "--git",
        "--pattern", "--type", "--path", "--file-type", "--glob-filter", "--output-mode",
        "--sort-by", "--context", "--max-results", "--outline", "--concurrency", "--timeout",
        "--verify", "--verify-timeout", "--max-verify-chars",
    }
)


def format_cli_usage() -> str:
    return "\n".join(
        [
            "agy-tokensaver direct CLI mode (for Swarm workers, scripts, and non-MCP environments)",
            "",
            "Usage:",
            "  python3 <plugin>/mcp_app/server.py --cli <command> [options] [args...]",
            "",
            "Commands (aliases):",
            "  outline | quick_outline <file...> [--cwd <dir>] [--max-chars <N>]",
            "  slice   | read_slices   <path:start-end,...> [--outline <file>] [--cwd <dir>]",
            "  search  | batch_search  [--text <p>] [--regex <p>] [--glob <p>] [--fuzzy <p>] [--git <cmd>]",
            "                          [--path <dir>] [--file-type <t>] [--glob-filter <g>]",
            "                          [--output-mode content|files_with_matches|count] [--sort-by path|mtime]",
            "                          [--context <0..10>] [--max-results <N>] [-i|--ignore-case] [--multiline]",
            "  check   | run_checks    <cmd...> [--compact|--full] [--concurrency <N>] [--timeout <sec>]",
            "  edit    | apply_edits   [--best-effort] [--dry-run] [--verify <cmd>] (JSON via --json or stdin)",
            "",
            "JSON input (works for every command using the exact MCP tool schema):",
            "  --json '<json>'         Pass tool arguments JSON object (or shorthand array) inline",
            "  --stdin                 Read tool arguments JSON from stdin",
            "  --cwd <dir>             Override working directory",
            "  --max-chars <N>         Override output character cap (hard UTF-8 ceiling <= 8800 bytes)",
        ]
    )


def _parse_int_flag(flag: str, raw: str) -> int:
    if not re.fullmatch(r"-?\d+", raw.strip()):
        raise ValueError(f"Expected integer value after {flag}")
    return int(raw.strip())


def _parse_json_payload(raw_text: str, tool: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_text)
    except Exception as exc:
        raise ValueError(f"Invalid JSON input for '{tool}': {exc}") from exc
    if isinstance(parsed, list):
        if tool == "batch_search":
            return {"queries": parsed}
        if tool == "apply_edits":
            return {"edits": parsed}
        if tool == "quick_outline":
            return {"paths": parsed}
        if tool == "read_slices":
            if all(isinstance(x, str) for x in parsed):
                return {"lineRanges": parsed}
            return {"slices": parsed}
        if tool == "run_checks":
            return {"commands": [{"command": x} if isinstance(x, str) else x for x in parsed]}
    if isinstance(parsed, dict):
        return dict(parsed)
    raise ValueError(f"JSON input for '{tool}' must be an object (or shorthand array).")


def _apply_mods(q: dict[str, Any], mods: dict[str, Any]) -> dict[str, Any]:
    out = dict(q)
    for k, v in mods.items():
        if k not in out:
            out[k] = v
    return out


def should_cli_exit_nonzero(tool: str, result: dict[str, Any]) -> bool:
    if result.get("isError"):
        return True
    txt = str(result.get("text") or "")
    if tool == "run_checks" and re.match(r"^Checks:\s+\d+/\d+\s+passed,\s+(?:[1-9]\d*\s+failed|[1-9]\d*\s+not run)\b", txt):
        return True
    if tool == "apply_edits" and re.search(r"(?:^|\n)Verify \[(?:FAIL|TIMEOUT|ERROR)\b", txt):
        return True
    return False


def run_cli(
    argv: list[str],
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    default_cwd: str | None = None,
) -> int:
    in_s = stdin or sys.stdin
    out_s = stdout or sys.stdout
    err_s = stderr or sys.stderr
    base_cwd = default_cwd or os.getcwd()

    if not argv or argv[0] in ("--help", "-h", "help"):
        out_s.write(format_cli_usage() + "\n")
        return 0

    tool = TOOL_ALIASES.get(argv[0].strip().lower())
    if not tool:
        err_s.write(f"Error: unknown --cli command '{argv[0]}'. Expected one of: search, outline, slice, edit, check (or --help).\n")
        return 1

    rest = argv[1:]
    cwd_opt: str | None = None
    max_chars: int | None = None
    json_raw: str | None = None
    explicit_stdin = False
    positionals: list[str] = []
    search_queries: list[dict[str, Any]] = []
    implicit_type_ids: set[int] = set()
    outline_files: list[str] = []
    output_mode: str | None = None
    concurrency: int | None = None
    timeout_sec: int | None = None
    best_effort: bool | None = None
    dry_run: bool | None = None
    verify_cmd: str | None = None
    verify_timeout: int | None = None
    max_verify_chars: int | None = None

    pending_mods: dict[str, Any] = {}
    pending_type: str | None = None
    after_dd = False
    i = 0

    def _push_q(base: dict[str, Any], explicit: bool) -> None:
        nonlocal pending_mods, pending_type
        idx = len(search_queries)
        search_queries.append(_apply_mods(base, pending_mods))
        if not explicit:
            implicit_type_ids.add(idx)
        pending_mods = {}
        pending_type = None

    try:
        while i < len(rest):
            raw_arg = rest[i]
            i += 1
            if after_dd:
                if tool == "batch_search":
                    _push_q({"type": pending_type or "text", "pattern": raw_arg}, pending_type is not None)
                else:
                    positionals.append(raw_arg)
                continue
            if raw_arg == "--":
                after_dd = True
                continue
            arg = raw_arg
            inline_val: str | None = None
            if raw_arg.startswith("--") and "=" in raw_arg:
                cand, val = raw_arg.split("=", 1)
                if cand in _VALUE_FLAGS:
                    arg = cand
                    inline_val = val

            def _val() -> str:
                nonlocal i
                if inline_val is not None:
                    return inline_val
                if i >= len(rest):
                    raise ValueError(f"Missing value after {arg}")
                v = rest[i]
                i += 1
                return v

            if arg in ("--help", "-h"):
                out_s.write(format_cli_usage() + "\n")
                return 0
            if arg == "--cwd":
                cwd_opt = _val()
            elif arg == "--max-chars":
                max_chars = _parse_int_flag(arg, _val())
            elif arg == "--json":
                json_raw = _val()
            elif arg == "--stdin":
                explicit_stdin = True
            elif tool == "batch_search":
                if arg in ("--text", "-t"):
                    _push_q({"type": "text", "pattern": _val()}, True)
                elif arg in ("--regex", "-r"):
                    _push_q({"type": "regex", "pattern": _val()}, True)
                elif arg in ("--glob", "-g"):
                    _push_q({"type": "glob", "pattern": _val()}, True)
                elif arg in ("--fuzzy", "-f"):
                    _push_q({"type": "fuzzy", "pattern": _val()}, True)
                elif arg == "--git":
                    _push_q({"type": "git", "pattern": _val()}, True)
                elif arg == "--type":
                    rt = _val().strip().lower()
                    if rt not in _SEARCH_TYPES:
                        raise ValueError(f"Invalid --type: {rt}")
                    pending_type = rt
                elif arg == "--pattern":
                    _push_q({"type": pending_type or "text", "pattern": _val()}, pending_type is not None)
                elif arg in ("--path", "-p"):
                    pending_mods["path"] = _val()
                elif arg == "--file-type":
                    pending_mods["fileType"] = _val()
                elif arg == "--glob-filter":
                    pending_mods["glob"] = _val()
                elif arg == "--output-mode":
                    om = _val()
                    if om not in ("content", "files_with_matches", "count"):
                        raise ValueError(f"Invalid --output-mode: {om}")
                    pending_mods["outputMode"] = om
                elif arg == "--sort-by":
                    sb = _val()
                    if sb not in ("path", "mtime"):
                        raise ValueError(f"Invalid --sort-by: {sb}")
                    pending_mods["sortBy"] = sb
                elif arg in ("--context", "-C"):
                    pending_mods["contextLines"] = _parse_int_flag(arg, _val())
                elif arg == "--max-results":
                    pending_mods["maxResults"] = _parse_int_flag(arg, _val())
                elif arg in ("--ignore-case", "-i"):
                    pending_mods["caseSensitive"] = False
                elif arg == "--multiline":
                    pending_mods["multiline"] = True
                elif arg.startswith("-"):
                    raise ValueError(f"Unknown option '{raw_arg}' for '{tool}'.")
                else:
                    _push_q({"type": pending_type or "text", "pattern": raw_arg}, pending_type is not None)
            elif tool == "read_slices":
                if arg in ("--outline", "-o"):
                    outline_files.append(_val())
                elif arg.startswith("-"):
                    raise ValueError(f"Unknown option '{raw_arg}' for '{tool}'.")
                else:
                    positionals.append(raw_arg)
            elif tool == "run_checks":
                if arg == "--compact":
                    output_mode = "compact"
                elif arg == "--full":
                    output_mode = "full"
                elif arg == "--output-mode":
                    om = _val()
                    if om not in ("compact", "full"):
                        raise ValueError(f"Invalid --output-mode for check: {om}")
                    output_mode = om
                elif arg == "--concurrency":
                    concurrency = _parse_int_flag(arg, _val())
                elif arg == "--timeout":
                    timeout_sec = _parse_int_flag(arg, _val())
                elif arg.startswith("-"):
                    raise ValueError(f"Unknown option '{raw_arg}' for '{tool}'.")
                else:
                    positionals.append(raw_arg)
            elif tool == "apply_edits":
                if arg == "--best-effort":
                    best_effort = True
                elif arg == "--dry-run":
                    dry_run = True
                elif arg == "--verify":
                    verify_cmd = _val()
                elif arg == "--verify-timeout":
                    verify_timeout = _parse_int_flag(arg, _val())
                elif arg == "--max-verify-chars":
                    max_verify_chars = _parse_int_flag(arg, _val())
                elif arg.startswith("-"):
                    raise ValueError(f"Unknown option '{raw_arg}' for '{tool}'.")
                else:
                    positionals.append(raw_arg)
            else:
                if arg.startswith("-"):
                    raise ValueError(f"Unknown option '{raw_arg}' for '{tool}'.")
                positionals.append(raw_arg)
    except ValueError as exc:
        err_s.write(f"Error: {exc}\n")
        return 1

    has_shorthand = bool(positionals or search_queries or outline_files)
    json_input: dict[str, Any] | None = None
    try:
        if json_raw is not None:
            json_input = _parse_json_payload(json_raw, tool)
        elif explicit_stdin or (not has_shorthand and not in_s.isatty()):
            raw_in = in_s.read(2 * 1024 * 1024 + 1)
            if len(raw_in) > 2 * 1024 * 1024:
                raise ValueError("stdin payload exceeds 2MB limit")
            if raw_in.strip():
                json_input = _parse_json_payload(raw_in.strip(), tool)
    except ValueError as exc:
        err_s.write(f"Error: {exc}\n")
        return 1

    eff_cwd = cwd_opt or (json_input.get("cwd") if isinstance(json_input, dict) and isinstance(json_input.get("cwd"), str) else base_cwd)
    result: dict[str, Any]

    if tool == "batch_search":
        if json_input is not None:
            payload = {**json_input, "cwd": eff_cwd}
            if max_chars is not None:
                payload["maxChars"] = max_chars
        else:
            qs = [
                _apply_mods(
                    ({**q, "type": pending_type} if (pending_type and idx in implicit_type_ids) else q),
                    pending_mods,
                )
                for idx, q in enumerate(search_queries)
            ]
            if not qs:
                err_s.write("Error: 'search' requires at least one query (--text/--regex/--glob/--fuzzy/--git, --json, or stdin).\n")
                return 1
            payload = {"cwd": eff_cwd, "queries": qs}
            if max_chars is not None:
                payload["maxChars"] = max_chars
        result = run_batch_search(payload)
    elif tool == "quick_outline":
        if json_input is not None:
            payload = {**json_input, "cwd": eff_cwd}
        else:
            if not positionals:
                err_s.write("Error: 'outline' requires one or more file paths or JSON input.\n")
                return 1
            payload = {"cwd": eff_cwd, "paths": positionals}
        if max_chars is not None:
            payload["maxChars"] = max_chars
        result = run_quick_outline(payload)
    elif tool == "read_slices":
        if json_input is not None:
            payload = {**json_input, "cwd": eff_cwd}
        else:
            if not positionals and not outline_files:
                err_s.write("Error: 'slice' requires lineRanges specs (e.g. src/a.py:1-60), --outline <file>, or JSON input.\n")
                return 1
            payload = {"cwd": eff_cwd}
            if outline_files:
                payload["outlines"] = outline_files
            if positionals:
                payload["lineRanges"] = positionals
        if max_chars is not None:
            payload["maxChars"] = max_chars
        result = run_read_slices(payload)
    elif tool == "apply_edits":
        if json_input is None:
            err_s.write("Error: 'edit' requires JSON input via --json or stdin.\n")
            return 1
        payload = {**json_input, "cwd": eff_cwd}
        if best_effort is not None:
            payload["bestEffort"] = best_effort
        if dry_run is not None:
            payload["dryRun"] = dry_run
        if verify_cmd is not None:
            payload["verify"] = verify_cmd
        if verify_timeout is not None:
            payload["verifyTimeoutSec"] = verify_timeout
        if max_verify_chars is not None:
            payload["maxVerifyChars"] = max_verify_chars
        elif max_chars is not None:
            payload["maxVerifyChars"] = max(300, min(7500, max_chars))
        result = run_apply_edits(payload)
    else:
        if json_input is not None:
            payload = {**json_input, "cwd": eff_cwd}
        else:
            if not positionals:
                err_s.write("Error: 'check' requires one or more shell command strings or JSON input.\n")
                return 1
            payload = {"cwd": eff_cwd, "commands": [{"command": c} for c in positionals]}
        if output_mode is not None:
            payload["outputMode"] = output_mode
        if concurrency is not None:
            payload["concurrency"] = concurrency
        if timeout_sec is not None:
            payload["timeoutSec"] = timeout_sec
        if max_chars is not None:
            payload["maxChars"] = max_chars
        result = run_checks(payload)

    txt = str(result.get("text") or "")
    out_s.write(txt if txt.endswith("\n") else txt + "\n")
    return 1 if should_cli_exit_nonzero(tool, result) else 0
