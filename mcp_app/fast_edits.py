"""Atomic multi-hunk & multi-file edit tool with optional inline verify (`apply_edits`).

Validates every hunk and overlap in memory before touching disk (0 files modified
on error in atomic mode), or commits valid files atomically per file under
`bestEffort: true` while returning closest-region / line-drift hints for failed
files. Preserves UTF-8 BOM, CRLF/LF/mixed line endings, permissions, and symlinks,
blocks `.git` internal metadata writes, and caps output strictly <= 8,800 UTF-8 bytes.

Incorporates engineering patterns adapted from INK-Q/agy-fast (MIT License).
"""

from __future__ import annotations

import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

from fast_utils import (
    DEFAULT_INLINE_MAX_BYTES,
    MCP_CALL_BUDGET_SEC,
    TOOL_VALIDATION_EXAMPLES,
    clamp_int,
    detect_utf16_bom,
    fit_sections_to_byte_budget,
    format_corrupted_array_error,
    format_exit_failure_label,
    has_utf8_bom,
    is_likely_binary,
    parse_maybe_json_array,
    prepare_compact_command_output,
    read_text_file,
    render_prepared_compact_command_output,
    resolve_path,
    run_shell_command,
    slice_safe_chars,
    to_posix_path,
    truncate_head_tail_with_byte_notice,
    truncate_with_byte_notice,
    utf8_len,
    with_validation_example,
)

MAX_NEARBY_LINES = 26


@dataclass
class _MatchSpan:
    start: int
    end: int
    replacement: str
    hunk_index: int


@dataclass
class _PlannedWrite:
    abs_path: str
    write_target_path: str
    rel_path: str
    existed: bool
    original_bytes: bytes | None
    original_mode: int | None
    original_symlink_target: str | None
    new_content: str
    hunk_count: int
    line_delta: int
    created: bool
    is_noop: bool


def _compute_line_starts(text: str) -> list[int]:
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _offset_to_line(line_starts: list[int], offset: int) -> int:
    lo, hi = 0, len(line_starts) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if line_starts[mid] <= offset:
            lo = mid + 1
        else:
            hi = mid - 1
    return max(1, hi + 1)


def _fmt_line_range(line_starts: list[int], offset: int, length: int, prefix: str = "L") -> str:
    s = _offset_to_line(line_starts, offset)
    e = _offset_to_line(line_starts, offset + length - 1 if length > 0 else offset)
    return f"{prefix}{s}" if s == e else f"{prefix}{s}-{prefix}{e}"


def _format_nearby_lines(text: str, start_line: int | None, end_line: int | None) -> str:
    if start_line is None and end_line is None:
        return ""
    lines = text.split("\n")
    frm = max(1, (start_line or end_line or 1) - 2)
    raw_to = min(len(lines), (end_line or start_line or 1) + 2)
    trunc = (raw_to - frm + 1) > MAX_NEARBY_LINES
    to = frm + MAX_NEARBY_LINES - 1 if trunc else raw_to
    snip = [
        f"    {ln}: {slice_safe_chars(lines[ln - 1], 157) + '...' if len(lines[ln - 1]) > 160 else lines[ln - 1]}"
        for ln in range(frm, to + 1)
    ]
    extra = f"\n    ... ({raw_to - to} more lines in range omitted)" if trunc else ""
    return f"\n  Nearby lines ({frm}-{to}):\n" + "\n".join(snip) + extra


def _extract_tokens(trimmed: str) -> list[str]:
    toks = re.findall(r"[A-Za-z0-9_$]{3,}", trimmed)
    if toks:
        return toks
    return [s for s in trimmed.split() if len(s) >= 2]


