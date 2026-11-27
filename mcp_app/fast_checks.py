"""Batched concurrent build/lint/test/diagnostic command runner (`run_checks`).

Executes up to 12 independent non-interactive shell commands in a bounded
worker pool under a shared 150s call budget, with compact pass collapsing,
green-phase folding, failure noise stripping, head-tail folding, and a strict
<= 8,800 UTF-8 byte inline cap.

Incorporates engineering patterns adapted from INK-Q/agy-fast (MIT License).
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from fast_utils import (
    DEFAULT_INLINE_MAX_BYTES,
    MCP_CALL_BUDGET_SEC,
    TOOL_VALIDATION_EXAMPLES,
    ExecResult,
    PreparedCompactOutput,
    clamp_int,
    compute_water_fill_budgets,
    fit_sections_to_byte_budget,
    format_compact_command_output,
    format_corrupted_array_error,
    format_exit_failure_label,
    normalize_terminal_output,
    parse_maybe_json_array,
    prepare_compact_command_output,
    render_prepared_compact_command_output,
    resolve_path,
    run_shell_command,
    slice_safe_chars,
    slice_utf8_bytes,
    truncate_head_tail_with_byte_notice,
    truncate_head_tail_with_notice,
    truncate_with_byte_notice,
    truncate_with_notice,
    utf8_len,
    with_validation_example,
)


@dataclass
class _NormCmd:
    index: int
    name: str
    command: str
    cwd: str
    timeout_ms: int


@dataclass
class _Outcome:
    spec: _NormCmd
    result: ExecResult | None
    passed: bool


def _cap_cmd(cmd: str, max_chars: int, max_bytes: int) -> str:
    if len(cmd) <= max_chars and utf8_len(cmd) <= max_bytes:
        return cmd
    return f"{slice_utf8_bytes(slice_safe_chars(cmd, max(8, max_chars - 3)), max(8, max_bytes - 3))}..."


def _format_cmd_header_prefix(hdr: str, cmd: str, char_share: int, byte_share: int) -> str:
    full = f"{hdr}\n$ {cmd}\n"
    hdr_b = utf8_len(hdr)
    max_pc = max(len(hdr) + 24, int(char_share * 0.35))
    max_pb = max(hdr_b + 24, int(byte_share * 0.35))
    if len(full) <= max_pc and utf8_len(full) <= max_pb:
        return full
    if (char_share - len(hdr) - 1) < 48 or (byte_share - hdr_b - 1) < 48:
        return f"{hdr}\n"
    disp = _cap_cmd(cmd, max(20, max_pc - len(hdr) - 4), max(20, max_pb - hdr_b - 4))
    return f"{hdr}\n$ {disp}\n"


def run_checks(args: dict[str, Any], budget_ms: int = MCP_CALL_BUDGET_SEC * 1000) -> dict[str, Any]:
    raw_cmds = args.get("commands")
    ok, kind, payload = parse_maybe_json_array(raw_cmds)
    if not ok:
        if kind == "corrupted_literal" or (kind == "wrong_type" and (isinstance(raw_cmds, bool) or raw_cmds is None)):
            return {
                "isError": True,
                "text": format_corrupted_array_error(
                    "Validation failed",
                    "run_checks",
                    [("commands", payload if kind == "corrupted_literal" else raw_cmds, "an array of command specs")],
                    "immediately fall back to a single compound run_command call.",
                ),
            }
        return {
            "isError": True,
            "text": with_validation_example(
                "Error: 'commands' must be a non-empty array of check commands.",
                TOOL_VALIDATION_EXAMPLES["run_checks"],
            ),
        }
    if not payload:
        return {
            "isError": True,
            "text": with_validation_example(
                "Error: 'commands' must be a non-empty array of check commands.",
                TOOL_VALIDATION_EXAMPLES["run_checks"],
            ),
        }
    if len(payload) > 12:
        return {"isError": True, "text": "Error: 'commands' supports at most 12 commands per call."}

    corrupted = [
        (f"commands[{i}]", c, "a command string or object ({ name?, command, cwd?, timeoutSec? })")
        for i, c in enumerate(payload)
        if not isinstance(c, (str, dict))
    ]
    if corrupted:
        return {
            "isError": True,
            "text": format_corrupted_array_error(
                "Validation failed",
                "run_checks",
                corrupted,
                "immediately fall back to a single compound run_command call.",
            ),
        }

    base_cwd = resolve_path(str(args.get("cwd") or os.getcwd()))
    if not os.path.exists(base_cwd):
        return {"isError": True, "text": f"Error: cwd does not exist: {base_cwd}"}
    if not os.path.isdir(base_cwd):
        return {"isError": True, "text": f"Error: cwd is not a directory: {base_cwd}"}

    concurrency = clamp_int(args.get("concurrency"), 1, 8, 4)
    def_timeout_sec = clamp_int(args.get("timeoutSec"), 1, MCP_CALL_BUDGET_SEC, 60)
    output_mode = "full" if args.get("outputMode") == "full" else "compact"
    max_chars = clamp_int(args.get("maxChars"), 500, DEFAULT_INLINE_MAX_BYTES, 8_000)
    max_bytes = DEFAULT_INLINE_MAX_BYTES

    norm_cmds: list[_NormCmd] = []
    for i, item in enumerate(payload):
        spec = {"command": item} if isinstance(item, str) else item
        cmd_str = spec.get("command")
        if not isinstance(cmd_str, str) or not cmd_str.strip():
            return {
                "isError": True,
                "text": with_validation_example(
                    f"Error: commands[{i}] must contain a non-empty 'command' string.",
                    TOOL_VALIDATION_EXAMPLES["run_checks"],
                ),
            }
        scwd = spec.get("cwd")
        if scwd is not None and not isinstance(scwd, str):
            return {"isError": True, "text": f"Error: commands[{i}].cwd must be a string path."}
        trimmed_cwd = (scwd or "").strip()
        cmd_cwd = resolve_path(trimmed_cwd, base_cwd) if trimmed_cwd else base_cwd
        if trimmed_cwd:
            if not os.path.exists(cmd_cwd):
                return {"isError": True, "text": f"Error: commands[{i}].cwd does not exist: {cmd_cwd}"}
            if not os.path.isdir(cmd_cwd):
                return {"isError": True, "text": f"Error: commands[{i}].cwd is not a directory: {cmd_cwd}"}
        t_sec = clamp_int(spec.get("timeoutSec"), 1, MCP_CALL_BUDGET_SEC, def_timeout_sec)
        nm = str(spec.get("name") or "").strip() or f"check-{i + 1}"
        norm_cmds.append(_NormCmd(i, nm, cmd_str.strip(), cmd_cwd, t_sec * 1000))

    safe_budget_ms = max(100, int(budget_ms)) if budget_ms > 0 else MCP_CALL_BUDGET_SEC * 1000
    wall_start = time.monotonic()
    deadline = wall_start + safe_budget_ms / 1000.0

    def _exec_one(spec: _NormCmd) -> _Outcome:
        rem_ms = int((deadline - time.monotonic()) * 1000)
        if rem_ms < 100:
            return _Outcome(spec, None, False)
        res = run_shell_command(spec.command, cwd=spec.cwd, timeout_ms=min(spec.timeout_ms, rem_ms))
        passed = not res.error and not res.timed_out and res.exit_code == 0
        return _Outcome(spec, res, passed)

    workers = min(concurrency, len(norm_cmds))
    if workers == 1:
        outcomes = [_exec_one(s) for s in norm_cmds]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(_exec_one, norm_cmds))

    wall_ms = int((time.monotonic() - wall_start) * 1000)
    passed_cnt = sum(1 for o in outcomes if o.passed)
    not_run_cnt = sum(1 for o in outcomes if o.result is None)
    failed_cnt = len(outcomes) - passed_cnt - not_run_cnt

    counts = [f"{passed_cnt}/{len(outcomes)} passed"]
    if failed_cnt > 0:
        counts.append(f"{failed_cnt} failed")
    if not_run_cnt > 0:
        counts.append(
            f"{not_run_cnt} not run (call time budget of {round(safe_budget_ms / 1000)}s used up; run them in another call)"
        )
    summary_hdr = f"Checks: {', '.join(counts)} (wall: {wall_ms}ms)"

    sep = "\n\n"
    body_budget = max(0, max_chars - len(summary_hdr) - len(sep))
    body_byte_budget = max(0, max_bytes - utf8_len(summary_hdr) - utf8_len(sep))
    avail_chars = max(0, body_budget - len(sep) * max(0, len(outcomes) - 1))
    avail_bytes = max(0, body_byte_budget - utf8_len(sep) * max(0, len(outcomes) - 1))

    def _fmt_not_run(spec: _NormCmd) -> str:
        base_h = f"## [NOT RUN] {spec.name}"
        pool_c = min(avail_chars, int(avail_chars * 0.35)) if failed_cnt > 0 else avail_chars
        share = pool_c // max(1, not_run_cnt)
        room = share - len(base_h) - 3
        if room < 16:
            return base_h if (len(base_h) <= share or share < 16) else f"{slice_safe_chars(base_h, share - 3)}..."
        cap = min(180, room)
        return f"{base_h}\n$ {_cap_cmd(spec.command, cap, cap)}"

    blocks: list[str] = [""] * len(outcomes)
    if output_mode == "full":
        entries: list[dict[str, Any]] = []
        for o in outcomes:
            if o.result is None:
                blk = _fmt_not_run(o.spec)
                entries.append({"not_run": True, "block": blk, "c": len(blk), "b": utf8_len(blk)})
            else:
                r = o.result
                badge = (
                    "ERROR"
                    if r.error
                    else ("TIMEOUT" if r.timed_out else ("PASS" if o.passed else f"FAIL ({format_exit_failure_label(r.exit_code, r.sig)})"))
                )
                hline = f"## [{badge}] {o.spec.name} ({r.duration_ms}ms)"
                comb = "\n".join(p for p in (normalize_terminal_output(r.stdout), normalize_terminal_output(r.stderr)) if p)
                body = f"spawn error: {r.error}" if r.error else (comb or "(no output)")
                full_b = f"{hline}\n$ {o.spec.command}\n{body}"
                entries.append(
                    {"not_run": False, "spec": o.spec, "hline": hline, "body": body, "full": full_b, "c": len(full_b), "b": utf8_len(full_b)}
                )
        c_buds = compute_water_fill_budgets([e["c"] for e in entries], avail_chars)
        b_buds = compute_water_fill_budgets([e["b"] for e in entries], avail_bytes)
        for i, e in enumerate(entries):
            if e["not_run"]:
                blocks[i] = e["block"]
            elif e["c"] <= c_buds[i] and e["b"] <= b_buds[i]:
                blocks[i] = e["full"]
            else:
                pref = _format_cmd_header_prefix(e["hline"], e["spec"].command, c_buds[i], b_buds[i])
                ac = max(0, c_buds[i] - len(pref))
                ab = max(0, b_buds[i] - utf8_len(pref))
                btxt = e["body"]
                if utf8_len(btxt) > ab:
                    btxt = truncate_head_tail_with_byte_notice(btxt, ab)
                if len(btxt) > ac:
                    btxt = truncate_head_tail_with_notice(btxt, ac)
                blocks[i] = f"{pref}{btxt}"
    else:
        pass_entries: list[tuple[int, str, str, str]] = []
        fail_entries: list[tuple[int, _NormCmd, str, str, PreparedCompactOutput, str, str]] = []
        nr_c = nr_b = p_c = p_b = 0
        for i, o in enumerate(outcomes):
            if o.result is None:
                blk = _fmt_not_run(o.spec)
                blocks[i] = blk
                nr_c += len(blk)
                nr_b += utf8_len(blk)
                continue
            r = o.result
            if o.passed:
                body = format_compact_command_output(r.stdout, r.stderr, r.exit_code, 300, r.timed_out, r.error, r.sig)
                chdr = f"- [PASS {r.duration_ms}ms] {o.spec.name}"
                fblk = f"{chdr} (`{o.spec.command}`): {body}"
                blocks[i] = fblk
                p_c += len(fblk)
                p_b += utf8_len(fblk)
                pass_entries.append((i, chdr, body, fblk))
                continue
            badge = (
                "ERROR"
                if r.error
                else ("TIMEOUT" if r.timed_out else f"FAIL ({format_exit_failure_label(r.exit_code, r.sig)})")
            )
            bline = f"## [{badge} {r.duration_ms}ms] {o.spec.name}"
            hdr = f"{bline}\n$ {o.spec.command}\n"
            prep = prepare_compact_command_output(r.stdout, r.stderr, r.exit_code, r.timed_out, r.error, r.sig)
            p1_body = render_prepared_compact_command_output(prep, max(body_budget, len(prep.body_text) + 256))
            fail_entries.append((i, o.spec, bline, hdr, prep, p1_body, f"{hdr}{p1_body}"))

        if fail_entries:
            avail_nr_c = max(0, avail_chars - nr_c)
            avail_nr_b = max(0, avail_bytes - nr_b)
            # Shrink verbose PASS summaries if they would starve FAIL diagnostics (<60% pool)
            if pass_entries:
                raw_fc = max(0, avail_nr_c - p_c)
                raw_fb = max(0, avail_nr_b - p_b)
                dem_fc = sum(len(fe[6]) for fe in fail_entries)
                dem_fb = sum(utf8_len(fe[6]) for fe in fail_entries)
                floor_c = min(int(avail_nr_c * 0.85), max(int(avail_nr_c * 0.6), sum(min(len(fe[3]), 120) + min(len(fe[5]), 160) for fe in fail_entries)))
                floor_b = min(int(avail_nr_b * 0.85), max(int(avail_nr_b * 0.6), sum(min(utf8_len(fe[3]), 120) + min(utf8_len(fe[5]), 160) for fe in fail_entries)))
                if raw_fc < min(dem_fc, floor_c) or raw_fb < min(dem_fb, floor_b):
                    pp_c = max(0, avail_nr_c - max(raw_fc, min(dem_fc, floor_c)))
                    pp_b = max(0, avail_nr_b - max(raw_fb, min(dem_fb, floor_b)))
                    pc_sh = compute_water_fill_budgets([len(pe[3]) for pe in pass_entries], pp_c)
                    pb_sh = compute_water_fill_budgets([utf8_len(pe[3]) for pe in pass_entries], pp_b)
                    p_c = p_b = 0
                    for pi, (idx, chdr, body, fblk) in enumerate(pass_entries):
                        sc, sb = pc_sh[pi], pb_sh[pi]
                        shrunk = fblk
                        if len(shrunk) > sc or utf8_len(shrunk) > sb:
                            nocmd = f"{chdr}: {body}"
                            if len(nocmd) <= sc and utf8_len(nocmd) <= sb:
                                shrunk = nocmd
                            else:
                                ac = sc - len(chdr) - 2
                                ab = sb - utf8_len(chdr) - 2
                                if ac >= 8 and ab >= 8:
                                    shrunk = f"{chdr}: {slice_utf8_bytes(slice_safe_chars(body, ac - 3), ab - 3)}..."
                                elif len(chdr) <= sc and utf8_len(chdr) <= sb:
                                    shrunk = chdr
                                else:
                                    shrunk = slice_utf8_bytes(slice_safe_chars(chdr, sc), sb)
                        blocks[idx] = shrunk
                        p_c += len(shrunk)
                        p_b += utf8_len(shrunk)

            fp_c = max(0, avail_nr_c - p_c)
            fp_b = max(0, avail_nr_b - p_b)
            cb = compute_water_fill_budgets([len(fe[6]) for fe in fail_entries], fp_c)
            bb = compute_water_fill_budgets([utf8_len(fe[6]) for fe in fail_entries], fp_b)
            for j, (idx, spec, bline, _hdr, prep, p1_body, _p1_blk) in enumerate(fail_entries):
                eff_h = _format_cmd_header_prefix(bline, spec.command, cb[j], bb[j])
                ac = max(0, cb[j] - len(eff_h))
                ab = max(0, bb[j] - utf8_len(eff_h))
                if len(p1_body) <= ac and utf8_len(p1_body) <= ab:
                    blocks[idx] = f"{eff_h}{p1_body}"
                    continue
                bpc = utf8_len(p1_body) / max(1, len(p1_body))
                target_c = min(ac, int(ab / max(1.0, bpc)))
                comp = render_prepared_compact_command_output(prep, target_c) if target_c > 0 else ""
                for _ in range(4):
                    cur_b = utf8_len(comp)
                    if cur_b <= ab or target_c <= 1:
                        break
                    basis = min(target_c, len(comp))
                    target_c = max(1, min(basis - 1, int(basis * (ab / cur_b) * 0.98)))
                    comp = render_prepared_compact_command_output(prep, target_c)
                blocks[idx] = f"{eff_h}{comp}"

    non_empty = [b for b in blocks if b]
    fitted = fit_sections_to_byte_budget(non_empty, body_budget, body_byte_budget, sep, "head-tail")
    text = truncate_with_notice(f"{summary_hdr}{sep}{fitted}", max_chars)
    text = truncate_with_byte_notice(text, max_bytes)
    return {"isError": False, "text": text}
