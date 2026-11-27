"""Shared utilities for agy-tokensaver's batched local MCP accelerator tools.

Pure Python 3.9+ stdlib — zero runtime dependencies.
Implements UTF-8 byte-safe truncation & water-filling, ANSI/noise filtering and
head-tail failure compaction, argument validation & call_mcp_tool corruption
detection, glob/walk filesystem helpers, fuzzy path ranking, hardened subprocess
execution (with Windows PowerShell unwrapping), and Antigravity host compat.

Incorporates engineering patterns adapted from INK-Q/agy-fast (MIT License).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

# Antigravity spills MCP tool responses over ~10 KB UTF-8 to disk (`output.txt`),
# forcing an extra LLM turn to read them. Keeping responses <= 8,800 UTF-8 bytes
# guarantees inline delivery with headroom for JSON/timestamp wrappers.
DEFAULT_INLINE_MAX_BYTES = 8_800

# Antigravity aborts an MCP tool call after 180s and drops its result; share a
# 150s budget so remaining 30s cover process-tree kill grace and formatting.
MCP_CALL_BUDGET_SEC = 150

DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024  # 1 MiB

DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "dist",
        "build",
        "out",
        "target",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".cache",
        ".turbo",
        "coverage",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".gradle",
        ".idea",
        "vendor",
    }
)


# ---------------------------------------------------------------------------
# UTF-8 byte & char slicing + Head / Head-Tail truncation + Water-filling
# ---------------------------------------------------------------------------


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def slice_utf8_bytes(text: str, max_bytes: int) -> str:
    if max_bytes <= 0 or not text:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    end = max_bytes
    # Back off any UTF-8 continuation bytes (10xxxxxx)
    while end > 0 and (raw[end] & 0xC0) == 0x80:
        end -= 1
    return raw[:end].decode("utf-8", errors="ignore")


def slice_utf8_bytes_tail(text: str, max_bytes: int) -> str:
    if max_bytes <= 0 or not text:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    start = len(raw) - max_bytes
    # Skip forward past any leading UTF-8 continuation bytes (10xxxxxx)
    while start < len(raw) and (raw[start] & 0xC0) == 0x80:
        start += 1
    return raw[start:].decode("utf-8", errors="ignore")


def slice_safe_chars(text: str, max_chars: int) -> str:
    if max_chars <= 0 or not text:
        return ""
    return text[:max_chars] if len(text) > max_chars else text


def slice_safe_chars_tail(text: str, max_chars: int) -> str:
    if max_chars <= 0 or not text:
        return ""
    return text[-max_chars:] if len(text) > max_chars else text


def truncate_with_byte_notice(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    total_bytes = utf8_len(text)
    if total_bytes <= max_bytes:
        return text
    if max_bytes < 56:
        if max_bytes <= 3:
            return slice_utf8_bytes(text, max_bytes)
        return f"{slice_utf8_bytes(text, max_bytes - 3)}..."
    max_notice_bytes = utf8_len(f"\n... [truncated: showed {total_bytes} of {total_bytes} bytes]")
    cutoff = max(0, max_bytes - max_notice_bytes)
    if cutoff < 8:
        return f"{slice_utf8_bytes(text, max_bytes - 3)}..."
    sl = slice_utf8_bytes(text, cutoff)
    last_nl = sl.rfind("\n")
    if last_nl > 0 and utf8_len(sl[:last_nl]) > cutoff * 0.6:
        sl = sl[:last_nl]
    shown = utf8_len(sl)
    res = f"{sl}\n... [truncated: showed {shown} of {total_bytes} bytes]"
    return res if utf8_len(res) <= max_bytes else slice_utf8_bytes(res, max_bytes)


def truncate_head_tail_with_byte_notice(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    total_bytes = utf8_len(text)
    if total_bytes <= max_bytes:
        return text
    if max_bytes < 120:
        return truncate_with_byte_notice(text, max_bytes)
    max_marker = utf8_len(
        f"\n... [truncated: folded {total_bytes} middle bytes; total {total_bytes} bytes] ...\n"
    )
    content_bytes = max_bytes - max_marker
    if content_bytes < 40:
        return truncate_with_byte_notice(text, max_bytes)
    head_budget = max(16, int(content_bytes * 0.38))
    tail_budget = max(24, content_bytes - head_budget)

    head = slice_utf8_bytes(text, head_budget)
    last_nl = head.rfind("\n")
    if last_nl > 0 and utf8_len(head[:last_nl]) > head_budget * 0.5:
        head = head[:last_nl]

    tail = slice_utf8_bytes_tail(text, tail_budget)
    first_nl = tail.find("\n")
    if first_nl != -1 and utf8_len(tail[first_nl + 1 :]) > tail_budget * 0.5:
        tail = tail[first_nl + 1 :]

    omitted = max(1, total_bytes - utf8_len(head) - utf8_len(tail))
    marker = f"\n... [truncated: folded {omitted} middle bytes; total {total_bytes} bytes] ...\n"
    res = f"{head}{marker}{tail}"
    return res if utf8_len(res) <= max_bytes else truncate_with_byte_notice(res, max_bytes)


def truncate_with_notice(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars < 48:
        if max_chars <= 3:
            return slice_safe_chars(text, max_chars)
        return f"{slice_safe_chars(text, max_chars - 3)}..."
    max_notice = len(f"\n... [truncated: showed {len(text)} of {len(text)} chars]")
    cutoff = max(0, max_chars - max_notice)
    if cutoff < 8:
        return f"{slice_safe_chars(text, max_chars - 3)}..."
    sl = slice_safe_chars(text, cutoff)
    last_nl = sl.rfind("\n")
    if last_nl > cutoff * 0.6:
        sl = sl[:last_nl]
    res = f"{sl}\n... [truncated: showed {len(sl)} of {len(text)} chars]"
    return res if len(res) <= max_chars else slice_safe_chars(res, max_chars)


def truncate_head_tail_with_notice(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars < 120:
        return truncate_with_notice(text, max_chars)
    max_marker = len(
        f"\n... [truncated: folded {len(text)} middle chars; total {len(text)} chars] ...\n"
    )
    content_budget = max_chars - max_marker
    if content_budget < 40:
        return truncate_with_notice(text, max_chars)
    head_budget = max(16, int(content_budget * 0.38))
    tail_budget = max(24, content_budget - head_budget)

    head = slice_safe_chars(text, head_budget)
    last_nl = head.rfind("\n")
    if last_nl > head_budget * 0.5:
        head = head[:last_nl]

    tail = slice_safe_chars_tail(text, tail_budget)
    first_nl = tail.find("\n")
    if first_nl != -1 and len(tail) - (first_nl + 1) > tail_budget * 0.5:
        tail = tail[first_nl + 1 :]

    omitted = max(1, len(text) - len(head) - len(tail))
    marker = f"\n... [truncated: folded {omitted} middle chars; total {len(text)} chars] ...\n"
    res = f"{head}{marker}{tail}"
    return res if len(res) <= max_chars else truncate_with_notice(res, max_chars)


def compute_water_fill_budgets(sizes: Sequence[int], total_budget: int) -> list[int]:
    """Short items keep exact size and donate unused share to larger items."""
    n = len(sizes)
    if n == 0:
        return []
    budgets = [0] * n
    remaining = max(0, int(total_budget))
    if remaining <= 0:
        return budgets
    order = sorted(((max(0, int(s)), i) for i, s in enumerate(sizes)), key=lambda x: (x[0], x[1]))
    for pos, (size, idx) in enumerate(order):
        rem_cnt = n - pos
        share = remaining // rem_cnt
        if size <= share:
            budgets[idx] = size
            remaining -= size
        else:
            for pos2 in range(pos, n):
                rem2 = n - pos2
                alloc = remaining // rem2
                budgets[order[pos2][1]] = alloc
                remaining -= alloc
            break
    return budgets


def fit_sections_to_budget(
    sections: Sequence[str],
    max_chars: int,
    separator: str = "\n\n",
    truncate_mode: str = "head",
) -> str:
    if not sections or max_chars <= 0:
        return ""
    trunc = truncate_head_tail_with_notice if truncate_mode == "head-tail" else truncate_with_notice
    joined = separator.join(sections)
    if len(joined) <= max_chars:
        return joined
    sep_total = len(separator) * (len(sections) - 1)
    if sep_total >= max_chars:
        return trunc(joined, max_chars)
    budgets = compute_water_fill_budgets([len(s) for s in sections], max_chars - sep_total)
    fitted = separator.join(trunc(sec, budgets[i]) for i, sec in enumerate(sections))
    return fitted if len(fitted) <= max_chars else trunc(fitted, max_chars)


def fit_sections_to_byte_budget(
    sections: Sequence[str],
    max_chars: int,
    max_bytes: int | None = DEFAULT_INLINE_MAX_BYTES,
    separator: str = "\n\n",
    truncate_mode: str = "head",
) -> str:
    trunc_b = (
        truncate_head_tail_with_byte_notice
        if truncate_mode == "head-tail"
        else truncate_with_byte_notice
    )
    char_fitted = fit_sections_to_budget(sections, max_chars, separator, truncate_mode)
    if max_bytes is None:
        return char_fitted
    if max_bytes <= 0:
        return ""
    if utf8_len(char_fitted) <= max_bytes:
        return char_fitted
    sep_bytes = utf8_len(separator) * (len(sections) - 1)
    if sep_bytes >= max_bytes:
        return trunc_b(char_fitted, max_bytes)
    byte_budgets = compute_water_fill_budgets(
        [utf8_len(s) for s in sections], max_bytes - sep_bytes
    )
    byte_fitted_secs = [trunc_b(sec, byte_budgets[i]) for i, sec in enumerate(sections)]
    fitted = separator.join(byte_fitted_secs)
    if len(fitted) > max_chars:
        fitted = fit_sections_to_budget(byte_fitted_secs, max_chars, separator, truncate_mode)
    return fitted if utf8_len(fitted) <= max_bytes else trunc_b(fitted, max_bytes)


# ---------------------------------------------------------------------------
# Terminal output normalization, noise stripping, green folding, failure focus
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_LINE_ERASE_CSI_RE = re.compile(r"\x1b\[(?:2K|0?K)")
_LEADING_CLEAR_TO_EOL_RE = re.compile(r"^(?:\x1b\[0?K)+")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def normalize_terminal_output(raw: str) -> str:
    """Clean carriage-return progress overwrites and ANSI line-erase sequences."""
    raw_lines = raw.replace("\r\n", "\n").split("\n")
    out_lines: list[str] = []
    for raw_line in raw_lines:
        if not raw_line:
            out_lines.append("")
            continue
        had_erase = bool(_LINE_ERASE_CSI_RE.search(raw_line))
        working = raw_line
        last_full = working.rfind("\x1b[2K")
        if last_full != -1:
            working = working[last_full + 4 :]
        if "\r" not in working and not had_erase:
            out_lines.append(strip_ansi(working))
            continue
        current_frame = ""
        for idx, seg in enumerate(working.split("\r")):
            if idx > 0 and _LEADING_CLEAR_TO_EOL_RE.match(seg):
                seg = _LEADING_CLEAR_TO_EOL_RE.sub("", seg)
                current_frame = strip_ansi(seg)
                continue
            clean = strip_ansi(seg)
            if clean:
                current_frame = clean
        if had_erase and not current_frame:
            continue
        out_lines.append(current_frame)
    return "\n".join(out_lines).rstrip()


_ZERO_INDEXED_ENTITY_RE = re.compile(
    r"\b(?:worker|task|step|job|shard|rank|stage|attempt|node|process|test|check|thread|agent|container|pod|case|suite|phase|index|slot|cpu|gpu|core|port|channel|client|server|host|peer)(?:\s+#?|-)\s*$",
    re.I,
)
_BENIGN_ZERO_LEADING_RE = re.compile(
    r"\b(?:0|no)\s+(?:errors?|warnings?|fail(?:ed|ures?|ing)?)(?:\(s\))?(?=$|[\s,;.)\]|])",
    re.I,
)
_BENIGN_ZERO_TRAILING_RE = re.compile(
    r"\b(?:errors|error\(s\)|warnings|warning\(s\)|failures|failure\(s\)|failed|failing)\s*(?:(?::|=)\s*0(?=$|\s*(?:$|[,;)\]|]|\.(?!\d)|(?:\b(?:skipped|warnings?|errors?|failures?|total|ignored|pending)\b)))|\(\s*0\s*\))",
    re.I,
)


def strip_benign_zero_counts(line: str) -> str:
    def _repl(m: re.Match[str]) -> str:
        prefix = line[max(0, m.start() - 24) : m.start()]
        return m.group(0) if _ZERO_INDEXED_ENTITY_RE.search(prefix) else " "

    after = _BENIGN_ZERO_LEADING_RE.sub(_repl, line)
    return _BENIGN_ZERO_TRAILING_RE.sub(" ", after)


_ERROR_LINE_RE = re.compile(
    r"(?:\berr!|\b(?:errors?|fail(?:ed|ing|ure|ures)?|exception|traceback|panic|fatal|assertion|syntaxerror|typeerror|referenceerror|zoderror|validationerror|ts\d{4}|e\d{4}|cannot find|not found|unexpected|undefined)\b|^\s*(?:not ok\b|[✖✕✘✗×]\s+\S|●\s+(?!Console\s*$|\((?:SSG|Static|Dynamic|ISR)\)(?:\s|$))\S)|caused by:|application failed to start|compilation error|<<<\s*(?:failure|error))",
    re.I,
)
_ROOT_CAUSE_HEADER_RE = re.compile(
    r"(?:^\s*(?:description|action):|caused by:|application failed to start|⎯\s*failed tests|compilation error)",
    re.I,
)
_FOLD_DANGER_RE = re.compile(
    r"(?:\berr!|\b(?:errors?|fail(?:ed|ing|ure|ures)?|warns?|warnings?|exceptions?|tracebacks?|panics?|fatals?|assertions?|syntaxerror|typeerror|referenceerror|zoderror|validationerror|ts\d{4}|e\d{4}|cannot find|not found|unexpected|undefined|compilation error|killed|sigkill|sigsegv|sigabrt|oom|out of memory|timed out|timeout|refused|denied|permission|unhandled|mismatched|mismatch)\b|\bexit\s+(?:status|code)\s+[1-9]\d*\b|caused by:|application failed to start|<<<\s*(?:failure|error)|[✖✕✘✗×]\s+)",
    re.I,
)

_JSON_INFO_LOG_RES = (
    re.compile(r'^\s*\{"level":(?:10|20|30)\b'),
    re.compile(r'^\s*\{[^}]*"level"\s*:\s*"(?:trace|debug|info)"', re.I),
)
_JSON_ERR_RES = re.compile(r'"statusCode"\s*:\s*5\d\d|\b(?:error|exception|failed|fatal)\b', re.I)
_ALWAYS_NOISE_RES = (
    re.compile(
        r"^\[ERROR\]\s*(?:To see the full stack trace|Re-run Maven using the -X|For more information about the errors|\[Help \d+\]|->\s*\[Help \d+\])",
        re.I,
    ),
    re.compile(r"^\(node:\d+\)\s*ExperimentalWarning:"),
    re.compile(r"^\(Use `node --trace-warnings"),
)
_SUMMARY_LINE_RE = re.compile(
    r"\b(passed|tests?|specs?|suites?|ok|warnings?|errors?|built in|compiled|done in|finished)\b",
    re.I,
)
_MONOREPO_PREFIX_RE = re.compile(
    r"^(?:@[\w.-]+\/)?[\w./-]+(?::[A-Za-z][\w.-]*|\s+[A-Za-z][\w.-]*):\s+"
)

_GREEN_BUILD_RES = (
    re.compile(r"^(?:vite|rollup|esbuild|webpack|rspack|parcel|tsup|rolldown)\s+v?\d[\w.-]*\b", re.I),
    re.compile(r"^transforming(?:\.\.\.|\s*\(\d+\).*|\s+\d+\s+modules?)?$", re.I),
    re.compile(r"^(?:[✓✔]\s*)?\d+\s+modules?\s+transformed\.?$", re.I),
    re.compile(r"^(?:rendering chunks|computing (?:gzip|brotli) size|building (?:for |SSR )?production|bundling|minifying|emitting assets)(?:\.{0,3})$", re.I),
    re.compile(r"^(?:\.\.?\/)?(?:dist|build|out|\.next|\.output|public|assets|lib)\/\S+\s+\d+(?:\.\d+)?\s*(?:B|kB|KB|MB|GB|KiB|MiB|bytes)\b", re.I),
    re.compile(r"^\S+\.(?:m?js|cjs|css|html|svg|png|woff2?|json|map|wasm)\s+\d+(?:\.\d+)?\s*(?:B|kB|KB|MB|GB|KiB|MiB)(?:\s+[│|]\s*(?:gzip|brotli|map):|\s*$)", re.I),
    re.compile(r"^(?:[✓✔⚡]\s*)?built\b.*(?:\bin\s{1,4}\d+(?:\.\d+)?\s{0,4}(?:ms|s|m)\b|\(\s{0,4}\d+(?:\.\d+)?\s{0,4}(?:B|KB|kB|MB|GB|KiB|MiB)\s{0,4}\))", re.I),
    re.compile(r"^(?:[✓✔⚡]\s*)?(?:done in\s+\d+(?:\.\d+)?\s*(?:ms|s|m)\.?|build (?:succeeded|completed|finished)\b|compiled successfully\b|bundle created\b)", re.I),
    re.compile(r"^(?:asset|chunk|orphan modules|runtime modules|cacheable modules)\s+\S.*\b(?:bytes|KiB|MiB|kB|KB|MB|modules?)\b", re.I),
    re.compile(r"^>\s+(?:@[\w.-]+\/)?[\w.-]+@[\w.+-]+(?:\s|$)"),
    re.compile(r"^>\s+(?:vite|tsc|esbuild|rollup|webpack|rspack|tsup|unbuild|next|nuxt|astro|svelte-kit|vitest|jest|mocha|pnpm|npm|yarn|turbo|nx|node|tsx)\b"),
    re.compile(r"^(?:@[\w.-]+\/)?[\w./-]+\s+[A-Za-z][\w.-]*\$\s+\S"),
    re.compile(r"^Scope:\s+\d+\s+of\s+\d+\s+workspace projects\b", re.I),
    re.compile(r"^Tasks:\s+\d+\s+successful\b", re.I),
    re.compile(r"^Cached:\s+\d+\s+cached\b", re.I),
    re.compile(r"^cache hit,\s+replaying logs\b", re.I),
    re.compile(r"^Time:\s+\d+(?:\.\d+)?\s*(?:ms|s|m)\b", re.I),
    re.compile(r"^(?:yarn run v\d|•\s+Packages in scope:|•\s+Running\s+\S+\s+in\s+\d+\s+packages)", re.I),
    re.compile(r"^[✓✔]\s+\S"),
    re.compile(r"^PASS\s+\S"),
    re.compile(r"^(?:---\s+PASS:\s*|===\s+(?:RUN|PAUSE|CONT)\s+)\S"),
    re.compile(r"^ok\t\S"),
    re.compile(r"^ok\s+\S+\s+(?:\d+(?:\.\d+)?s|\(cached\))", re.I),
    re.compile(r"^ok\s+\d+\s+-\s+\S"),
    re.compile(r"^test\s+\S+\s+\.\.\.\s+ok\b"),
    re.compile(r"^running\s+\d+\s+tests?$", re.I),
    re.compile(r"^(?:Test Files|Tests|Suites|Snapshots|Start at|Duration|test result:\s{0,4}ok\.)\s.*(?:\bpassed\b|\btotal\b|\b\d+(?:\.\d+)?(?:ms|s)\b|\bok\.)", re.I),
    re.compile(r"^(?:Compiling|Checking|Downloading|Downloaded|Updating|Fresh|Locking|Packaging|Documenting|Finished)\s+\S"),
    re.compile(r"^go:\s+downloading\s+\S"),
    re.compile(r"^>\s+Task\s+:[\w:.-]+\s+(?:UP-TO-DATE|FROM-CACHE|NO-SOURCE|SKIPPED)\s*$"),
    re.compile(
        r"^\[INFO\]\s*(?:---\s+\S|Building\s+\S|Compiling\s+\S|Changes detected|Nothing to compile|Copying\s+\d+|Using\s+'[^']+'\s+encoding|Downloading\b|Downloaded\b|Installing\s+\S|Packaging\s+\S|Tests run:\s*\d+,\s*Failures:\s*0,\s*Errors:\s*0\b|Results:|Running\s+\S|Reactor Summary|BUILD SUCCESS|Total time:|Finished at:|-+\s*$|$)",
        re.I,
    ),
)

_UNAMBIGUOUS_PASS_RES = (
    re.compile(r"^[✓✔]\s+(?!built\b|done\b|\d+\s+modules?\s+transformed\b)\S"),
    re.compile(r"^---\s+PASS:\s+\S"),
    re.compile(r"^ok\s+\d+\s+-\s+\S"),
    re.compile(r"^ok\t\S"),
    re.compile(r"^test\s+\S+\s+\.\.\.\s+ok\s*$"),
    re.compile(r"^PASS\s+\S+\.(?:[cm]?[jt]sx?|py|go|rs|rb|java|kt|cs|php|vue|svelte)\b"),
)

_MIN_FOLD_RUN_LINES = 4
_PROTECTED_TAIL_LINES = 15


def is_error_line(line: str) -> bool:
    return bool(_ERROR_LINE_RE.search(strip_benign_zero_counts(line)))


def has_fold_danger_token(line: str) -> bool:
    cleaned = strip_benign_zero_counts(line)
    return bool(_ERROR_LINE_RE.search(cleaned) or _FOLD_DANGER_RE.search(cleaned))


def is_failure_noise_line(line: str) -> bool:
    if any(r.search(line) for r in _ALWAYS_NOISE_RES):
        return True
    if any(r.search(line) for r in _JSON_INFO_LOG_RES):
        return not _JSON_ERR_RES.search(strip_benign_zero_counts(line))
    return False


def _is_unambiguous_pass(trimmed: str) -> bool:
    if not trimmed or re.search(r"[✖✕✘✗×]", trimmed):
        return False
    return any(r.search(trimmed) for r in _UNAMBIGUOUS_PASS_RES)


def _is_green_build_or_pass_line(line: str) -> bool:
    trimmed = line.strip()
    if not trimmed:
        return False
    if _is_unambiguous_pass(trimmed):
        return True
    m = _MONOREPO_PREFIX_RE.match(trimmed)
    inner = trimmed[m.end() :].strip() if m else ""
    if inner and _is_unambiguous_pass(inner):
        return True
    if has_fold_danger_token(trimmed):
        return False
    if any(r.search(trimmed) for r in _GREEN_BUILD_RES):
        return True
    if m and inner:
        if inner == "Done":
            return True
        return any(r.search(inner) for r in _GREEN_BUILD_RES)
    return False


def fold_green_build_phase_lines(lines: list[str]) -> tuple[list[str], int]:
    foldable_end = max(0, len(lines) - _PROTECTED_TAIL_LINES)
    if foldable_end < _MIN_FOLD_RUN_LINES:
        return lines, 0
    out: list[str] = []
    collapsed = 0
    i = 0
    while i < foldable_end:
        if not _is_green_build_or_pass_line(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        start = i
        last_green = i
        green_count = 1
        i += 1
        while i < foldable_end:
            cur = lines[i]
            if _is_green_build_or_pass_line(cur):
                last_green = i
                green_count += 1
                i += 1
            elif not cur.strip():
                i += 1
            else:
                break
        if green_count >= _MIN_FOLD_RUN_LINES:
            run_len = last_green - start + 1
            preview = lines[last_green].strip()
            if len(preview) > 120:
                preview = f"{preview[:117]}..."
            placeholder = f"... (collapsed {run_len} build/pass lines; last: {preview})"
            orig_chars = (run_len - 1) + sum(len(lines[j]) for j in range(start, last_green + 1))
            if len(placeholder) < orig_chars:
                out.append(placeholder)
                collapsed += run_len
            else:
                out.extend(lines[start : last_green + 1])
            i = last_green + 1
        else:
            out.extend(lines[start : last_green + 1])
            i = last_green + 1
    out.extend(lines[foldable_end:])
    return out, collapsed


def _omitted_marker(omitted: int, mark_truncated: bool) -> str:
    return (
        f"... [truncated: {omitted} lines omitted] ..."
        if mark_truncated
        else f"... ({omitted} lines omitted) ..."
    )


def _render_selected(lines: list[str], indices: list[int], leading: bool, mark_trunc: bool) -> str:
    if not indices:
        return ""
    out: list[str] = []
    if leading and indices[0] > 0:
        out.append(_omitted_marker(indices[0], mark_trunc))
    prev = -1
    for idx in indices:
        if prev != -1 and idx > prev + 1:
            out.append(_omitted_marker(idx - prev - 1, mark_trunc))
        out.append(lines[idx])
        prev = idx
    return "\n".join(out)


def _pack_tail_indices(
    lines: list[str], candidates: list[int], budget: int, leading: bool, mark_trunc: bool
) -> tuple[list[int], str]:
    if not candidates or budget <= 0:
        return [], ""
    chosen_rev: list[int] = []
    used = 0
    for idx in reversed(candidates):
        line_len = len(lines[idx])
        if chosen_rev:
            next_idx = chosen_rev[-1]
            if next_idx > idx + 1:
                add = line_len + 1 + len(_omitted_marker(next_idx - idx - 1, mark_trunc)) + 1
            else:
                add = line_len + 1
        else:
            add = line_len
        hdr_len = (len(_omitted_marker(idx, mark_trunc)) + 1) if (leading and idx > 0) else 0
        if used + add + hdr_len <= budget:
            chosen_rev.append(idx)
            used += add
        else:
            break
    indices = list(reversed(chosen_rev))
    return indices, _render_selected(lines, indices, leading, mark_trunc)


def _compact_single_oversized_line(line: str, line_idx: int, limit: int) -> str:
    raw_prefix = f"{_omitted_marker(line_idx, True)}\n" if line_idx > 0 else ""
    prefix = raw_prefix if (limit - len(raw_prefix) >= 40) else ""
    avail = limit - len(prefix)
    if len(line) <= avail:
        return f"{prefix}{line}"
    max_marker = f" ... [truncated: folded {len(line)} chars] ... "
    if avail >= len(max_marker) + 24:
        c_avail = avail - len(max_marker)
        h_chars = max(8, int(c_avail * 0.28))
        t_chars = max(16, c_avail - h_chars)
        head = slice_safe_chars(line, h_chars)
        tail = slice_safe_chars_tail(line, t_chars)
        omitted = max(1, len(line) - len(head) - len(tail))
        return f"{prefix}{head} ... [truncated: folded {omitted} chars] ... {tail}"
    if avail <= 7:
        return f"{prefix}{slice_safe_chars_tail(line, avail)}"
    c_avail = avail - 5
    h_chars = max(1, int(c_avail * 0.28))
    t_chars = max(1, c_avail - h_chars)
    return f"{prefix}{slice_safe_chars(line, h_chars)} ... {slice_safe_chars_tail(line, t_chars)}"


def compact_long_failure_lines(lines: list[str], max_chars: int) -> str:
    if max_chars <= 0 or not lines:
        return ""
    full = "\n".join(lines)
    if len(full) <= max_chars:
        return full
    if max_chars < 80:
        if max_chars <= 3:
            return slice_safe_chars_tail(full, max_chars)
        return f"...{slice_safe_chars_tail(full, max_chars - 3)}"

    first_err = -1
    keep: set[int] = set()
    for i, line in enumerate(lines):
        trimmed = line.strip()
        m = _MONOREPO_PREFIX_RE.match(trimmed)
        inner = trimmed[m.end() :].strip() if m else ""
        if _is_unambiguous_pass(trimmed) or (inner and _is_unambiguous_pass(inner)):
            continue
        if _ROOT_CAUSE_HEADER_RE.search(line):
            if first_err == -1:
                first_err = i
            for j in range(max(0, i - 1), min(len(lines), i + 9)):
                keep.add(j)
        elif is_error_line(line) or (inner and m and not re.search(r"\s", m.group(0).rstrip()) and is_error_line(inner)):
            if first_err == -1:
                first_err = i
            for j in range(max(0, i - 1), min(len(lines), i + 4)):
                keep.add(j)

    for i in range(max(0, len(lines) - _PROTECTED_TAIL_LINES), len(lines)):
        keep.add(i)

    if first_err == -1:
        _, tail_txt = _pack_tail_indices(lines, list(range(len(lines))), max_chars, True, True)
        if tail_txt:
            return tail_txt
        return _compact_single_oversized_line(lines[-1], len(lines) - 1, max_chars)

    cands = sorted(keep)
    full_cands = _render_selected(lines, cands, True, True)
    if len(full_cands) <= max_chars:
        return full_cands

    tail_ids, tail_txt = _pack_tail_indices(lines, cands, max_chars, True, True)
    if tail_ids and tail_ids[0] <= first_err:
        return tail_txt

    if first_err == cands[-1]:
        return _compact_single_oversized_line(lines[first_err], first_err, max_chars)

    max_sep = len(f"\n{_omitted_marker(len(lines), True)}\n")
    head_budget = int(max(0, max_chars - max_sep) * 0.28)
    first_pos = cands.index(first_err)
    head_ids: list[int] = []
    if head_budget >= 24:
        prev_idx = first_err - 1
        if prev_idx >= 0:
            lead = (len(_omitted_marker(prev_idx, True)) + 1) if prev_idx > 0 else 0
            if len(lines[prev_idx]) <= int(head_budget * 0.35) and lead + len(lines[prev_idx]) + 1 + len(lines[first_err]) <= head_budget:
                head_ids.append(prev_idx)
        for pos in range(first_pos, len(cands)):
            idx = cands[pos]
            if head_ids and idx > head_ids[-1] + 1:
                break
            trial = head_ids + [idx]
            if len(_render_selected(lines, trial, True, True)) <= head_budget:
                head_ids.append(idx)
            else:
                break

    if not head_ids:
        rem_cands = [idx for idx in cands if idx > first_err]
        max_tail_b = max(0, max_chars - max(40, head_budget) - max_sep)
        t_ids, t_txt = _pack_tail_indices(lines, rem_cands, max_tail_b, False, True)
        if t_ids:
            omitted = t_ids[0] - first_err - 1
            sep = f"\n{_omitted_marker(omitted, True)}\n" if omitted > 0 else "\n"
            avail_first = max_chars - len(t_txt) - len(sep)
            if avail_first >= 24:
                first_r = _compact_single_oversized_line(lines[first_err], first_err, avail_first)
                comb = f"{first_r}{sep}{t_txt}"
                return comb if len(comb) <= max_chars else slice_safe_chars(comb, max_chars)
        return tail_txt or _compact_single_oversized_line(lines[first_err], first_err, max_chars)

    head_txt = _render_selected(lines, head_ids, True, True)
    head_last = head_ids[-1]
    rem_cands = [idx for idx in cands if idx > head_last]
    tail_budget = max(0, max_chars - len(head_txt) - max_sep)
    t_ids, t_txt = _pack_tail_indices(lines, rem_cands, tail_budget, False, True)
    if t_ids:
        omitted = t_ids[0] - head_last - 1
        sep = f"\n{_omitted_marker(omitted, True)}\n" if omitted > 0 else "\n"
        comb = f"{head_txt}{sep}{t_txt}"
        return comb if len(comb) <= max_chars else slice_safe_chars(comb, max_chars)
    return head_txt if len(head_txt) <= max_chars else slice_safe_chars(head_txt, max_chars)


def format_exit_failure_label(exit_code: int | None, sig: str | None = None) -> str:
    if sig:
        labels = {
            "SIGKILL": "SIGKILL - possible OOM",
            "SIGSEGV": "SIGSEGV - segmentation fault",
            "SIGABRT": "SIGABRT - process aborted",
            "SIGTERM": "SIGTERM",
        }
        return labels.get(sig, sig)
    fatal = {
        137: "exit 137 / SIGKILL - possible OOM",
        139: "exit 139 / SIGSEGV - segmentation fault",
        134: "exit 134 / SIGABRT - process aborted",
        0xC0000005: "exit 0xC0000005 / STATUS_ACCESS_VIOLATION",
        -1073741819: "exit 0xC0000005 / STATUS_ACCESS_VIOLATION",
        0xC00000FD: "exit 0xC00000FD / STATUS_STACK_OVERFLOW",
        -1073741571: "exit 0xC00000FD / STATUS_STACK_OVERFLOW",
    }
    if exit_code is not None and exit_code in fatal:
        return fatal[exit_code]
    return f"exit {exit_code if exit_code is not None else 'err'}"


@dataclass
class PreparedCompactOutput:
    kind: str  # error | timeout | pass-empty | pass-summary | fail-empty | fail-lines
    body_text: str
    folded_lines: list[str]
    collapsed_lines: int
    raw_lines: int
    raw_chars: int


def prepare_compact_command_output(
    stdout: str,
    stderr: str,
    exit_code: int | None,
    timed_out: bool = False,
    error: str | None = None,
    sig: str | None = None,
) -> PreparedCompactOutput:
    out = normalize_terminal_output(stdout)
    err = normalize_terminal_output(stderr)
    combined = "\n".join(p for p in (out, err) if p)
    raw_chars = len(combined)
    raw_lines = combined.count("\n") + 1 if combined else 0

    if error:
        return PreparedCompactOutput("error", f"spawn error: {error}", [], 0, raw_lines, raw_chars)
    if timed_out:
        tail = "\n".join(combined.split("\n")[-20:]) if combined else ""
        body = f"TIMED OUT\n{tail}" if tail else "TIMED OUT (no output)"
        return PreparedCompactOutput("timeout", body, [], 0, raw_lines, raw_chars)
    if exit_code == 0:
        if not combined:
            return PreparedCompactOutput("pass-empty", "OK", [], 0, 0, 0)
        lines = [ln.strip() for ln in combined.split("\n") if ln.strip()]
        summary = [ln for ln in lines if _SUMMARY_LINE_RE.search(ln)]
        picked = summary[-2:] if summary else (lines if len(lines) <= 2 else [lines[-1]])
        return PreparedCompactOutput("pass-summary", " | ".join(picked), [], 0, raw_lines, raw_chars)
    if not combined:
        lbl = format_exit_failure_label(exit_code, sig)
        body = f"Terminated ({lbl})" if (sig or "exit " not in lbl) else f"Exited with code {exit_code if exit_code is not None else 'unknown'}"
        return PreparedCompactOutput("fail-empty", body, [], 0, 0, 0)

    split_lines = combined.split("\n")
    filtered = [ln for ln in split_lines if not is_failure_noise_line(ln)]
    cleaned = filtered if filtered else split_lines
    folded, collapsed = fold_green_build_phase_lines(cleaned)
    return PreparedCompactOutput("fail-lines", "\n".join(folded), folded, collapsed, raw_lines, raw_chars)


def render_prepared_compact_command_output(prepared: PreparedCompactOutput, max_chars: int) -> str:
    if prepared.kind in ("error", "fail-empty", "pass-empty"):
        return truncate_with_notice(prepared.body_text, max_chars)
    if prepared.kind == "timeout":
        return truncate_head_tail_with_notice(prepared.body_text, max_chars)
    if prepared.kind == "pass-summary":
        return truncate_with_notice(prepared.body_text, min(max_chars, 320))
    if len(prepared.body_text) <= max_chars:
        return prepared.body_text
    return compact_long_failure_lines(prepared.folded_lines, max_chars)


def format_compact_command_output(
    stdout: str,
    stderr: str,
    exit_code: int | None,
    max_chars: int,
    timed_out: bool = False,
    error: str | None = None,
    sig: str | None = None,
) -> str:
    prep = prepare_compact_command_output(stdout, stderr, exit_code, timed_out, error, sig)
    return render_prepared_compact_command_output(prep, max_chars)


# ---------------------------------------------------------------------------
# Argument validation & call_mcp_tool serialization-corruption detection
# ---------------------------------------------------------------------------

TOOL_VALIDATION_EXAMPLES: dict[str, str] = {
    "batch_search": '{"queries":[{"type":"text","pattern":"foo","path":"src"}]}',
    "apply_edits": '{"edits":[{"path":"src/app.ts","hunks":[{"target":"old","replacement":"new"}]}]}',
    "read_slices": '{"outlines":["src/app.ts"],"lineRanges":["src/app.ts:1-60"]}',
    "read_slices_line_range": '{"lineRanges":["src/app.ts:1-60"]}',
    "quick_outline": '{"paths":["src/app.ts"]}',
    "run_checks": '{"commands":[{"name":"test","command":"npm test"}]}',
}


def with_validation_example(message: str, example_json: str) -> str:
    return f"{message}\nExample: {example_json}"


def describe_value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"boolean ({str(value).lower()})"
    if isinstance(value, list):
        return f"array (len={len(value)})"
    if isinstance(value, (int, float)):
        return f"number ({value})"
    if isinstance(value, str):
        return f"string (len={len(value)})"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def parse_maybe_json_array(value: Any) -> tuple[bool, str, Any]:
    """Return (ok, kind_or_detail, parsed_list_or_corrupted_val)."""
    if value is None:
        return False, "wrong_type", "null"
    if isinstance(value, list):
        return True, "ok", value
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return False, "wrong_type", "empty string"
        try:
            parsed = json.loads(trimmed)
        except Exception as exc:
            return False, "invalid_json", str(exc)
        if isinstance(parsed, list):
            return True, "ok", parsed
        if parsed is None or isinstance(parsed, bool):
            return False, "corrupted_literal", parsed
        return False, "invalid_json", f"decoded to {describe_value_type(parsed)} instead of a JSON array"
    return False, "wrong_type", describe_value_type(value)


def parse_maybe_string_or_json_array(value: Any) -> tuple[bool, str, Any]:
    ok, kind, payload = parse_maybe_json_array(value)
    if ok or kind == "corrupted_literal":
        return ok, kind, payload
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return False, kind, payload
        if trimmed.startswith(("[", "{", "```")) or "\n" in trimmed or "\r" in trimmed:
            return False, kind, payload
        if kind == "invalid_json" and isinstance(payload, str) and payload.startswith("decoded to "):
            return False, kind, payload
        return True, "ok", [trimmed]
    return False, kind, payload


def format_corrupted_array_error(
    header: str,
    tool_name: str,
    items: list[tuple[str, Any, str]],
    fallback_advice: str,
) -> str:
    details = [
        f"- {label}: received {describe_value_type(val)} instead of {expected}"
        for label, val, expected in items[:8]
    ]
    if len(items) > 8:
        details.append(f"- ... (+{len(items) - 8} more corrupted entries)")
    return "\n".join(
        [
            f"{header}: nested argument serialization corruption detected in call_mcp_tool.",
            *details,
            "",
            f"Hint: untyped call_mcp_tool Arguments can occasionally collapse nested objects/arrays into booleans, null, or an empty array. Do NOT retry the same {tool_name} payload — {fallback_advice}",
        ]
    )


def clamp_int(val: Any, lo: int, hi: int, fallback: int) -> int:
    if isinstance(val, bool) or val is None:
        return fallback
    if isinstance(val, int):
        return max(lo, min(hi, val))
    if isinstance(val, float):
        return max(lo, min(hi, int(val)))
    if isinstance(val, str) and val.strip():
        try:
            return max(lo, min(hi, int(float(val.strip()))))
        except ValueError:
            return fallback
    return fallback


# ---------------------------------------------------------------------------
# Filesystem, binary/BOM detection, glob compilation, bounded walk
# ---------------------------------------------------------------------------


def expand_home(input_path: str) -> str:
    if input_path == "~":
        return str(Path.home())
    if input_path.startswith(("~/", "~\\")):
        return os.path.join(str(Path.home()), input_path[2:])
    return input_path


def resolve_path(input_path: str, base_dir: str | None = None) -> str:
    expanded = expand_home(input_path.strip())
    root = os.path.abspath(expand_home(base_dir)) if base_dir else os.getcwd()
    return os.path.abspath(os.path.join(root, expanded))


def to_posix_path(p: str) -> str:
    return p.replace("\\", "/")


def is_likely_binary(buf: bytes) -> bool:
    return b"\x00" in buf[:8192]


def has_utf8_bom(buf: bytes) -> bool:
    return len(buf) >= 3 and buf[:3] == b"\xef\xbb\xbf"


def detect_utf16_bom(buf: bytes) -> str | None:
    if len(buf) < 2:
        return None
    if buf[:2] == b"\xff\xfe":
        if len(buf) >= 4 and buf[2:4] == b"\x00\x00":
            return None
        return "UTF-16 LE"
    if buf[:2] == b"\xfe\xff":
        return "UTF-16 BE"
    return None


@dataclass
class TextFileContent:
    content: str
    raw_content: str
    eol: str  # "\r\n" | "\n"
    mixed_eol: bool
    has_bom: bool
    size: int
    mode: int
    raw_bytes: bytes


def read_text_file(file_path: str, max_bytes: int = 10 * 1024 * 1024) -> TextFileContent:
    st = os.stat(file_path)
    if not os.path.isfile(file_path):
        raise ValueError(f"Not a regular file: {file_path}")
    if st.st_size > max_bytes:
        raise ValueError(
            f"File exceeds maximum size ({st.st_size} bytes > {max_bytes} bytes): {file_path}"
        )
    with open(file_path, "rb") as fh:
        buf = fh.read()
    utf16 = detect_utf16_bom(buf)
    if utf16:
        raise ValueError(
            f"UTF-16 encoded file is not supported (convert to UTF-8 first): {file_path} [{utf16}]"
        )
    if is_likely_binary(buf):
        raise ValueError(f"Binary file cannot be read as text: {file_path}")
    bom = has_utf8_bom(buf)
    raw_content = (buf[3:] if bom else buf).decode("utf-8", errors="replace")
    crlf_count = raw_content.count("\r\n")
    lf_only = raw_content.count("\n") - crlf_count
    eol = "\r\n" if crlf_count > lf_only else "\n"
    return TextFileContent(
        content=raw_content,
        raw_content=raw_content,
        eol=eol,
        mixed_eol=(crlf_count > 0 and lf_only > 0),
        has_bom=bom,
        size=st.st_size,
        mode=st.st_mode & 0o7777,
        raw_bytes=buf,
    )


def _find_closing_bracket(s: str, open_idx: int) -> int:
    i = open_idx + 1
    if i < len(s) and s[i] in ("!", "^"):
        i += 1
    if i < len(s) and s[i] == "]":
        i += 1
    while i < len(s):
        if s[i] == "]":
            return i
        i += 1
    return -1


def _find_matching_brace(s: str, open_idx: int) -> int:
    depth = 0
    i = open_idx
    while i < len(s):
        if s[i] == "[":
            close = _find_closing_bracket(s, i)
            if close != -1:
                i = close + 1
                continue
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_brace_alternatives(inner: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch == "[":
            close = _find_closing_bracket(inner, i)
            if close != -1:
                i = close + 1
                continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(inner[start:i])
            start = i + 1
        i += 1
    parts.append(inner[start:])
    return parts


def _compile_glob_body(norm: str) -> str:
    out: list[str] = []
    i = 0
    n = len(norm)
    while i < n:
        ch = norm[i]
        if ch == "*":
            if i + 1 < n and norm[i + 1] == "*":
                if i + 2 < n and norm[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                else:
                    out.append(".*")
                    i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            close = _find_closing_bracket(norm, i)
            if close == -1:
                out.append(r"\[")
                i += 1
            else:
                inner = norm[i + 1 : close]
                neg = inner.startswith(("!", "^"))
                if neg:
                    inner = inner[1:]
                inner = inner.replace("\\", "\\\\").replace("]", r"\]")
                out.append(f"(?!/)[^{inner}]" if neg else f"[{inner}]")
                i = close + 1
        elif ch == "{":
            close = _find_matching_brace(norm, i)
            if close == -1:
                out.append(r"\{")
                i += 1
            else:
                parts = _split_brace_alternatives(norm[i + 1 : close])
                sub = "|".join(
                    _compile_glob_body(
                        re.sub(r"(?:\*\*/)+", "**/", re.sub(r"\*{3,}", "**", p))
                    )
                    for p in parts
                )
                out.append(f"(?:{sub})")
                i = close + 1
        elif ch in ".+^$()|\\":
            out.append("\\" + ch)
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    norm = pattern.strip().replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    norm = re.sub(r"\*{3,}", "**", norm)
    norm = re.sub(r"(?:\*\*/)+", "**/", norm)
    try:
        return re.compile(f"^{_compile_glob_body(norm)}$")
    except re.error as exc:
        raise ValueError(f"Invalid glob pattern '{pattern}': {exc}") from exc


@dataclass
class WalkResult:
    files: list[str]
    truncated: bool = False
    timed_out: bool = False
    dirs_truncated: bool = False


def walk_files(
    root_dir: str,
    max_files: int = 5000,
    max_depth: int = 32,
    include_hidden: bool = False,
    ignore_dirs: frozenset[str] | set[str] | None = None,
    file_filter: Callable[[str], bool] | None = None,
    timeout_ms: int | None = None,
    max_dirs: int | None = None,
) -> WalkResult:
    ignore = ignore_dirs if ignore_dirs is not None else DEFAULT_IGNORE_DIRS
    deadline = (time.monotonic() + timeout_ms / 1000.0) if (timeout_ms and timeout_ms > 0) else None
    files: list[str] = []
    truncated = False
    timed_out = False
    dirs_truncated = False
    visited_dirs = 0

    def _visit(cur_dir: str, rel_dir: str, depth: int) -> None:
        nonlocal truncated, timed_out, dirs_truncated, visited_dirs
        if truncated or depth > max_depth:
            return
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            truncated = True
            return
        if max_dirs is not None and visited_dirs >= max_dirs:
            dirs_truncated = True
            truncated = True
            return
        visited_dirs += 1
        try:
            with os.scandir(cur_dir) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            return
        subdirs: list[tuple[str, str]] = []
        for entry in entries:
            name = entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            if not include_hidden and name.startswith("."):
                if not (is_dir and name == ".agents"):
                    continue
            rel_posix = f"{rel_dir}/{name}" if rel_dir else name
            if is_dir:
                if name in ignore:
                    continue
                subdirs.append((entry.path, rel_posix))
            elif is_file:
                if file_filter and not file_filter(rel_posix):
                    continue
                if len(files) >= max_files:
                    truncated = True
                    return
                files.append(rel_posix)
        if depth >= max_depth:
            return
        for sub_abs, sub_rel in subdirs:
            if truncated:
                return
            _visit(sub_abs, sub_rel, depth + 1)

    _visit(root_dir, "", 0)
    return WalkResult(
        files=files,
        truncated=truncated,
        timed_out=timed_out,
        dirs_truncated=dirs_truncated,
    )


# ---------------------------------------------------------------------------
# Fuzzy subsequence path scorer & ranker
# ---------------------------------------------------------------------------


def subsequence_cost(needle: str, haystack: str) -> int | None:
    if not needle:
        return 2_147_483_647
    q = needle.lower()
    h = haystack.lower()
    pos = h.find(q)
    if pos >= 0:
        return -100 if pos == 0 else -60 + min(25, pos)
    cursor = 0
    first = -1
    last = -1
    hlen = len(h)
    for ch in q:
        idx = h.find(ch, cursor)
        if idx == -1:
            return None
        if first < 0:
            first = idx
        last = idx
        cursor = idx + 1
        if cursor > hlen:
            return None
    return max(0, last - first + 1 - len(q)) - (20 if first == 0 else 0)


def fuzzy_score_path(query: str, candidate_posix: str) -> int:
    trimmed = query.strip().replace("\\", "/")
    if not trimmed:
        return -1
    tokens = trimmed.split()
    slash_idx = candidate_posix.rfind("/")
    basename = candidate_posix if slash_idx == -1 else candidate_posix[slash_idx + 1 :]
    total = 0
    for tok in tokens:
        cost = subsequence_cost(tok, candidate_posix)
        if cost is None:
            return -1
        full_score = max(1, 1000 - cost)
        if "/" not in tok:
            b_cost = subsequence_cost(tok, basename)
            base_score = -1 if b_cost is None else max(1, 1000 - b_cost)
        else:
            base_score = -1
        total += max(full_score, base_score + 45) if base_score >= 0 else full_score
        if basename.lower() == tok.lower():
            total += 60
    return total


def rank_fuzzy_paths(
    query: str, candidates: Sequence[str], max_results: int
) -> tuple[list[tuple[str, int]], int]:
    scored: list[tuple[str, int]] = []
    for cand in candidates:
        sc = fuzzy_score_path(query, cand)
        if sc >= 0:
            scored.append((cand, sc))
    scored.sort(key=lambda item: (-item[1], len(item[0]), item[0]))
    return scored[:max_results], len(scored)


# ---------------------------------------------------------------------------
# Subprocess execution, ripgrep probe, Windows PowerShell unwrapping
# ---------------------------------------------------------------------------


@dataclass
class ExecResult:
    exit_code: int | None
    sig: str | None
    timed_out: bool
    duration_ms: int
    stdout: str
    stderr: str
    truncated: bool
    error: str | None = None


_rg_available_cache: bool | None = None


def is_ripgrep_available() -> bool:
    global _rg_available_cache
    if _rg_available_cache is None:
        _rg_available_cache = shutil.which("rg") is not None
    return _rg_available_cache


def with_non_interactive_git_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GCM_INTERACTIVE", "never")
    return env


_WINDOWS_PS_WRAPPER_RE = re.compile(
    r'^(?:"(?:pwsh|powershell)(?:\.exe)?"|\'(?:pwsh|powershell)(?:\.exe)?\'|(?:pwsh|powershell)(?:\.exe)?)(?=\s)',
    re.I,
)
_WINDOWS_PS_BENIGN_FLAGS = frozenset(
    {"-noprofile", "-nop", "-noninteractive", "-noni", "-nologo", "-nol"}
)
_WINDOWS_UNWRAP_SAFE_BINS = frozenset(
    {
        "git", "rg", "node", "npm", "npx", "pnpm", "yarn", "bun", "deno",
        "python", "python3", "py", "pip", "pip3", "pytest", "uv", "ruff",
        "mypy", "flake8", "black", "go", "cargo", "rustc", "rustup", "java",
        "javac", "mvn", "gradle", "tsc", "tsx", "eslint", "prettier",
        "vitest", "jest", "biome", "dotnet", "cmake", "make", "ninja",
        "gcc", "g++", "clang", "gh", "docker", "kubectl", "terraform", "agy",
    }
)
_WINDOWS_UNWRAP_FORBIDDEN_RE = re.compile(r"[|;&><`$(){}@%!^*\r\n'#,?\[\]~\x00-\x1f]")
_WINDOWS_PS_SYNTAX_RE = re.compile(
    r"\$env:[A-Za-z_]\w*|\$(?:LASTEXITCODE|PSVersionTable|PSScriptRoot|PROFILE|HOME|PWD|null|true|false|_)\b|\$\([^)]|@\(|(?:^|[;|&({\r\n]\s*)(?:Get|Set|New|Remove|Test|Select|Where|ForEach|Sort|Group|Measure|Compare|ConvertTo|ConvertFrom|Export|Import|Invoke|Start|Stop|Restart|Wait|Read|Write|Out|Format|Resolve|Join|Split|Copy|Move|Rename|Clear|Add|Push|Pop|Enable|Disable)-[A-Z][A-Za-z0-9]*\b",
    re.I,
)


def needs_powershell_on_windows(command: str) -> bool:
    if not command or re.match(r'^\s*["\']?(?:pwsh|powershell)(?:\.exe)?["\']?(?:\s|$)', command, re.I):
        return False
    return bool(_WINDOWS_PS_SYNTAX_RE.search(command))


def unwrap_trivial_windows_powershell_command(command: str) -> str:
    """Unwrap trivial `powershell -Command "..."` around allowlisted CLI binaries."""
    trimmed = command.strip()
    m = _WINDOWS_PS_WRAPPER_RE.match(trimmed)
    if not m:
        return command
    rest = trimmed[m.end() :]
    saw_cmd_flag = False
    while True:
        rest = rest.lstrip()
        if not rest:
            return command
        tm = re.match(r"^(\S+)", rest)
        if not tm:
            return command
        tok = tm.group(1)
        lower = tok.lower()
        rest = rest[len(tok) :]
        if lower in ("-command", "-c"):
            if not rest or not rest[0].isspace():
                return command
            saw_cmd_flag = True
            break
        if lower not in _WINDOWS_PS_BENIGN_FLAGS:
            return command
    if not saw_cmd_flag:
        return command
    payload = rest.strip()
    if not payload:
        return command
    if payload[0] in ('"', "'"):
        q = payload[0]
        if len(payload) < 2 or payload.find(q, 1) != len(payload) - 1:
            return command
        inner = payload[1:-1]
        if q == '"' and inner.endswith("\\"):
            return command
        inner = inner.strip()
    else:
        if payload[0] in "-&./\\":
            return command
        inner = payload
    if not inner or _WINDOWS_UNWRAP_FORBIDDEN_RE.search(inner):
        return command
    first_m = re.match(r"^([A-Za-z0-9_+.-]+)", inner)
    if not first_m:
        return command
    base_bin = re.sub(r"\.(?:exe|cmd|bat)$", "", first_m.group(1).lower())
    if base_bin not in _WINDOWS_UNWRAP_SAFE_BINS or needs_powershell_on_windows(inner):
        return command
    return inner


def _kill_proc_tree(proc: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
        else:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                proc.terminate()
            try:
                proc.wait(timeout=0.4)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _spawn_and_collect(
    cmd: str | list[str],
    shell: bool,
    cwd: str,
    timeout_ms: int,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    env: dict[str, str] | None = None,
) -> ExecResult:
    t0 = time.monotonic()
    timeout_sec = max(0.1, timeout_ms / 1000.0)
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": shell,
        "env": env,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        return ExecResult(
            exit_code=None,
            sig=None,
            timed_out=False,
            duration_ms=int((time.monotonic() - t0) * 1000),
            stdout="",
            stderr="",
            truncated=False,
            error=str(exc),
        )
    timed_out = False
    try:
        out_b, err_b = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_proc_tree(proc)
        try:
            out_b, err_b = proc.communicate(timeout=1.0)
        except Exception:
            out_b, err_b = b"", b""

    trunc = False
    if len(out_b) > max_output_bytes:
        out_b = out_b[:max_output_bytes]
        trunc = True
    if len(err_b) > max_output_bytes:
        err_b = err_b[:max_output_bytes]
        trunc = True

    rc = proc.returncode
    sig_name: str | None = None
    if rc is not None and rc < 0 and os.name != "nt":
        try:
            sig_name = signal.Signals(-rc).name
        except ValueError:
            sig_name = f"SIG{-rc}"
    return ExecResult(
        exit_code=None if timed_out else rc,
        sig=sig_name,
        timed_out=timed_out,
        duration_ms=int((time.monotonic() - t0) * 1000),
        stdout=out_b.decode("utf-8", errors="replace"),
        stderr=err_b.decode("utf-8", errors="replace"),
        truncated=trunc,
    )


def exec_binary(
    binary: str,
    args: list[str],
    cwd: str,
    timeout_ms: int = 15_000,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    env: dict[str, str] | None = None,
) -> ExecResult:
    return _spawn_and_collect([binary, *args], False, cwd, timeout_ms, max_output_bytes, env)


def run_shell_command(
    command: str,
    cwd: str,
    timeout_ms: int = 60_000,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    env: dict[str, str] | None = None,
) -> ExecResult:
    eff_env = with_non_interactive_git_env(env)
    if os.name == "nt":
        eff_cmd = unwrap_trivial_windows_powershell_command(command)
        if needs_powershell_on_windows(eff_cmd):
            ps = shutil.which("pwsh") or shutil.which("powershell.exe") or "powershell.exe"
            prefix = (
                "try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}; "
                "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
            )
            return _spawn_and_collect(
                [ps, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", prefix + eff_cmd],
                False,
                cwd,
                timeout_ms,
                max_output_bytes,
                eff_env,
            )
        return _spawn_and_collect(eff_cmd, True, cwd, timeout_ms, max_output_bytes, eff_env)
    return _spawn_and_collect(command, True, cwd, timeout_ms, max_output_bytes, eff_env)


# ---------------------------------------------------------------------------
# Host compatibility & minimal JSON Schema subset sanitizer
# ---------------------------------------------------------------------------

_MINIMAL_SCHEMA_STRIPPED = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "pattern",
        "multipleOf",
    }
)


def sanitize_schema_to_minimal_subset(node: dict[str, Any]) -> dict[str, Any]:
    """Strip JSON Schema range keywords while preserving property names like 'pattern'."""
    out: dict[str, Any] = {}
    for key, val in node.items():
        if key in _MINIMAL_SCHEMA_STRIPPED:
            continue
        if key == "properties" and isinstance(val, dict):
            out["properties"] = {
                pk: sanitize_schema_to_minimal_subset(pv) if isinstance(pv, dict) else pv
                for pk, pv in val.items()
            }
        elif key == "items" and isinstance(val, dict):
            out["items"] = sanitize_schema_to_minimal_subset(val)
        elif isinstance(val, list):
            out[key] = [
                sanitize_schema_to_minimal_subset(item) if isinstance(item, dict) else item
                for item in val
            ]
        elif isinstance(val, dict):
            out[key] = sanitize_schema_to_minimal_subset(val)
        else:
            out[key] = val
    return out


_SEMVER_RE = re.compile(r"(?:^|[^0-9.])v?(\d+)\.(\d+)\.(\d+)(?:\.\d+)?(?:$|[^0-9.])")


def parse_semver_tuple(raw: str | None) -> tuple[int, int, int] | None:
    if not raw or not isinstance(raw, str):
        return None
    m = _SEMVER_RE.search(raw.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def resolve_edits_format(
    client_info: dict[str, Any] | None = None,
    transcript_path: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Return (edits_format: 'array'|'string', host_label).

    Defaults to native array (2.6-3.3x fewer output tokens than escaped JSON strings).
    Automatically switches to 'string' only on pre-eager Antigravity CLI 1.5.0-1.5.7
    or when overridden via AGY_TS_EDITS_FORMAT / AGY_FAST_EDITS_FORMAT.
    """
    e = os.environ if env is None else env
    override = (e.get("AGY_TS_EDITS_FORMAT") or e.get("AGY_FAST_EDITS_FORMAT") or "").strip().lower()
    if override in ("array", "string"):
        return override, f"override:{override}"

    surface = "unknown"
    version: tuple[int, int, int] | None = None
    cname = str((client_info or {}).get("name") or "").strip().lower()
    cver = str((client_info or {}).get("version") or "").strip()
    if re.match(r"^(?:google[-_\s]?)?antigravity[-_\s]?cli$|^agy(?:[-_\s]?cli)?$", cname):
        surface = "cli"
        version = parse_semver_tuple(cver)
    elif re.match(r"^(?:google[-_\s]?)?antigravity[-_\s]?ide$", cname):
        surface = "ide"
        version = parse_semver_tuple(cver)
    elif re.match(r"^(?:google[-_\s]?)?antigravity(?:[-_\s]?(?:2\.0|app|desktop))?$", cname):
        surface = "desktop"
        version = parse_semver_tuple(cver)

    if surface == "unknown" and transcript_path:
        norm = transcript_path.replace("\\", "/").lower()
        if "/.gemini/antigravity-cli/" in norm:
            surface = "cli"
        elif "/.gemini/antigravity-ide/" in norm:
            surface = "ide"
        elif "/.gemini/antigravity/" in norm:
            surface = "desktop"

    ls_ver = e.get("ANTIGRAVITY_LS_VERSION", "")
    if version is None and ls_ver:
        version = parse_semver_tuple(ls_ver)
        if surface == "unknown":
            if "cli" in ls_ver.lower():
                surface = "cli"
            elif "ide" in ls_ver.lower():
                surface = "ide"
            elif "antigravity" in ls_ver.lower():
                surface = "desktop"

    label = f"{surface}@{version[0]}.{version[1]}.{version[2]}" if version else surface
    if surface == "cli" and version is not None and (1, 5, 0) <= version < (1, 5, 8):
        return "string", label
    return "array", label