def _find_closest_region_snippet(
    norm_content: str,
    norm_target: str,
    range_start: int | None = None,
    range_end: int | None = None,
) -> str:
    file_lines = norm_content.split("\n")
    target_lines = [ln.strip() for ln in norm_target.split("\n") if ln.strip()]
    if not file_lines or not target_lines:
        return ""

    first_anchor = next((idx for idx, tl in enumerate(target_lines) if len(tl) >= 6), 0)
    longest_anchor = max(range(len(target_lines)), key=lambda idx: len(target_lines[idx]))
    anchor_ids = {first_anchor, longest_anchor}
    if len(target_lines[-1]) >= 8:
        anchor_ids.add(len(target_lines) - 1)
    anchors = [(target_lines[i], i, set(_extract_tokens(target_lines[i]))) for i in sorted(anchor_ids)]
    target_tok_sets = [set(_extract_tokens(tl)) for tl in target_lines]
    trimmed_file = [ln.strip() for ln in file_lines]

    scan_start = max(0, (range_start or 1) - 1)
    scan_end = min(len(trimmed_file) - 1, (range_end or len(trimmed_file)) - 1)
    if scan_start > scan_end:
        return ""

    win_len = min(len(target_lines), 10)
    score_ids = list(range(win_len)) + [i for i in sorted(anchor_ids) if i >= win_len]

    def _score(cand_start: int) -> float:
        total = 0.0
        for k in score_ids:
            fi = cand_start + k
            if fi > scan_end or fi >= len(trimmed_file):
                continue
            fl, tl = trimmed_file[fi], target_lines[k]
            if not fl or not tl:
                continue
            tt = target_tok_sets[k]
            hi_ent = len(tl) >= 12 and len(tt) >= 2
            if fl == tl:
                total += (8.0 + min(6.0, len(tl) / 14.0)) if hi_ent else 4.0
            elif len(tl) >= 8 and (tl in fl or fl in tl):
                total += 6.0 if hi_ent else 3.5
            elif tt:
                hits = sum(1 for tok in _extract_tokens(fl) if tok in tt)
                if hits >= 2 or (hits >= 1 and len(tt) <= 2 and hits == len(tt)):
                    total += (4.5 if hi_ent else 3.0) * (hits / len(tt))
        return total

    evaluated: set[int] = set()
    best_start = -1
    best_score = 0.0
    for i in range(scan_start, scan_end + 1):
        fl = trimmed_file[i]
        if not fl:
            continue
        for a_txt, a_idx, a_toks in anchors:
            matched = fl == a_txt or (len(a_txt) >= 8 and (a_txt in fl or fl in a_txt))
            if not matched and a_toks:
                hits = sum(1 for tok in _extract_tokens(fl) if tok in a_toks)
                matched = hits >= 2 or (hits >= 1 and len(a_toks) <= 2 and hits == len(a_toks))
            if not matched:
                continue
            cand = max(scan_start, min(scan_end, i - a_idx))
            if cand in evaluated:
                continue
            evaluated.add(cand)
            sc = _score(cand)
            if sc > best_score:
                best_score = sc
                best_start = cand

    if best_start < 0 or best_score < 2.5:
        return ""
    span = min(max(len(target_lines), 4), 8)
    frm = max(1, best_start + 1 - 2)
    to = min(len(file_lines), min(frm + 13, best_start + 1 + span + 2))
    snip = [
        f"    {ln}: {slice_safe_chars(file_lines[ln - 1], 157) + '...' if len(file_lines[ln - 1]) > 160 else file_lines[ln - 1]}"
        for ln in range(frm, to + 1)
    ]
    return f"\n  Closest match near line {best_start + 1} (lines {frm}-{to}):\n" + "\n".join(snip)


