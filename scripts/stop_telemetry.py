#!/usr/bin/env python3
"""Stop telemetry hook: incremental byte-offset transcript scan with file lock (python3 -S stdlib-only)."""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from hook_common import (
    data_dir,
    decode_hook_input,
    decode_line,
    get_int,
    get_str,
    read_stdin_capped,
    script_plugin_root,
)

_fcntl: Any = None
try:
    import fcntl as _fcntl
except ImportError:
    pass


def _silent() -> int:
    sys.stdout.buffer.write(b"{}")
    sys.stdout.buffer.flush()
    return 0


def main() -> int:
    plugin_root = script_plugin_root(__file__)
    doc = decode_hook_input(read_stdin_capped())
    if doc is None:
        return _silent()

    tx = get_str(doc, "transcriptPath") or ""
    cid = get_str(doc, "conversationId") or ""
    term = get_str(doc, "terminationReason") or ""
    if not tx or not cid or not os.path.isfile(tx):
        return _silent()

    try:
        st = os.stat(tx)
        inode, fsize = int(st.st_ino), int(st.st_size)
        if fsize == 0:
            return _silent()

        dd = data_dir(plugin_root)
        os.makedirs(dd, exist_ok=True)
        with open(os.path.join(dd, "sessions.lock"), "a+b") as lk:
            if _fcntl is not None:
                _fcntl.flock(lk.fileno(), _fcntl.LOCK_EX)
            sfile = os.path.join(dd, "sessions.json")
            sessions: dict[str, Any] = {}
            if os.path.isfile(sfile) and os.path.getsize(sfile) > 0:
                try:
                    with open(sfile, "rb") as rfh:
                        parsed = json.loads(rfh.read().decode("utf-8"))
                    if not isinstance(parsed, dict):
                        return _silent()
                    sessions = parsed
                except Exception:
                    return _silent()

            rec = sessions.get(cid) if isinstance(sessions.get(cid), dict) else None
            offset = turns = tot_in = tot_cr = tot_out = warm_in = warm_cr = drops = tail_2049 = prev_cr = 0
            models: set[str] = set()
            if rec is not None and int(rec.get("_inode", -1)) == inode and int(rec.get("_offset", 0)) <= fsize:
                offset = int(rec.get("_offset", 0))
                if offset == fsize:
                    return _silent()
                turns = int(rec.get("turns", 0))
                tot_in = int(rec.get("uncached_input_tokens", 0))
                tot_cr = int(rec.get("cache_read_tokens", 0))
                tot_out = int(rec.get("output_tokens", 0))
                warm_in = int(rec.get("_warm_in", 0))
                warm_cr = int(rec.get("_warm_cr", 0))
                drops = int(rec.get("cache_drops", 0))
                tail_2049 = int(rec.get("tail_2049_hits", 0))
                prev_cr = int(rec.get("_prev_cr", 0))
                if isinstance(rec.get("models"), list):
                    models.update(str(m) for m in rec["models"])

            new_offset = offset
            with open(tx, "rb") as tfh:
                if offset > 0:
                    tfh.seek(offset)
                for line in tfh:
                    if not line.endswith(b"\n"):
                        break
                    new_offset += len(line)
                    if b'"PLANNER_RESPONSE"' not in line:
                        continue
                    obj = decode_line(line)
                    if obj is None or get_str(obj, "type") != "PLANNER_RESPONSE":
                        continue
                    in_t = get_int(obj, "input_tokens") or 0
                    cr_t = get_int(obj, "cache_read_tokens") or 0
                    out_t = get_int(obj, "output_tokens") or 0
                    if in_t == 0 and cr_t == 0 and out_t == 0:
                        continue
                    turns += 1
                    tot_in += in_t
                    tot_cr += cr_t
                    tot_out += out_t
                    mdl = get_str(obj, "model") or ""
                    if mdl:
                        models.add(mdl)
                    if turns > 1:
                        warm_in += in_t
                        warm_cr += cr_t
                        if prev_cr >= 4000 and cr_t < prev_cr * 0.8:
                            drops += 1
                        if 2048 <= in_t <= 2060 and cr_t >= 4000:
                            tail_2049 += 1
                    prev_cr = cr_t

            if turns > 0:
                denom = tot_in + tot_cr
                wden = warm_in + warm_cr
                sessions[cid] = {
                    "updated_ts": int(time.time()),
                    "turns": turns,
                    "models": sorted(models),
                    "uncached_input_tokens": tot_in,
                    "cache_read_tokens": tot_cr,
                    "output_tokens": tot_out,
                    "output_per_turn": tot_out // turns,
                    "cache_hit_rate": round(tot_cr / denom, 4) if denom else 0,
                    "warm_cache_hit_rate": round(warm_cr / wden, 4) if wden else 0,
                    "cache_drops": drops,
                    "tail_2049_hits": tail_2049,
                    "termination_reason": term,
                    "_inode": inode,
                    "_offset": new_offset,
                    "_warm_in": warm_in,
                    "_warm_cr": warm_cr,
                    "_prev_cr": prev_cr,
                }
                if len(sessions) > 200:
                    keep = sorted(sessions.keys(), key=lambda k: int(sessions[k].get("updated_ts", 0)), reverse=True)[:200]
                    sessions = {k: sessions[k] for k in keep}
                tmp = os.path.join(dd, f"sessions.{os.getpid()}.tmp")
                with open(tmp, "wb") as wfh:
                    wfh.write(json.dumps(sessions, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                os.replace(tmp, sfile)
    except Exception:
        pass
    return _silent()


if __name__ == "__main__":
    raise SystemExit(main())
