"""Multi-language symbol outlines (`quick_outline`) and targeted slices (`read_slices`).

Extracts exact [startLine, endLine] symbol/section maps across 15+ languages
(TS/JS incl. routes & tests, Python, GDScript, Go, Rust, Java/Kotlin/C#/Swift/C/C++,
HTML/Vue/Svelte with embedded script/style parsing, CSS/SCSS/Less, Shell, Markdown,
JSON/YAML) with breadth-first water-filling strictly under <= 8,800 UTF-8 bytes.

Incorporates engineering patterns adapted from INK-Q/agy-fast (MIT License).
"""

from __future__ import annotations

import ast
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from fast_utils import (
    DEFAULT_INLINE_MAX_BYTES,
    TOOL_VALIDATION_EXAMPLES,
    clamp_int,
    compute_water_fill_budgets,
    fit_sections_to_byte_budget,
    format_corrupted_array_error,
    parse_maybe_json_array,
    parse_maybe_string_or_json_array,
    read_text_file,
    resolve_path,
    to_posix_path,
    utf8_len,
    with_validation_example,
)

MAX_OUTLINES = 40
MAX_SLICE_FILES = 30
SLICE_TRUNCATION_TIP = (
    "\n[tip: MCP responses are hard-capped <=8.8KB inline; for >150 lines across files, "
    "use parallel read_file(StartLine, EndLine) (46KB/call, 0 remote check)]"
)
OUTLINE_BATCH_TIP = (
    "\n[tip: outlining >20 files splits the 8.8KB cap thinly; batch 10-20 files per call for fuller outlines]"
)


@dataclass
class SymbolEntry:
    kind: str
    name: str
    start_line: int
    end_line: int
    depth: int


@dataclass
class _ScanState:
    in_block_comment: bool = False
    in_backtick: bool = False
    template_expr_stack: list[int] | None = None
    paren_depth: int = 0

    def __post_init__(self) -> None:
        if self.template_expr_stack is None:
            self.template_expr_stack = []


_REGEX_PRE_WORDS = frozenset(
    {"return", "case", "throw", "in", "of", "typeof", "void", "delete", "new"}
)


def _is_regex_start(line: str, slash_idx: int) -> bool:
    j = slash_idx - 1
    while j >= 0 and line[j].isspace():
        j -= 1
    if j < 0:
        return True
    prev = line[j]
    if prev in "=(,:![&|?{};~+-*%^<>":
        return True
    k = j
    while k >= 0 and (line[k].isalnum() or line[k] in "_$"):
        k -= 1
    return line[k + 1 : j + 1] in _REGEX_PRE_WORDS


def _scan_braces_on_line(
    line: str,
    state: _ScanState,
    lang: str,
    ignore_in_parens: bool = False,
    seen_open: bool = False,
) -> tuple[int, int, int, int, bool, str]:
    open_b = close_b = open_p = close_p = 0
    p_depth = state.paren_depth
    in_single = in_double = in_regex = in_rclass = esc = False
    last_code = ""
    tstack = state.template_expr_stack
    assert tstack is not None
    n = len(line)
    i = 0
    while i < n:
        ch = line[i]
        nxt = line[i + 1] if i + 1 < n else ""
        if state.in_block_comment:
            if ch == "*" and nxt == "/":
                state.in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if esc:
            esc = False
            i += 1
            continue
        if in_regex:
            if ch == "\\":
                esc = True
            elif ch == "[":
                in_rclass = True
            elif ch == "]":
                in_rclass = False
            elif ch == "/" and not in_rclass:
                in_regex = False
                last_code = "/"
            i += 1
            continue
        if state.in_backtick:
            if ch == "\\" and lang != "go":
                esc = True
            elif ch == "`":
                state.in_backtick = False
                last_code = "`"
            elif lang == "js" and ch == "$" and nxt == "{":
                state.in_backtick = False
                tstack.append(0)
                i += 2
                continue
            i += 1
            continue
        if in_single:
            if ch == "\\":
                esc = True
            elif ch == "'":
                in_single = False
                last_code = "'"
            i += 1
            continue
        if in_double:
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_double = False
                last_code = '"'
            i += 1
            continue

        if lang == "shell" and ch == "#" and (i == 0 or line[i - 1] in " \t;&|()"):
            break
        if lang in ("js", "c", "go", "rust") and ch == "/" and nxt == "/":
            break
        if lang in ("js", "c", "go", "rust") and ch == "/" and nxt == "*":
            state.in_block_comment = True
            i += 2
            continue
        if (
            lang == "js"
            and ch == "/"
            and nxt not in ("/", "*")
            and not (nxt == ">" and (i + 2 >= n or line[i + 2] != "/"))
            and (i == 0 or line[i - 1] != "<")
            and _is_regex_start(line, i)
        ):
            in_regex = True
            in_rclass = False
            i += 1
            continue
        if ch == "'" and lang != "json":
            if lang == "rust":
                if nxt == "\\" or (i + 2 < n and line[i + 2] == "'"):
                    in_single = True
                i += 1
                continue
            in_single = True
            i += 1
            continue
        if ch == '"':
            in_double = True
            i += 1
            continue
        if ch == "`" and lang in ("js", "c", "go"):
            state.in_backtick = True
            i += 1
            continue

        if ch == "{":
            if tstack:
                tstack[-1] += 1
            elif not (ignore_in_parens and not seen_open and p_depth > 0):
                open_b += 1
                seen_open = True
            last_code = "{"
        elif ch == "}":
            if tstack:
                if tstack[-1] == 0:
                    tstack.pop()
                    state.in_backtick = True
                else:
                    tstack[-1] -= 1
            elif not (ignore_in_parens and not seen_open and p_depth > 0):
                close_b += 1
            last_code = "}"
        elif ch == "(":
            open_p += 1
            p_depth += 1
            last_code = "("
        elif ch == ")":
            close_p += 1
            p_depth = max(0, p_depth - 1)
            last_code = ")"
        elif not ch.isspace():
            last_code = ch
        i += 1
    state.paren_depth = p_depth
    return open_b, close_b, open_p, close_p, (last_code == ";"), last_code


def _find_brace_block_end(lines: list[str], start_idx: int, lang: str = "c") -> int:
    depth = 0
    seen_open = False
    st = _ScanState()
    max_scan = min(len(lines), start_idx + 2000)
    for i in range(start_idx, max_scan):
        ob, cb, _op, _cp, ends_semi, last_ch = _scan_braces_on_line(
            lines[i], st, lang, lang in ("js", "go"), seen_open
        )
        if ob > 0:
            seen_open = True
        depth += ob - cb
        if seen_open and depth <= 0:
            if lang == "js" and last_ch == ">" and i <= start_idx + 12:
                seen_open = False
                depth = 0
                continue
            return i + 1
        if not seen_open and st.paren_depth <= 0 and ends_semi:
            return i + 1
        if not seen_open and i > start_idx + 40:
            return start_idx + 1
    return min(len(lines), max_scan) if seen_open else start_idx + 1