def _plan_hunks(norm_content: str, hunks: list[dict[str, Any]], display_path: str) -> tuple[str, int, list[_MatchSpan], list[str]]:
    line_starts = _compute_line_starts(norm_content)
    spans: list[_MatchSpan] = []
    errors: list[str] = []

    for idx, hunk in enumerate(hunks):
        if not isinstance(hunk, dict):
            errors.append(f"{display_path} hunk #{idx + 1}: must be an object with 'target' and 'replacement'")
            continue
        target = hunk.get("target")
        repl = hunk.get("replacement")
        if not isinstance(target, str) or not target:
            errors.append(f"{display_path} hunk #{idx + 1}: 'target' must be a non-empty string")
            continue
        if not isinstance(repl, str):
            errors.append(f"{display_path} hunk #{idx + 1}: 'replacement' must be a string")
            continue
        norm_target = target.replace("\r\n", "\n")
        norm_repl = repl.replace("\r\n", "\n")

        s_line = hunk.get("startLine")
        e_line = hunk.get("endLine")
        if s_line is not None and (isinstance(s_line, bool) or not isinstance(s_line, int) or s_line < 1):
            errors.append(f"{display_path} hunk #{idx + 1}: Invalid startLine: {s_line} (must be integer >= 1)")
            continue
        if e_line is not None and (isinstance(e_line, bool) or not isinstance(e_line, int) or e_line < 1):
            errors.append(f"{display_path} hunk #{idx + 1}: Invalid endLine: {e_line} (must be integer >= 1)")
            continue
        total_lines = len(line_starts)
        eff_s = s_line if s_line is not None else 1
        eff_e = min(total_lines, e_line) if e_line is not None else total_lines
        if eff_s > total_lines:
            win_start = win_end = len(norm_content)
        elif eff_e < eff_s:
            errors.append(f"{display_path} hunk #{idx + 1}: Invalid line range: startLine ({eff_s}) > endLine ({eff_e})")
            continue
        else:
            win_start = line_starts[eff_s - 1]
            win_end = line_starts[eff_e] if eff_e < total_lines else len(norm_content)

        found_offsets: list[int] = []
        pos = win_start
        while pos <= win_end - len(norm_target):
            hit = norm_content.find(norm_target, pos, win_end)
            if hit == -1:
                break
            found_offsets.append(hit)
            pos = hit + max(1, len(norm_target))

        if not found_offsets:
            has_range = s_line is not None or e_line is not None
            range_note = f" within lines {s_line or 1}-{e_line or total_lines}" if has_range else ""
            hint = ""
            if has_range:
                g_offs: list[int] = []
                gp = 0
                while gp <= len(norm_content) - len(norm_target) and len(g_offs) < 5:
                    gh = norm_content.find(norm_target, gp)
                    if gh == -1:
                        break
                    g_offs.append(gh)
                    gp = gh + max(1, len(norm_target))
                if g_offs:
                    locs = ", ".join(_fmt_line_range(line_starts, o, len(norm_target)) for o in g_offs)
                    hint = f" Exact match found outside range at {locs} — adjust startLine/endLine or omit them."
                elif (eff_e - eff_s + 1) > MAX_NEARBY_LINES - 4:
                    hint = (
                        _find_closest_region_snippet(norm_content, norm_target, eff_s, eff_e)
                        or _find_closest_region_snippet(norm_content, norm_target)
                        or _format_nearby_lines(norm_content, eff_s, eff_e)
                    )
                else:
                    hint = _format_nearby_lines(norm_content, s_line, e_line)
            else:
                hint = _find_closest_region_snippet(norm_content, norm_target)
            errors.append(f"{display_path} hunk #{idx + 1}: target text not found{range_note}.{hint}")
            continue

        if len(found_offsets) > 1 and not hunk.get("replaceAll"):
            locs = ", ".join(_fmt_line_range(line_starts, o, len(norm_target), "") for o in found_offsets[:10])
            errors.append(
                f"{display_path} hunk #{idx + 1}: target matched {len(found_offsets)} times (lines: {locs}). "
                "Provide startLine/endLine, more surrounding context, or set replaceAll: true."
            )
            continue

        for off in found_offsets:
            spans.append(_MatchSpan(off, off + len(norm_target), norm_repl, idx))

    if errors:
        return norm_content, 0, [], errors

    spans.sort(key=lambda sp: (sp.start, sp.end))
    for i in range(1, len(spans)):
        if spans[i].start < spans[i - 1].end:
            ln = _offset_to_line(line_starts, spans[i].start)
            errors.append(
                f"{display_path}: hunk #{spans[i - 1].hunk_index + 1} and hunk #{spans[i].hunk_index + 1} overlap near line {ln}."
            )
    if errors:
        return norm_content, 0, [], errors

    pieces: list[str] = []
    cursor = 0
    for sp in spans:
        pieces.append(norm_content[cursor : sp.start])
        pieces.append(sp.replacement)
        cursor = sp.end
    pieces.append(norm_content[cursor:])
    return "".join(pieces), len(spans), spans, []


def _apply_spans_mixed_eol(raw_content: str, norm_len: int, spans: list[_MatchSpan], fallback_eol: str) -> str:
    n2r = [0] * (norm_len + 1)
    ni = 0
    ri = 0
    rlen = len(raw_content)
    while ri < rlen:
        n2r[ni] = ri
        ni += 1
        if raw_content[ri] == "\r" and ri + 1 < rlen and raw_content[ri + 1] == "\n":
            ri += 2
        else:
            ri += 1
    n2r[ni] = rlen

    pieces: list[str] = []
    cur = 0
    for sp in spans:
        rs, re_ = n2r[sp.start], n2r[sp.end]
        pieces.append(raw_content[cur:rs])
        seg = raw_content[rs:re_]
        crlf = seg.count("\r\n")
        lf_only = seg.count("\n") - crlf
        if crlf > lf_only:
            eol = "\r\n"
        elif lf_only > crlf:
            eol = "\n"
        else:
            nl_after = raw_content.find("\n", re_)
            if nl_after != -1:
                eol = "\r\n" if (nl_after > 0 and raw_content[nl_after - 1] == "\r") else "\n"
            else:
                eol = fallback_eol
        pieces.append(sp.replacement.replace("\n", "\r\n") if eol == "\r\n" else sp.replacement)
        cur = re_
    pieces.append(raw_content[cur:])
    return "".join(pieces)


