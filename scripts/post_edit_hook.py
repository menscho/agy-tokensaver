#!/usr/bin/env python3
"""PostToolUse edit tracker: bumps editseq and invalidates edited file entries (python3 -S stdlib-only)."""

from __future__ import annotations

import os
import sys

from hook_common import (
    data_dir,
    decode_hook_input,
    get_str,
    path_key,
    read_stdin_capped,
    resolve_path,
    safe_cid,
    script_plugin_root,
    tool_input,
)


def main() -> int:
    plugin_root = script_plugin_root(__file__)
    doc = decode_hook_input(read_stdin_capped())
    if doc is not None:
        cid = get_str(doc, "conversationId") or ""
        if cid:
            ti = tool_input(doc)
            try:
                rdir = os.path.join(data_dir(plugin_root), "reads")
                os.makedirs(rdir, exist_ok=True)
                scid = safe_cid(cid)
                es_file = os.path.join(rdir, f"{scid}.editseq")
                seq = 0
                if os.path.isfile(es_file):
                    with open(es_file, "r", encoding="utf-8", errors="replace") as efh:
                        seq = int((efh.readline() or "0").strip() or "0")
                with open(es_file, "w", encoding="utf-8") as wfh:
                    wfh.write(f"{seq + 1}\n")
                for suffix in ("searches", "lastblk"):
                    try:
                        os.unlink(os.path.join(rdir, f"{scid}.{suffix}"))
                    except OSError:
                        pass

                path = get_str(ti, "Path", "TargetFile", "AbsolutePath") or ""
                if path:
                    pk = path_key(resolve_path(path, doc, ti))
                    rfile = os.path.join(rdir, f"{scid}.tsv")
                    if os.path.isfile(rfile):
                        with open(rfile, "r", encoding="utf-8", errors="replace") as rfh:
                            kept = [
                                rl.rstrip("\r\n")
                                for rl in rfh
                                if rl.rstrip("\r\n") and rl.split("\t", 1)[0] != pk
                            ]
                        with open(rfile, "w", encoding="utf-8") as ofh:
                            ofh.write(("\n".join(kept) + "\n") if kept else "")
            except Exception:
                pass
    sys.stdout.buffer.write(b"{}")
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