def _find_type_alias_end(lines: list[str], start_idx: int) -> int:
    first = lines[start_idx]
    eq = first.find("=")
    rhs = first[eq + 1 :] if eq >= 0 else first
    st = _ScanState()
    ob, cb, op, cp, ends_semi, last_ch = _scan_braces_on_line(rhs, st, "js")
    if ob > cb:
        return _find_brace_block_end(lines, start_idx, "js")
    if ends_semi and op <= cp:
        return start_idx + 1
    end_idx = start_idx
    pbal = op - cp
    prev_last = last_ch
    max_scan = min(len(lines) - 1, start_idx + 40)
    while end_idx < max_scan:
        nxt = lines[end_idx + 1]
        if not nxt.strip():
            break
        is_cont = (
            pbal > 0
            or prev_last in ("=", "|", "&", "?", ":")
            or bool(re.match(r"^[ \t]+(?:[|&?:]|\w+\s*\?*:)", nxt))
        )
        if not is_cont:
            break
        end_idx += 1
        ob2, cb2, op2, cp2, semi2, last2 = _scan_braces_on_line(nxt, st, "js")
        if ob2 > cb2:
            return _find_brace_block_end(lines, end_idx, "js")
        pbal += op2 - cp2
        prev_last = last2
        if semi2 and pbal <= 0:
            break
    return end_idx + 1


def _find_paren_call_end(lines: list[str], start_idx: int) -> int:
    st = _ScanState()
    seen = False
    for i in range(start_idx, min(len(lines), start_idx + 400)):
        _ob, _cb, op, _cp, _s, _lc = _scan_braces_on_line(lines[i], st, "js")
        if op > 0:
            seen = True
        if seen and st.paren_depth <= 0:
            return i + 1
    return start_idx + 1


def _has_arrow_or_brace(lines: list[str], start_idx: int, lookahead: int = 12) -> tuple[bool, bool]:
    st = _ScanState()
    pdepth = 0
    seen_p = False
    has_arrow = False
    for i in range(start_idx, min(len(lines), start_idx + lookahead)):
        ln = lines[i]
        ob, _cb, op, cp, semi, _lc = _scan_braces_on_line(ln, st, "js")
        if op > 0:
            seen_p = True
        pdepth += op - cp
        if "=>" in ln:
            has_arrow = True
        if ob > 0 and (not seen_p or pdepth <= 0):
            return has_arrow, True
        if seen_p and pdepth <= 0 and semi:
            return has_arrow, False
    return has_arrow, False