def _count_lines(text: str) -> int:
    if not text:
        return 0
    body = text[:-1] if text.endswith("\n") else text
    return body.count("\n") + 1


def is_git_internal_path(file_path: str) -> bool:
    for raw_seg in re.split(r"[\\/]+", file_path):
        if not raw_seg:
            continue
        seg = raw_seg.lower()
        if os.name == "nt":
            seg = re.sub(r"[. ]+$", "", re.sub(r":.*$", "", re.sub(r"[. ]+$", "", seg)))
        if seg == ".git":
            return True
    return False


def _canonicalize_target_path(abs_path: str) -> str:
    if os.path.exists(abs_path):
        try:
            return os.path.realpath(abs_path)
        except OSError:
            return abs_path
    trailing: list[str] = []
    cur = abs_path
    while True:
        parent = os.path.dirname(cur)
        if parent == cur:
            return abs_path
        trailing.append(os.path.basename(cur))
        cur = parent
        if os.path.exists(cur):
            try:
                return os.path.join(os.path.realpath(cur), *reversed(trailing))
            except OSError:
                return abs_path


def _find_missing_ancestor_dirs(directory: str) -> list[str]:
    missing: list[str] = []
    cur = os.path.abspath(directory)
    while not os.path.lexists(cur):
        missing.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return missing


def _commit_planned_writes(plans: list[_PlannedWrite]) -> None:
    written: list[_PlannedWrite] = []
    created_dirs: list[str] = []
    created_dir_set: set[str] = set()
    try:
        for plan in plans:
            if plan.is_noop:
                continue
            target = plan.write_target_path
            parent = os.path.dirname(target)
            for d in reversed(_find_missing_ancestor_dirs(parent)):
                if d not in created_dir_set:
                    created_dir_set.add(d)
                    created_dirs.append(d)
            os.makedirs(parent, exist_ok=True)
            tmp_path = os.path.join(parent, f".agy-tmp-{os.getpid()}-{int(time.time() * 1000)}-{secrets.token_hex(4)}")
            try:
                with open(tmp_path, "wb") as fh:
                    fh.write(plan.new_content.encode("utf-8"))
                if plan.original_mode is not None and os.name != "nt":
                    os.chmod(tmp_path, plan.original_mode)
                os.replace(tmp_path, target)
                written.append(plan)
            except Exception:
                try:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                except OSError:
                    pass
                raise
    except Exception:
        for item in reversed(written):
            t = item.write_target_path
            try:
                if item.original_symlink_target is not None:
                    if os.path.lexists(t):
                        os.unlink(t)
                    os.symlink(item.original_symlink_target, t)
                elif item.existed and item.original_bytes is not None:
                    with open(t, "wb") as fh:
                        fh.write(item.original_bytes)
                    if item.original_mode is not None and os.name != "nt":
                        os.chmod(t, item.original_mode)
                elif not item.existed and os.path.lexists(t):
                    os.unlink(t)
            except OSError:
                pass
        for d in reversed(created_dirs):
            try:
                os.rmdir(d)
            except OSError:
                pass
        raise


def _resolve_hunk_aliases(raw_hunk: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    errs: list[str] = []

    def _pick(canon: str, aliases: tuple[str, ...]) -> Any:
        has_c = canon in raw_hunk and raw_hunk[canon] is not None
        present = [k for k in aliases if k in raw_hunk and raw_hunk[k] is not None]
        if has_c:
            val = raw_hunk[canon]
            for ak in present:
                if raw_hunk[ak] != val:
                    errs.append(f"conflicting '{canon}' and alias '{ak}' (values differ; omit the alias or make them identical)")
                    return None
            return val
        if not present:
            return raw_hunk.get(canon)
        first = raw_hunk[present[0]]
        for ak in present[1:]:
            if raw_hunk[ak] != first:
                errs.append(f"conflicting aliases '{present[0]}' and '{ak}' (values differ; use '{canon}')")
                return None
        return first

    tgt = _pick("target", ("oldText", "old_string"))
    rep = _pick("replacement", ("newText", "new_string"))
    if errs:
        return None, errs
    out = dict(raw_hunk)
    out["target"] = tgt
    out["replacement"] = rep
    return out, []


def _format_error_bullets(heading: str, errors: list[str], max_bytes: int) -> str:
    full = f"{heading}\n- " + "\n- ".join(errors)
    if utf8_len(full) <= max_bytes:
        return full
    prefix = f"{heading}\n"
    avail = max_bytes - utf8_len(prefix)
    if avail <= 64 or len(errors) <= 1:
        return truncate_with_byte_notice(full, max_bytes)
    fitted = fit_sections_to_byte_budget([f"- {e}" for e in errors], avail, avail, "\n")
    res = f"{prefix}{fitted}"
    return res if utf8_len(res) <= max_bytes else truncate_with_byte_notice(res, max_bytes)


def _compact_summary_lines(header: str, lines: list[str], no_op: int, max_bytes: int) -> str:
    full = f"{header}\n" + "\n".join(lines)
    if utf8_len(full) <= max_bytes:
        return full
    written = len(lines) - no_op
    phrase = f"{written} written, {no_op} unchanged" if no_op else f"{written} written"
    prefix = f"{header}\n"
    kept: list[str] = []
    used = utf8_len(prefix)
    for i, ln in enumerate(lines):
        rem = len(lines) - (i + 1)
        omit = f"\n... (+{rem} more applied file(s) omitted; total {phrase})" if rem else ""
        lb = (1 if kept else 0) + utf8_len(ln)
        if used + lb + utf8_len(omit) > max_bytes:
            break
        kept.append(ln)
        used += lb
    if kept:
        om = len(lines) - len(kept)
        return f"{prefix}" + "\n".join(kept) + (f"\n... (+{om} more applied file(s) omitted; total {phrase})" if om else "")
    minimal = f"{header} ({phrase}; list omitted to preserve verify diagnostics)"
    return minimal if utf8_len(minimal) <= max_bytes else truncate_with_byte_notice(minimal, max_bytes)


def run_apply_edits(args: dict[str, Any]) -> dict[str, Any]:
    raw_edits_arg = args.get("edits")
    ok, kind, payload = parse_maybe_json_array(raw_edits_arg)
    if not ok:
        if kind == "corrupted_literal" or (kind == "wrong_type" and (isinstance(raw_edits_arg, bool) or raw_edits_arg is None)):
            return {
                "isError": True,
                "text": format_corrupted_array_error(
                    "Validation failed (0 files modified)",
                    "apply_edits",
                    [("edits", payload if kind == "corrupted_literal" else raw_edits_arg, "an array of edit objects")],
                    "immediately fall back to parallel built-in edit_file / write_file calls for these file(s).",
                ),
            }
        if kind == "invalid_json":
            return {
                "isError": True,
                "text": with_validation_example(
                    f"Error: 'edits' is not a valid JSON array (0 files modified): {payload}",
                    TOOL_VALIDATION_EXAMPLES["apply_edits"],
                ),
            }
        return {
            "isError": True,
            "text": with_validation_example(
                "Error: 'edits' must be a non-empty JSON array string (or array) of file edit objects.",
                TOOL_VALIDATION_EXAMPLES["apply_edits"],
            ),
        }

    raw_edits = payload
    if not raw_edits:
        return {
            "isError": True,
            "text": format_corrupted_array_error(
                "Validation failed (0 files modified)",
                "apply_edits",
                [("edits", raw_edits, "a non-empty array of edit objects")],
                "immediately fall back to parallel built-in edit_file / write_file calls for these file(s).",
            ),
        }
    if len(raw_edits) > 50:
        return {"isError": True, "text": "Error: 'edits' supports at most 50 file entries per call."}

    corrupted: list[tuple[str, Any, str]] = []
    norm_entries: list[dict[str, Any]] = []
    for i, entry in enumerate(raw_edits):
        if not isinstance(entry, dict):
            corrupted.append((f"edits[{i}]", entry, "an edit object"))
            continue
        norm_hunks: list[dict[str, Any]] | None = None
        alias_errs: list[tuple[int, str]] = []
        if "hunks" in entry and entry["hunks"] is not None:
            hok, hkind, hpay = parse_maybe_json_array(entry["hunks"])
            if not hok:
                corrupted.append((f"edits[{i}].hunks", hpay if hkind == "corrupted_literal" else entry["hunks"], "an array of hunk objects"))
                continue
            h_corrupt = False
            resolved_hunks: list[dict[str, Any]] = []
            for j, rh in enumerate(hpay):
                if not isinstance(rh, dict):
                    corrupted.append((f"edits[{i}].hunks[{j}]", rh, "a hunk object"))
                    h_corrupt = True
                    continue
                nh, errs = _resolve_hunk_aliases(rh)
                if errs:
                    for e in errs:
                        alias_errs.append((j, e))
                elif nh is not None:
                    resolved_hunks.append(nh)
            if h_corrupt:
                continue
            norm_hunks = resolved_hunks
        norm_entries.append(
            {
                "path": entry.get("path"),
                "hunks": norm_hunks,
                "create": entry.get("create"),
                "overwrite": entry.get("overwrite"),
                "alias_errs": alias_errs,
            }
        )
    if corrupted:
        return {
            "isError": True,
            "text": format_corrupted_array_error(
                "Validation failed (0 files modified)",
                "apply_edits",
                corrupted,
                "immediately fall back to parallel built-in edit_file / write_file calls for these file(s).",
            ),
        }

    # Validate optional verify spec up front
    verify_spec: dict[str, Any] | None = None
    raw_verify = args.get("verify")
    if raw_verify is not None:
        if isinstance(raw_verify, str):
            if raw_verify.strip():
                verify_spec = {"command": raw_verify.strip()}
        elif isinstance(raw_verify, dict):
            cmd = raw_verify.get("command")
            if not isinstance(cmd, str) or not cmd.strip():
                return {
                    "isError": True,
                    "text": with_validation_example(
                        "Validation failed (0 files modified):\n- 'verify.command' must be a non-empty string.",
                        TOOL_VALIDATION_EXAMPLES["apply_edits"],
                    ),
                }
            if raw_verify.get("cwd") is not None and not isinstance(raw_verify.get("cwd"), str):
                return {
                    "isError": True,
                    "text": with_validation_example(
                        "Validation failed (0 files modified):\n- 'verify.cwd' must be a string.",
                        TOOL_VALIDATION_EXAMPLES["apply_edits"],
                    ),
                }
            verify_spec = {
                "command": cmd.strip(),
                "cwd": raw_verify.get("cwd"),
                "timeoutSec": raw_verify.get("timeoutSec"),
            }
        else:
            return {
                "isError": True,
                "text": with_validation_example(
                    "Validation failed (0 files modified):\n- 'verify' must be a command string or { command, cwd?, timeoutSec? } object.",
                    TOOL_VALIDATION_EXAMPLES["apply_edits"],
                ),
            }

    base_cwd = resolve_path(str(args.get("cwd") or os.getcwd()))
    dry_run = bool(args.get("dryRun"))
    best_effort = bool(args.get("bestEffort"))

    if not dry_run and verify_spec and verify_spec.get("cwd"):
        vcwd = resolve_path(str(verify_spec["cwd"]), base_cwd)
        if not os.path.exists(vcwd):
            return {"isError": True, "text": f"Validation failed (0 files modified):\n- 'verify.cwd' directory not found: {verify_spec['cwd']}"}
        if not os.path.isdir(vcwd):
            return {"isError": True, "text": f"Validation failed (0 files modified):\n- 'verify.cwd' is not a directory: {verify_spec['cwd']}"}

    merged: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for i, entry in enumerate(norm_entries):
        raw_p = entry.get("path")
        if not isinstance(raw_p, str) or not raw_p.strip():
            errors.append(f"edits[{i}]: 'path' must be a non-empty string")
            for hidx, msg in entry["alias_errs"]:
                errors.append(f"edits[{i}] hunk #{hidx + 1}: {msg}")
            continue
        abs_p = resolve_path(raw_p, base_cwd)
        target_p = _canonicalize_target_path(abs_p)
        try:
            rel_p = to_posix_path(os.path.relpath(abs_p, base_cwd)) or os.path.basename(abs_p)
        except ValueError:
            rel_p = to_posix_path(abs_p)
        if entry["alias_errs"]:
            for hidx, msg in entry["alias_errs"]:
                errors.append(f"{rel_p} hunk #{hidx + 1}: {msg}")
            continue
        if is_git_internal_path(abs_p) or is_git_internal_path(target_p):
            errors.append(f"{rel_p}: refusing to modify Git internal metadata path (.git)")
            continue
        has_create = isinstance(entry.get("create"), str)
        has_hunks = isinstance(entry.get("hunks"), list) and len(entry["hunks"]) > 0
        if not has_create and not has_hunks:
            errors.append(f"{rel_p}: must specify either non-empty 'hunks' or 'create'")
            continue
        if has_create and has_hunks:
            errors.append(f"{rel_p}: cannot specify both 'create' and 'hunks' on the same entry")
            continue
        if target_p not in merged:
            merged[target_p] = {
                "abs_path": abs_p,
                "write_target_path": target_p,
                "rel_path": rel_p,
                "create": entry["create"] if has_create else None,
                "overwrite": bool(entry.get("overwrite")),
                "hunks": list(entry["hunks"]) if has_hunks else [],
            }
        else:
            if has_create or merged[target_p]["create"] is not None:
                errors.append(f"{rel_p}: duplicate entries cannot mix 'create' with other edits")
                continue
            merged[target_p]["hunks"].extend(entry["hunks"])

    if errors:
        ex_suffix = f"\nExample: {TOOL_VALIDATION_EXAMPLES['apply_edits']}"
        return {
            "isError": True,
            "text": with_validation_example(
                _format_error_bullets("Validation failed (0 files modified):", errors, DEFAULT_INLINE_MAX_BYTES - utf8_len(ex_suffix)),
                TOOL_VALIDATION_EXAMPLES["apply_edits"],
            ),
        }

    # Phase 1: validate and plan every file in memory
    plans: list[_PlannedWrite] = []
    for item in merged.values():
        tpath = item["write_target_path"]
        rpath = item["rel_path"]
        if item["create"] is not None:
            existed = False
            orig_bytes: bytes | None = None
            orig_mode: int | None = None
            orig_sym: str | None = None
            if os.path.lexists(tpath):
                if os.path.islink(tpath) and not os.path.exists(tpath):
                    existed = True
                    if not item["overwrite"]:
                        errors.append(f"{rpath}: dangling symlink already exists (set overwrite: true to replace it)")
                        continue
                    orig_sym = os.readlink(tpath)
                else:
                    if os.path.isdir(tpath):
                        errors.append(f"{rpath}: target path is a directory")
                        continue
                    existed = True
                    if not item["overwrite"]:
                        errors.append(f"{rpath}: file already exists (set overwrite: true to replace it)")
                        continue
                    st = os.stat(tpath)
                    orig_mode = st.st_mode & 0o7777
                    with open(tpath, "rb") as fh:
                        orig_bytes = fh.read()
                    utf16 = detect_utf16_bom(orig_bytes)
                    if utf16:
                        errors.append(f"{rpath}: UTF-16 encoded file is not supported [{utf16}]")
                        continue
                    if is_likely_binary(orig_bytes):
                        errors.append(f"{rpath}: Cannot overwrite binary file with text content")
                        continue
            had_bom = bool(orig_bytes and has_utf8_bom(orig_bytes))
            old_lns = _count_lines((orig_bytes[3:] if had_bom else orig_bytes).decode("utf-8", errors="replace")) if orig_bytes else 0
            c_txt = str(item["create"])
            new_c = f"\ufeff{c_txt}" if (had_bom and not c_txt.startswith("\ufeff")) else c_txt
            new_lns = _count_lines(c_txt[1:] if c_txt.startswith("\ufeff") else c_txt)
            plans.append(
                _PlannedWrite(
                    item["abs_path"], tpath, rpath, existed, orig_bytes, orig_mode, orig_sym,
                    new_c, 1, new_lns - old_lns, not existed, False,
                )
            )
            continue

        try:
            fdata = read_text_file(tpath)
        except Exception as exc:
            errors.append(f"{rpath}: {exc}")
            continue
        try:
            fdata.raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"{rpath}: file is not valid UTF-8 (convert to UTF-8 first or use create + overwrite: true)")
            continue

        norm_orig = fdata.raw_content.replace("\r\n", "\n")
        new_norm, applied, spans, h_errs = _plan_hunks(norm_orig, item["hunks"], rpath)
        if h_errs:
            errors.extend(h_errs)
            continue
        unchanged = new_norm == norm_orig
        new_c = ""
        is_noop = unchanged
        if not unchanged:
            final_c = (
                _apply_spans_mixed_eol(fdata.raw_content, len(norm_orig), spans, fdata.eol)
                if fdata.mixed_eol
                else (new_norm.replace("\n", "\r\n") if fdata.eol == "\r\n" else new_norm)
            )
            new_c = f"\ufeff{final_c}" if (fdata.has_bom and not final_c.startswith("\ufeff")) else final_c
            is_noop = new_c.encode("utf-8") == fdata.raw_bytes
        line_delta = 0 if unchanged else (_count_lines(new_norm) - _count_lines(norm_orig))
        plans.append(
            _PlannedWrite(
                item["abs_path"], tpath, rpath, True, fdata.raw_bytes, fdata.mode, None,
                new_c, applied, line_delta, False, is_noop,
            )
        )

    if errors and (not best_effort or not plans):
        prefix = "Edit aborted (0 files modified):" if best_effort else "Atomic edit aborted (0 files modified):"
        return {"isError": True, "text": _format_error_bullets(prefix, errors, DEFAULT_INLINE_MAX_BYTES)}

    if not dry_run:
        try:
            _commit_planned_writes(plans)
        except Exception as exc:
            return {"isError": True, "text": f"Write failed (changes rolled back): {exc}"}

    summary_lines = [
        f"- {p.rel_path}: unchanged (no-op: replacement identical to target)"
        if p.is_noop
        else f"- {p.rel_path}: {'created' if p.created else f'updated ({p.hunk_count} match' + ('es' if p.hunk_count != 1 else '') + ')'}, lines {'+' if p.line_delta >= 0 else ''}{p.line_delta}"
        for p in plans
    ]
    no_op_cnt = sum(1 for p in plans if p.is_noop)
    written_cnt = len(plans) - no_op_cnt

    if best_effort and errors:
        total_f = len(merged)
        failed_f = max(1, total_f - len(plans))
        unch_suf = f", {no_op_cnt} unchanged" if no_op_cnt else ""
        p_hdr = (
            f"Partial dry-run ({len(plans)}/{total_f} file(s) valid; {failed_f} file(s) failed):"
            if dry_run
            else f"Partial edit applied ({written_cnt}/{total_f} file(s) written{unch_suf}; {failed_f} file(s) failed — do NOT re-send applied files):"
        )
        f_hdr = "Failed (fix only these):" if dry_run else "Failed (retry ONLY these failed file(s); do NOT re-send applied files):"
        v_note = (
            "\n\nVerify skipped because failed file(s) remain; fix only the failed file(s) and re-run verify."
            if (not dry_run and verify_spec)
            else ""
        )
        failed_sec = _format_error_bullets(f_hdr, errors, max(256, DEFAULT_INLINE_MAX_BYTES - utf8_len(p_hdr) - utf8_len(v_note) - 200))
        rem_b = max(80, DEFAULT_INLINE_MAX_BYTES - utf8_len(p_hdr) - utf8_len(failed_sec) - utf8_len(v_note) - 8)
        applied_sec = _compact_summary_lines("Applied:", summary_lines, no_op_cnt, rem_b)
        assembled = f"{p_hdr}\n\n{failed_sec}\n\n{applied_sec}{v_note}"
        return {"isError": True, "text": truncate_with_byte_notice(assembled, DEFAULT_INLINE_MAX_BYTES)}

    header = (
        f"Dry run validated {len(plans)} file(s) (0 files modified):"
        if dry_run
        else (f"Applied edits to {written_cnt} file(s) ({no_op_cnt} unchanged):" if no_op_cnt else f"Applied edits to {len(plans)} file(s):")
    )
    applied_full = f"{header}\n" + "\n".join(summary_lines)

    if not dry_run and verify_spec:
        vcwd = resolve_path(str(verify_spec["cwd"]), base_cwd) if verify_spec.get("cwd") else base_cwd
        t_sec = clamp_int(verify_spec.get("timeoutSec") or args.get("verifyTimeoutSec"), 1, MCP_CALL_BUDGET_SEC, 60)
        max_v_chars = clamp_int(args.get("maxVerifyChars"), 300, 7500, 4000)
        vres = run_shell_command(verify_spec["command"], cwd=vcwd, timeout_ms=t_sec * 1000)
        status = (
            "TIMEOUT"
            if vres.timed_out
            else ("ERROR" if vres.error else ("PASS" if vres.exit_code == 0 else f"FAIL ({format_exit_failure_label(vres.exit_code, vres.sig)})"))
        )
        v_prefix = f"\n\nVerify [{status} in {vres.duration_ms}ms] `{verify_spec['command']}`:\n"
        prep = prepare_compact_command_output(vres.stdout, vres.stderr, vres.exit_code, vres.timed_out, vres.error, vres.sig)
        res_app = min(utf8_len(applied_full), DEFAULT_INLINE_MAX_BYTES - 512) if status == "PASS" else min(utf8_len(applied_full), 180)
        max_vb = max(256, DEFAULT_INLINE_MAX_BYTES - res_app - utf8_len(v_prefix))
        tc = min(max_v_chars, max_vb)
        formatted = render_prepared_compact_command_output(prep, tc)
        for _ in range(4):
            ab = utf8_len(formatted)
            if ab <= max_vb or tc <= 64:
                break
            tc = max(64, int(tc * (max_vb / ab) * 0.94))
            formatted = render_prepared_compact_command_output(prep, tc)
        if utf8_len(formatted) > max_vb:
            formatted = truncate_head_tail_with_byte_notice(formatted, max_vb)
        v_sec = f"{v_prefix}{formatted}"
        rem_app_b = max(64, DEFAULT_INLINE_MAX_BYTES - utf8_len(v_sec))
        app_sec = _compact_summary_lines(header, summary_lines, no_op_cnt, rem_app_b)
        comb = f"{app_sec}{v_sec}"
        return {"isError": False, "text": truncate_with_byte_notice(comb, DEFAULT_INLINE_MAX_BYTES)}

    return {"isError": False, "text": truncate_with_byte_notice(applied_full, DEFAULT_INLINE_MAX_BYTES)}