_TS_PATTERNS = (
    (re.compile(r"^(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)"), "class"),
    (re.compile(r"^(?:export\s+)?(?:declare\s+)?interface\s+([A-Za-z_$][\w$]*)"), "interface"),
    (re.compile(r"^(?:export\s+)?(?:declare\s+)?type\s+([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*="), "type"),
    (re.compile(r"^(?:export\s+)?(?:declare\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)"), "enum"),
    (re.compile(r"^(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:async\s+)?function\*?\s+([A-Za-z_$][\w$]*)"), "function"),
    (re.compile(r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>)"), "function"),
)
_TS_ML_ARROW_RE = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?\(")
_TS_METHOD_RE = re.compile(
    r"^([ \t]{1,4})(?:(?:public|private|protected|static|readonly|override|abstract|async|get|set)\s+)*([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\("
)
_TS_ROUTE_RE = re.compile(
    r"^[ \t]{1,6}(?:app|router|server|fastify)\.(get|post|put|patch|delete|all|route)\s*(?:<[^>]*>)?\s*\(\s*['\"`]([^'\"`]+)['\"`]"
)
_TS_SUITE_RE = re.compile(r"^(?:describe|suite)(?:\.(?:only|skip))?\s*\(\s*['\"`]([^'\"`]+)['\"`]")
_TS_TEST_RE = re.compile(r"^[ \t]{0,4}(?:it|test)(?:\.(?:only|skip))?\s*\(\s*['\"`]([^'\"`]+)['\"`]")
_JS_CONTROL_KW = frozenset(
    {"if", "for", "while", "switch", "catch", "else", "try", "return", "new", "throw", "await", "super", "typeof", "delete"}
)


def _extract_ts_js(lines: list[str]) -> list[SymbolEntry]:
    symbols: list[SymbolEntry] = []
    cur_cls_end = cur_cls_indent = cur_fn_end = cur_suite_end = -1
    st = _ScanState()
    for i, line in enumerate(lines):
        was_c, was_bt = st.in_block_comment, st.in_backtick
        _scan_braces_on_line(line, st, "js")
        if was_c or was_bt:
            continue
        trimmed = line.strip()
        if not trimmed or trimmed.startswith(("//", "/*", "*")):
            continue
        matched_top = False
        if not line.startswith((" ", "\t")):
            for pat, kind in _TS_PATTERNS:
                m = pat.match(line)
                if m:
                    end_ln = _find_type_alias_end(lines, i) if kind == "type" else _find_brace_block_end(lines, i, "js")
                    symbols.append(SymbolEntry(kind, m.group(1), i + 1, end_ln, 0))
                    if kind == "class":
                        cur_cls_end = end_ln
                        cur_cls_indent = -1
                    elif kind == "function" and end_ln - (i + 1) >= 40:
                        cur_fn_end = end_ln
                    matched_top = True
                    break
            if not matched_top:
                ma = _TS_ML_ARROW_RE.match(line)
                if ma and _has_arrow_or_brace(lines, i, 12)[0]:
                    end_ln = _find_brace_block_end(lines, i, "js")
                    symbols.append(SymbolEntry("function", ma.group(1), i + 1, end_ln, 0))
                    if end_ln - (i + 1) >= 40:
                        cur_fn_end = end_ln
                    matched_top = True
            if not matched_top:
                ms = _TS_SUITE_RE.match(line)
                if ms:
                    end_ln = _find_paren_call_end(lines, i)
                    symbols.append(SymbolEntry("suite", ms.group(1)[:64], i + 1, end_ln, 0))
                    cur_suite_end = end_ln
                    matched_top = True
        if not matched_top and i + 1 <= cur_cls_end:
            mm = _TS_METHOD_RE.match(line)
            if mm and mm.group(2) not in _JS_CONTROL_KW:
                iw = len(mm.group(1).replace("\t", "  "))
                if cur_cls_indent == -1:
                    cur_cls_indent = iw
                if iw == cur_cls_indent and _has_arrow_or_brace(lines, i, 10)[1]:
                    symbols.append(SymbolEntry("method", mm.group(2), i + 1, _find_brace_block_end(lines, i, "js"), 1))
        elif not matched_top and i + 1 <= cur_fn_end:
            mr = _TS_ROUTE_RE.match(line)
            if mr:
                symbols.append(SymbolEntry("route", f"{mr.group(1).upper()} {mr.group(2)}", i + 1, _find_paren_call_end(lines, i), 1))
        elif not matched_top:
            mt = _TS_TEST_RE.match(line)
            if mt:
                symbols.append(SymbolEntry("test", mt.group(1)[:64], i + 1, _find_paren_call_end(lines, i), 1 if i + 1 <= cur_suite_end else 0))
    return symbols


def _extract_python(lines: list[str]) -> list[SymbolEntry]:
    src = "\n".join(lines)
    try:
        tree = ast.parse(src)
        out: list[SymbolEntry] = []

        def _walk(body: list[ast.stmt], depth: int) -> None:
            for node in body:
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    kind = "class" if isinstance(node, ast.ClassDef) else ("method" if depth > 0 else "function")
                    start_ln = node.lineno
                    for dec in node.decorator_list:
                        if getattr(dec, "lineno", start_ln) < start_ln:
                            start_ln = dec.lineno
                    end_ln = getattr(node, "end_lineno", None) or node.lineno
                    out.append(SymbolEntry(kind, node.name, start_ln, end_ln, min(depth, 1)))
                    if isinstance(node, ast.ClassDef):
                        _walk(node.body, depth + 1)

        _walk(tree.body, 0)
        return out
    except SyntaxError:
        pass

    # Indentation fallback for mid-edit / syntax-broken Python files
    pat = re.compile(r"^([ \t]*)(async\s+def|def|class)\s+([A-Za-z_]\w*)")
    raw: list[tuple[int, str, str, int]] = []
    for i, ln in enumerate(lines):
        m = pat.match(ln)
        if m:
            ind = len(m.group(1).replace("\t", "    "))
            kind = "class" if m.group(2) == "class" else ("method" if ind > 0 else "function")
            raw.append((ind, kind, m.group(3), i))
    syms: list[SymbolEntry] = []
    for ind, kind, name, def_idx in raw:
        end_idx = len(lines) - 1
        for j in range(def_idx + 1, len(lines)):
            t = lines[j].strip()
            if not t or t.startswith("#"):
                continue
            cur_ind = len(lines[j][: len(lines[j]) - len(lines[j].lstrip(" \t"))].replace("\t", "    "))
            if cur_ind <= ind:
                end_idx = j - 1
                break
        while end_idx > def_idx and not lines[end_idx].strip():
            end_idx -= 1
        syms.append(SymbolEntry(kind, name, def_idx + 1, end_idx + 1, 1 if ind > 0 else 0))
    return syms


def _extract_gdscript(lines: list[str]) -> list[SymbolEntry]:
    pat_cls_name = re.compile(r"^class_name\s+([A-Za-z_]\w*)\b")
    pat_cls = re.compile(r"^class\s+([A-Za-z_]\w*)\b")
    pat_func = re.compile(r"^(?:static\s+)?func\s+([A-Za-z_]\w*)\s*\(")
    pat_sig = re.compile(r"^signal\s+([A-Za-z_]\w*)\b")
    pat_enum = re.compile(r"^enum(?:\s+([A-Za-z_]\w*))?\s*(?:\{|$|#)")
    syms: list[SymbolEntry] = []
    for i, ln in enumerate(lines):
        ind_str = ln[: len(ln) - len(ln.lstrip(" \t"))]
        rest = re.sub(r"^(?:@[A-Za-z_]\w*(?:\([^)]*\))?\s+)+", "", ln[len(ind_str) :])
        if not rest or rest.startswith("#"):
            continue
        ind = len(ind_str.replace("\t", "    "))
        for pat, kind in (
            (pat_cls_name, "class_name"),
            (pat_cls, "class"),
            (pat_func, "method" if ind > 0 else "function"),
            (pat_sig, "signal"),
            (pat_enum, "enum"),
        ):
            m = pat.match(rest)
            if m:
                name = m.group(1) or "<anonymous>"
                end_idx = i
                if kind in ("class", "function", "method"):
                    end_idx = len(lines) - 1
                    for j in range(i + 1, len(lines)):
                        t = lines[j].strip()
                        if not t or t.startswith("#"):
                            continue
                        ci = len(lines[j][: len(lines[j]) - len(lines[j].lstrip(" \t"))].replace("\t", "    "))
                        if ci <= ind:
                            end_idx = j - 1
                            break
                    while end_idx > i and not lines[end_idx].strip():
                        end_idx -= 1
                elif kind == "enum" and "{" in rest:
                    end_idx = _find_brace_block_end(lines, i, "c") - 1
                syms.append(SymbolEntry(kind, name, i + 1, max(i + 1, end_idx + 1), 1 if ind > 0 else 0))
                break
    return syms


def _extract_go(lines: list[str]) -> list[SymbolEntry]:
    func_re = re.compile(r"^func\s+(?:\(([^)]+)\)\s+)?([A-Za-z_]\w*)")
    type_re = re.compile(r"^type\s+([A-Za-z_]\w*(?:\[[^\]]*\])?)\s+(struct|interface)\b")
    st = _ScanState()
    syms: list[SymbolEntry] = []
    for i, ln in enumerate(lines):
        wc, wb = st.in_block_comment, st.in_backtick
        _scan_braces_on_line(ln, st, "go")
        if wc or wb:
            continue
        fm = func_re.match(ln)
        if fm:
            recv = (fm.group(1) or "").strip()
            name = f"({recv}) {fm.group(2)}" if recv else fm.group(2)
            syms.append(SymbolEntry("method" if recv else "function", name, i + 1, _find_brace_block_end(lines, i, "go"), 0))
            continue
        tm = type_re.match(ln)
        if tm:
            syms.append(SymbolEntry(tm.group(2), tm.group(1), i + 1, _find_brace_block_end(lines, i, "go"), 0))
    return syms


def _extract_rust(lines: list[str]) -> list[SymbolEntry]:
    pat = re.compile(
        r'^([ \t]*)(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|unsafe|const|extern(?:\s+"[^"]*")?)\s+)*(fn|struct|enum|trait|impl|mod)(?:\s+|\s*(?=<))([A-Za-z_<][\w:<>,+\'&\s]*?)(?:\s*[\({;]|$)'
    )
    st = _ScanState()
    syms: list[SymbolEntry] = []
    for i, ln in enumerate(lines):
        wc = st.in_block_comment
        _scan_braces_on_line(ln, st, "rust")
        if wc:
            continue
        t = ln.strip()
        if not t or t.startswith(("//", "/*")):
            continue
        m = pat.match(ln)
        if m and len(m.group(1)) <= 4:
            syms.append(SymbolEntry(m.group(2), m.group(3).strip(), i + 1, _find_brace_block_end(lines, i, "rust"), 1 if m.group(1) else 0))
    return syms


def _extract_c_like(lines: list[str]) -> list[SymbolEntry]:
    type_re = re.compile(
        r"^([ \t]{0,4})(?:(?:public|private|protected|internal|static|final|abstract|sealed|data|open|export|partial)\s+)*(class|interface|enum|struct|record|protocol|namespace)\s+([A-Za-z_]\w*)"
    )
    kw_fn_re = re.compile(
        r"^([ \t]{0,4})(?:(?:public|private|protected|internal|static|final|override|virtual|inline|async|suspend|open|mutating)\s+)*(?:fun|func)\s+([A-Za-z_]\w*)"
    )
    c_meth_re = re.compile(
        r"^([ \t]{0,4})(?:(?:public|private|protected|internal|static|final|abstract|override|virtual|inline|constexpr|extern|async|synchronized|native|unsigned|signed|const)\s+)*(?:[A-Za-z_~][\w:<>,\[\]\s*&]*?\s+)([A-Za-z_~][\w:]*)\s*\([^;]*$"
    )
    excl = frozenset({"if", "for", "while", "switch", "catch", "else", "return", "sizeof", "alignof", "decltype", "new", "delete", "throw", "typedef", "using", "case"})
    st = _ScanState()
    syms: list[SymbolEntry] = []
    for i, ln in enumerate(lines):
        wc = st.in_block_comment
        _scan_braces_on_line(ln, st, "c")
        if wc:
            continue
        t = ln.strip()
        if not t or t.startswith(("//", "/*", "*", "#")):
            continue
        tm = type_re.match(ln)
        if tm:
            ind = len(tm.group(1)) > 0
            syms.append(SymbolEntry(tm.group(2), tm.group(3), i + 1, _find_brace_block_end(lines, i, "c"), 1 if ind else 0))
            continue
        fm = kw_fn_re.match(ln)
        if fm:
            ind = len(fm.group(1)) > 0
            syms.append(SymbolEntry("method" if ind else "function", fm.group(2), i + 1, _find_brace_block_end(lines, i, "c"), 1 if ind else 0))
            continue
        cm = c_meth_re.match(ln)
        if cm and cm.group(2) not in excl and "=" not in t and _has_arrow_or_brace(lines, i, 8)[1]:
            ind = len(cm.group(1)) > 0
            syms.append(SymbolEntry("method" if ind else "function", cm.group(2), i + 1, _find_brace_block_end(lines, i, "c"), 1 if ind else 0))
    return syms


def _extract_shell(lines: list[str]) -> list[SymbolEntry]:
    pat = re.compile(r"^(?:function\s+([A-Za-z_][\w-]*)\s*(?:\(\))?|([A-Za-z_][\w-]*)\s*\(\))\s*\{")
    syms: list[SymbolEntry] = []
    for i, ln in enumerate(lines):
        m = pat.match(ln)
        if m:
            name = m.group(1) or m.group(2)
            if name:
                syms.append(SymbolEntry("function", name, i + 1, _find_brace_block_end(lines, i, "shell"), 0))
    return syms


def _extract_markdown(lines: list[str]) -> list[SymbolEntry]:
    raw: list[tuple[int, str, int]] = []
    in_fence = False
    for i, ln in enumerate(lines):
        if re.match(r"^\s*(`{3,}|~{3,})", ln):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = re.match(r"^(#{1,4})\s+(.+?)\s*#*$", ln)
        if m:
            raw.append((len(m.group(1)), m.group(2), i + 1))
    out: list[SymbolEntry] = []
    for idx, (lvl, name, s_ln) in enumerate(raw):
        end_ln = len(lines)
        for j in range(idx + 1, len(raw)):
            if raw[j][0] <= lvl:
                end_ln = raw[j][2] - 1
                break
        out.append(SymbolEntry(f"h{lvl}", name, s_ln, max(s_ln, end_ln), lvl - 1))
    return out


def _extract_json_yaml(lines: list[str], is_json: bool) -> list[SymbolEntry]:
    syms: list[SymbolEntry] = []
    if is_json:
        key_indent: str | None = None
        for i, ln in enumerate(lines):
            m = re.match(r'^([ \t]+)"([^"]+)"\s*:', ln)
            if m:
                if key_indent is None:
                    key_indent = m.group(1)
                if m.group(1) == key_indent:
                    syms.append(SymbolEntry("key", m.group(2), i + 1, i + 1, 0))
        for idx, sym in enumerate(syms):
            nxt = syms[idx + 1].start_line - 1 if idx + 1 < len(syms) else len(lines) - 1
            end_ln = max(sym.start_line, nxt)
            while idx == len(syms) - 1 and end_ln > sym.start_line and re.match(r"^\s*[}\]]?,?\s*$", lines[end_ln - 1]):
                end_ln -= 1
            sym.end_line = end_ln
        if not syms:
            try:
                parsed = json.loads("\n".join(lines))
                if isinstance(parsed, dict):
                    for k in list(parsed.keys())[:60]:
                        syms.append(SymbolEntry("key", str(k), 1, max(1, len(lines)), 0))
            except Exception:
                pass
        return syms

    for i, ln in enumerate(lines):
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*:", ln)
        if m:
            syms.append(SymbolEntry("key", m.group(1), i + 1, i + 1, 0))
    for idx, sym in enumerate(syms):
        end_ln = syms[idx + 1].start_line - 1 if idx + 1 < len(syms) else len(lines)
        while end_ln > sym.start_line and not lines[end_ln - 1].strip():
            end_ln -= 1
        sym.end_line = max(sym.start_line, end_ln)
    return syms


def _extract_css(lines: list[str], compact_only: bool = False) -> list[SymbolEntry]:
    at_re = re.compile(r"^@(layer|media|keyframes|supports|container|font-face|theme)\b([^{;]*)", re.I)
    tok_re = re.compile(r"^(:root\b|html(?:\[[^\]]+\]|\.[A-Za-z_-][\w-]*)?(?=\s|[,{])|\[data-theme[^\]]*\]|body\b)[^{;]*\{")
    sec_re = re.compile(r"^/\*\s*[=\-*#]*\s*([^*/=][^*/]{1,70}?)\s*[=\-*#]*\s*\*/$")
    st = _ScanState()
    depth = 0
    syms: list[SymbolEntry] = []
    for i, ln in enumerate(lines):
        t = ln.strip()
        wc = st.in_block_comment
        if not wc and depth == 0 and t:
            cm = sec_re.match(t)
            if cm and re.search(r"[A-Za-z0-9]", cm.group(1)):
                syms.append(SymbolEntry("section", cm.group(1).strip()[:64], i + 1, i + 1, 0))
            else:
                am = at_re.match(t)
                if am:
                    end_ln = _find_brace_block_end(lines, i, "c") if "{" in t else i + 1
                    rest = re.sub(r"\s+", " ", (am.group(2) or "").strip())[:56]
                    syms.append(SymbolEntry(f"@{am.group(1).lower()}", rest or f"@{am.group(1).lower()}", i + 1, end_ln, 0))
                else:
                    tm = tok_re.match(t)
                    if tm:
                        hdr = re.sub(r"\s+", " ", t[: t.find("{")].strip())[:60]
                        syms.append(SymbolEntry("tokens", hdr or tm.group(1), i + 1, _find_brace_block_end(lines, i, "c"), 0))
        ob, cb, *_ = _scan_braces_on_line(ln, st, "c")
        depth = max(0, depth + ob - cb)
    if not compact_only and len(syms) < 10:
        st2 = _ScanState()
        d2 = 0
        last_added = -999
        seen = {s.start_line for s in syms}
        for i, ln in enumerate(lines):
            if len(syms) >= 40:
                break
            t = ln.strip()
            wc = st2.in_block_comment
            if not wc and d2 == 0 and (i + 1) not in seen and i - last_added >= 10 and re.match(r"^[.#\[a-zA-Z*:]", t) and "{" in t and not t.startswith("@"):
                sel = re.sub(r"\s+", " ", t[: t.find("{")].strip())[:60]
                if sel:
                    syms.append(SymbolEntry("rule", sel, i + 1, _find_brace_block_end(lines, i, "c"), 0))
                    last_added = i
            ob, cb, *_ = _scan_braces_on_line(ln, st2, "c")
            d2 = max(0, d2 + ob - cb)
        syms.sort(key=lambda s: s.start_line)
    return syms


_HTML_SEMANTIC = frozenset({"head", "body", "header", "nav", "main", "aside", "section", "article", "footer", "form", "dialog", "template"})
_HTML_BLOCK_RE = re.compile(r"<(script|style)\b([^>]*)>([\s\S]*?)</\1\s*>", re.I)
_HTML_TAG_RE = re.compile(r"<([A-Za-z][A-Za-z0-9_-]*)\b([^>]*?)(/?)>")


def _extract_html_sfc(lines: list[str], is_sfc: bool = False) -> list[SymbolEntry]:
    text = "\n".join(lines)
    line_starts = [0]
    for idx, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(idx + 1)

    def _off_to_line(off: int) -> int:
        lo, hi = 0, len(line_starts) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if line_starts[mid] <= off:
                lo = mid + 1
            else:
                hi = mid - 1
        return max(1, hi + 1)

    syms: list[SymbolEntry] = []
    block_spans: list[tuple[int, int]] = []
    for m in _HTML_BLOCK_RE.finditer(text):
        tag = m.group(1).lower()
        s_ln = _off_to_line(m.start())
        e_ln = _off_to_line(m.end() - 1)
        block_spans.append((m.start(), m.end()))
        attrs = re.sub(r"\s+", " ", m.group(2).strip())
        lbl = f"<{tag} {attrs[:32]}>" if attrs else f"<{tag}>"
        syms.append(SymbolEntry(tag, lbl, s_ln, e_ln, 0))
        inner = m.group(3)
        if inner.strip():
            inner_start_ln = _off_to_line(m.start(3))
            inner_lines = inner.split("\n")
            non_empty = [ln for ln in inner_lines if ln.strip()]
            min_ind = min((len(ln) - len(ln.lstrip(" \t")) for ln in non_empty), default=0)
            dedented = [ln[min_ind:] if len(ln) >= min_ind else ln.lstrip(" \t") for ln in inner_lines]
            inner_syms = _extract_ts_js(dedented) if tag == "script" else _extract_css(dedented, compact_only=True)
            for s in inner_syms:
                syms.append(
                    SymbolEntry(
                        s.kind,
                        s.name,
                        s.start_line + inner_start_ln - 1,
                        max(s.start_line + inner_start_ln - 1, s.end_line + inner_start_ln - 1),
                        min(2, 1 + s.depth),
                    )
                )

    for m in _HTML_TAG_RE.finditer(text):
        pos = m.start()
        if any(bs <= pos < be for bs, be in block_spans):
            continue
        raw_tag = m.group(1)
        low = raw_tag.lower()
        attrs = m.group(2)
        if low in ("script", "style"):
            continue
        id_m = re.search(r'\b(?:id|data-testid|role)\s*=\s*["\']([^"\']+)["\']', attrs)
        is_comp = raw_tag[0].isupper() and any(c.islower() for c in raw_tag)
        if low in _HTML_SEMANTIC or is_comp or (low in ("div", "ul", "ol", "table") and id_m):
            s_ln = _off_to_line(pos)
            close_m = re.search(rf"</{re.escape(raw_tag)}\s*>", text[m.end() :], re.I)
            e_ln = _off_to_line(m.end() + close_m.start()) if close_m else s_ln
            suffix = f"#{id_m.group(1)}" if id_m else ""
            kind = "template" if low == "template" else ("component" if is_comp else "block")
            syms.append(SymbolEntry(kind, f"{raw_tag}{suffix}", s_ln, max(s_ln, e_ln), 0))

    syms.sort(key=lambda s: (s.start_line, s.depth))
    if is_sfc and not syms:
        return _extract_ts_js(lines)
    return syms


def extract_symbols(file_path: str, content: str) -> list[SymbolEntry]:
    norm = content[1:] if content.startswith("\ufeff") else content
    lines = norm.replace("\r\n", "\n").split("\n")
    ext = os.path.splitext(file_path)[1].lower()
    if ext in (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"):
        return _extract_ts_js(lines)
    if ext in (".html", ".htm"):
        return _extract_html_sfc(lines, is_sfc=False)
    if ext in (".vue", ".svelte"):
        return _extract_html_sfc(lines, is_sfc=True)
    if ext in (".css", ".scss", ".less", ".pcss"):
        return _extract_css(lines)
    if ext in (".py", ".pyi"):
        return _extract_python(lines)
    if ext == ".gd":
        return _extract_gdscript(lines)
    if ext == ".go":
        return _extract_go(lines)
    if ext == ".rs":
        return _extract_rust(lines)
    if ext in (".java", ".kt", ".kts", ".cs", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp"):
        return _extract_c_like(lines)
    if ext in (".sh", ".bash", ".zsh"):
        return _extract_shell(lines)
    if ext in (".md", ".mdx"):
        return _extract_markdown(lines)
    if ext == ".json":
        return _extract_json_yaml(lines, is_json=True)
    if ext in (".yaml", ".yml"):
        return _extract_json_yaml(lines, is_json=False)
    return _extract_ts_js(lines)


def _format_sym(s: SymbolEntry) -> str:
    ind = "  " * s.depth
    span = f"L{s.start_line}-L{s.end_line}" if s.end_line > s.start_line else f"L{s.start_line}"
    return f"{ind}{span} [{s.kind}] {s.name}"


def _pack_outline_to_budget(header: str, symbols: list[SymbolEntry], char_budget: int, byte_budget: int) -> str:
    items = [(_format_sym(s), len(_format_sym(s)) + 1, utf8_len(_format_sym(s)) + 1) for s in symbols]
    hc, hb = len(header), utf8_len(header)
    if hc + sum(it[1] for it in items) <= char_budget and hb + sum(it[2] for it in items) <= byte_budget:
        return f"{header}\n" + "\n".join(it[0] for it in items)

    by_depth: dict[int, list[int]] = {}
    for idx, s in enumerate(symbols):
        by_depth.setdefault(s.depth, []).append(idx)
    depths = sorted(by_depth.keys())
    top_d = depths[0] if depths else 0
    top_ids = by_depth.get(top_d, [])
    nested_total = len(symbols) - len(top_ids)

    top_c = hc + sum(items[i][1] for i in top_ids)
    top_b = hb + sum(items[i][2] for i in top_ids)

    if nested_total > 0:
        full_nf = f"\n... (+{nested_total} nested symbols omitted)"
        comp_nf = f"\n... (+{nested_total} nested)"
        fits_full = top_c + len(full_nf) <= char_budget and top_b + len(full_nf) <= byte_budget
        fits_comp = not fits_full and top_c + len(comp_nf) <= char_budget and top_b + len(comp_nf) <= byte_budget
        if fits_full or fits_comp:
            use_comp = not fits_full
            selected = set(top_ids)
            uc, ub = top_c, top_b
            omitted = nested_total
            stopped = False
            for d in depths:
                if d == top_d or stopped:
                    continue
                for idx in by_depth[d]:
                    it = items[idx]
                    nxt_om = omitted - 1
                    nf_len = len(f"\n... (+{nxt_om} nested)" if use_comp else f"\n... (+{nxt_om} nested symbols omitted)") if nxt_om > 0 else 0
                    if uc + it[1] + nf_len <= char_budget and ub + it[2] + nf_len <= byte_budget:
                        selected.add(idx)
                        uc += it[1]
                        ub += it[2]
                        omitted = nxt_om
                    else:
                        stopped = True
                        break
            ordered = sorted(selected)
            footer = (f"\n... (+{omitted} nested)" if use_comp else f"\n... (+{omitted} nested symbols omitted)") if omitted > 0 else ""
            return f"{header}\n" + "\n".join(items[i][0] for i in ordered) + footer

    # Top-level symbols overflow -> fit prefix + L<min>-L<max> omission footer
    m = len(top_ids)
    suf_min = [0] * m
    suf_max = [0] * m
    for k in range(m - 1, -1, -1):
        sym = symbols[top_ids[k]]
        if k == m - 1:
            suf_min[k], suf_max[k] = sym.start_line, sym.end_line
        else:
            suf_min[k] = min(sym.start_line, suf_min[k + 1])
            suf_max[k] = max(sym.end_line, suf_max[k + 1])

    uc, ub = hc, hb
    picked = 0
    chosen_footer = ""
    for k in range(m):
        it = items[top_ids[k]]
        rem_om = m - (k + 1)
        if rem_om == 0:
            if uc + it[1] <= char_budget and ub + it[2] <= byte_budget:
                picked = k + 1
                chosen_footer = ""
            break
        rc = char_budget - (uc + it[1])
        rb = byte_budget - (ub + it[2])
        cands = [
            f"\n... (+{rem_om} more top-level symbols in L{suf_min[k + 1]}-L{suf_max[k + 1]} omitted{', plus nested' if nested_total else ''}; use batch_search or read_file)",
            f"\n... (+{rem_om} more in L{suf_min[k + 1]}-L{suf_max[k + 1]} omitted)",
            f"\n... (+{rem_om} L{suf_min[k + 1]}-L{suf_max[k + 1]})",
        ]
        fit_f = next((c for c in cands if len(c) <= rc and utf8_len(c) <= rb), None)
        if fit_f is not None:
            uc += it[1]
            ub += it[2]
            picked = k + 1
            chosen_footer = fit_f
        else:
            break
    if picked > 0:
        return f"{header}\n" + "\n".join(items[top_ids[k]][0] for k in range(picked)) + chosen_footer
    if m > 0:
        short_f = f"\n... (+{m} L{suf_min[0]}-L{suf_max[0]})"
        if len(short_f) <= char_budget - hc and utf8_len(short_f) <= byte_budget - hb:
            return f"{header}{short_f}"
    return header


def _parse_slice_shorthand(raw: str, label: str) -> tuple[dict[str, Any] | None, str | None]:
    trimmed = raw.strip()
    m = re.match(r"^(.+):(\d+(?:-\d+)?(?:\s*,\s*\d+(?:-\d+)?)*)$", trimmed)
    if not m or not m.group(1).strip():
        return None, with_validation_example(
            f"Error: {label} must use 'path:start-end' or 'path:start1-end1,start2-end2' format (got {raw!r}).",
            TOOL_VALIDATION_EXAMPLES["read_slices_line_range"],
        )
    ranges: list[list[int]] = []
    for part in m.group(2).split(","):
        seg = part.strip()
        if "-" in seg:
            a, b = seg.split("-", 1)
            ranges.append([int(a), int(b)])
        else:
            n = int(seg)
            ranges.append([n, n])
    return {"path": m.group(1).strip(), "ranges": ranges}, None


def run_read_slices(args: dict[str, Any]) -> dict[str, Any]:
    corrupted: list[tuple[str, Any, str]] = []
    fmt_err: str | None = None
    outlines: list[str] = []

    if "outlines" in args and args["outlines"] is not None:
        raw_o = args["outlines"]
        ok, kind, payload = parse_maybe_string_or_json_array(raw_o)
        if not ok:
            if kind == "corrupted_literal" or isinstance(raw_o, bool):
                corrupted.append(("outlines", payload if kind == "corrupted_literal" else raw_o, "an array of file path strings"))
            else:
                fmt_err = with_validation_example(
                    f"Error: 'outlines' must be an array of non-empty file path strings ({payload}).",
                    TOOL_VALIDATION_EXAMPLES["read_slices"],
                )
        else:
            for i, it in enumerate(payload):
                if it is None or isinstance(it, bool):
                    corrupted.append((f"outlines[{i}]", it, "a non-empty file path string"))
                elif not isinstance(it, str) or not it.strip():
                    fmt_err = fmt_err or with_validation_example(
                        f"Error: outlines[{i}] must be a non-empty file path string.",
                        TOOL_VALIDATION_EXAMPLES["read_slices"],
                    )
            if not corrupted and fmt_err is None:
                outlines = payload
    elif "outlines" in args and args["outlines"] is None:
        corrupted.append(("outlines", None, "an array of file path strings"))

    slices: list[dict[str, Any]] = []
    if "slices" in args and args["slices"] is not None:
        raw_s = args["slices"]
        ok, kind, payload = parse_maybe_json_array(raw_s)
        if not ok:
            if kind == "corrupted_literal" or isinstance(raw_s, bool):
                corrupted.append(("slices", payload if kind == "corrupted_literal" else raw_s, "an array of slice objects"))
            elif fmt_err is None:
                fmt_err = with_validation_example(
                    f"Error: 'slices' must be an array of slice objects ({payload}).",
                    TOOL_VALIDATION_EXAMPLES["read_slices"],
                )
        else:
            for i, it in enumerate(payload):
                if isinstance(it, str):
                    parsed, err = _parse_slice_shorthand(it, f"slices[{i}]")
                    if parsed:
                        slices.append(parsed)
                    elif fmt_err is None:
                        fmt_err = err
                elif it is None or isinstance(it, bool):
                    corrupted.append((f"slices[{i}]", it, "a slice object ({ path, ranges }) or 'path:start-end' string"))
                elif not isinstance(it, dict):
                    fmt_err = fmt_err or with_validation_example(
                        f"Error: slices[{i}] must be a slice object or 'path:start-end' string.",
                        TOOL_VALIDATION_EXAMPLES["read_slices"],
                    )
                else:
                    rng = it.get("ranges")
                    if rng is None or isinstance(rng, bool):
                        corrupted.append((f"slices[{i}].ranges", rng, "an array of [startLine, endLine] pairs"))
                    elif not isinstance(rng, list):
                        fmt_err = fmt_err or with_validation_example(
                            f"Error: slices[{i}].ranges must be an array of [startLine, endLine] pairs.",
                            TOOL_VALIDATION_EXAMPLES["read_slices"],
                        )
                    else:
                        slices.append(it)
    elif "slices" in args and args["slices"] is None:
        corrupted.append(("slices", None, "an array of slice objects"))

    if "lineRanges" in args and args["lineRanges"] is not None:
        raw_lr = args["lineRanges"]
        ok, kind, payload = parse_maybe_string_or_json_array(raw_lr)
        if not ok:
            if kind == "corrupted_literal" or isinstance(raw_lr, bool):
                corrupted.append(("lineRanges", payload if kind == "corrupted_literal" else raw_lr, "an array of 'path:start-end' strings"))
            elif fmt_err is None:
                fmt_err = with_validation_example(
                    f"Error: 'lineRanges' must be an array of 'path:start-end' strings ({payload}).",
                    TOOL_VALIDATION_EXAMPLES["read_slices_line_range"],
                )
        else:
            for i, it in enumerate(payload):
                if it is None or isinstance(it, bool):
                    corrupted.append((f"lineRanges[{i}]", it, "a 'path:start-end' string"))
                elif not isinstance(it, str) or not it.strip():
                    fmt_err = fmt_err or with_validation_example(
                        f"Error: lineRanges[{i}] must be a non-empty 'path:start-end' string.",
                        TOOL_VALIDATION_EXAMPLES["read_slices_line_range"],
                    )
                else:
                    parsed, err = _parse_slice_shorthand(it, f"lineRanges[{i}]")
                    if parsed:
                        slices.append(parsed)
                    elif fmt_err is None:
                        fmt_err = err
    elif "lineRanges" in args and args["lineRanges"] is None:
        corrupted.append(("lineRanges", None, "an array of 'path:start-end' strings"))

    if corrupted:
        return {
            "isError": True,
            "text": format_corrupted_array_error(
                "Validation failed",
                "read_slices",
                corrupted,
                "immediately fall back to parallel built-in read_file(StartLine, EndLine) calls.",
            ),
        }
    if fmt_err:
        return {"isError": True, "text": fmt_err}
    if not outlines and not slices:
        return {
            "isError": True,
            "text": with_validation_example(
                "Error: provide at least one entry in 'outlines', 'slices', or 'lineRanges'.",
                TOOL_VALIDATION_EXAMPLES["read_slices"],
            ),
        }
    if len(outlines) > MAX_OUTLINES or len(slices) > MAX_SLICE_FILES:
        return {
            "isError": True,
            "text": f"Error: limits exceeded (max {MAX_OUTLINES} outlines and {MAX_SLICE_FILES} slice files per call).",
        }

    base_cwd = resolve_path(str(args.get("cwd") or os.getcwd()))
    max_chars = clamp_int(args.get("maxChars"), 1000, DEFAULT_INLINE_MAX_BYTES, 8_500)
    max_bytes = DEFAULT_INLINE_MAX_BYTES

    def _rel(abs_p: str) -> str:
        try:
            r = to_posix_path(os.path.relpath(abs_p, base_cwd))
            return r if r and r != "." else os.path.basename(abs_p)
        except ValueError:
            return to_posix_path(abs_p)

    def _do_outline(raw_p: str) -> dict[str, Any]:
        abs_p = resolve_path(raw_p, base_cwd)
        rel_p = _rel(abs_p)
        try:
            data = read_text_file(abs_p)
            lines = data.content.replace("\r\n", "\n").split("\n")
            syms = extract_symbols(abs_p, data.content)
            hdr = f"## Outline: {rel_p} ({len(lines)} lines)"
            if not syms:
                return {"kind": "static", "full": f"{hdr}\n(no top-level symbols detected)"}
            body = "\n".join(_format_sym(s) for s in syms)
            return {"kind": "outline", "header": hdr, "symbols": syms, "full": f"{hdr}\n{body}"}
        except Exception as exc:
            return {"kind": "static", "full": f"## Outline: {rel_p}\nERROR: {exc}"}

    def _do_slice(req: dict[str, Any]) -> dict[str, Any]:
        raw_p = req.get("path")
        if not isinstance(raw_p, str) or not raw_p.strip():
            return {"kind": "static", "full": "## Slice: (invalid)\nERROR: 'path' must be a non-empty string"}
        abs_p = resolve_path(raw_p, base_cwd)
        rel_p = _rel(abs_p)
        rngs = req.get("ranges")
        if not isinstance(rngs, list) or not rngs:
            return {"kind": "static", "full": f"## Slice: {rel_p}\nERROR: 'ranges' must be a non-empty array of [startLine, endLine] pairs"}
        try:
            data = read_text_file(abs_p)
            lines = data.content.replace("\r\n", "\n").split("\n")
            total_lns = len(lines)
            blocks: list[str] = []
            for rng in rngs:
                if not isinstance(rng, (list, tuple)) or len(rng) < 2:
                    blocks.append("ERROR: each range must be [startLine, endLine]")
                    continue
                try:
                    rs, re_ = int(rng[0]), int(rng[1])
                except (ValueError, TypeError):
                    blocks.append(f"ERROR: invalid range {rng!r}")
                    continue
                s = max(1, rs)
                e = min(total_lns, re_)
                if s > total_lns:
                    blocks.append(f"--- L{rs}-L{re_} (out of bounds; file has {total_lns} lines) ---")
                    continue
                if e < s:
                    blocks.append(f"ERROR: startLine ({rs}) > endLine ({re_})")
                    continue
                w = len(str(e))
                snip = [f"--- L{s}-L{e} (of {total_lns}) ---"]
                for ln in range(s, e + 1):
                    snip.append(f"{str(ln).rjust(w)}: {lines[ln - 1]}")
                blocks.append("\n".join(snip))
            return {"kind": "static", "full": f"## Slice: {rel_p} ({total_lns} lines)\n" + "\n".join(blocks)}
        except Exception as exc:
            return {"kind": "static", "full": f"## Slice: {rel_p}\nERROR: {exc}"}

    entries: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        if outlines:
            entries.extend(list(pool.map(_do_outline, outlines)))
        if slices:
            entries.extend(list(pool.map(_do_slice, slices)))

    sep = "\n\n"
    sep_c = len(sep) * max(0, len(entries) - 1)
    sep_b = utf8_len(sep) * max(0, len(entries) - 1)
    c_sizes = [len(e["full"]) for e in entries]
    b_sizes = [utf8_len(e["full"]) for e in entries]
    if sum(c_sizes) + sep_c <= max_chars and sum(b_sizes) + sep_b <= max_bytes:
        return {"isError": False, "text": sep.join(e["full"] for e in entries)}

    tip = SLICE_TRUNCATION_TIP if slices else (OUTLINE_BATCH_TIP if len(outlines) > 20 else None)
    apply_tip = tip is not None and max_chars > len(tip) + 200
    body_mc = max_chars - (len(tip) if apply_tip and tip else 0)
    body_mb = max_bytes - (utf8_len(tip) if apply_tip and tip else 0)

    c_buds = compute_water_fill_budgets(c_sizes, max(0, body_mc - sep_c))
    b_buds = compute_water_fill_budgets(b_sizes, max(0, body_mb - sep_b))
    secs = [
        _pack_outline_to_budget(e["header"], e["symbols"], c_buds[i], b_buds[i])
        if (e["kind"] == "outline" and (c_sizes[i] > c_buds[i] or b_sizes[i] > b_buds[i]))
        else e["full"]
        for i, e in enumerate(entries)
    ]
    body = fit_sections_to_byte_budget(secs, body_mc, body_mb, sep)
    return {"isError": False, "text": f"{body}{tip}" if (apply_tip and tip) else body}


def run_quick_outline(args: dict[str, Any]) -> dict[str, Any]:
    raw_paths = args.get("paths")
    if raw_paths is None and "paths" not in args:
        raw_paths = args.get("outlines") if "outlines" in args else args.get("files")
    if raw_paths is None and "paths" not in args and "outlines" not in args and "files" not in args:
        return {
            "isError": True,
            "text": with_validation_example(
                "Error: provide at least one file path in 'paths'.",
                TOOL_VALIDATION_EXAMPLES["quick_outline"],
            ),
        }
    ok, kind, payload = parse_maybe_string_or_json_array(raw_paths)
    corrupted: list[tuple[str, Any, str]] = []
    if not ok:
        if kind == "corrupted_literal" or raw_paths is None or isinstance(raw_paths, bool):
            corrupted.append(("paths", payload if kind == "corrupted_literal" else raw_paths, "an array of file path strings"))
            return {
                "isError": True,
                "text": format_corrupted_array_error(
                    "Validation failed",
                    "quick_outline",
                    corrupted,
                    "immediately fall back to parallel built-in read_file(StartLine, EndLine) calls.",
                ),
            }
        return {
            "isError": True,
            "text": with_validation_example(
                f"Error: 'paths' must be an array of non-empty file path strings ({payload}).",
                TOOL_VALIDATION_EXAMPLES["quick_outline"],
            ),
        }
    for i, it in enumerate(payload):
        if it is None or isinstance(it, bool):
            corrupted.append((f"paths[{i}]", it, "a non-empty file path string"))
        elif not isinstance(it, str) or not it.strip():
            return {
                "isError": True,
                "text": with_validation_example(
                    f"Error: paths[{i}] must be a non-empty file path string.",
                    TOOL_VALIDATION_EXAMPLES["quick_outline"],
                ),
            }
    if corrupted:
        return {
            "isError": True,
            "text": format_corrupted_array_error(
                "Validation failed",
                "quick_outline",
                corrupted,
                "immediately fall back to parallel built-in read_file(StartLine, EndLine) calls.",
            ),
        }
    if not payload:
        return {
            "isError": True,
            "text": with_validation_example(
                "Error: provide at least one file path in 'paths'.",
                TOOL_VALIDATION_EXAMPLES["quick_outline"],
            ),
        }
    if len(payload) > MAX_OUTLINES:
        return {"isError": True, "text": f"Error: limit exceeded (max {MAX_OUTLINES} paths per call)."}
    return run_read_slices({"cwd": args.get("cwd"), "outlines": payload, "maxChars": args.get("maxChars")})
