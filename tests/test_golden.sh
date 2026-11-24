#!/usr/bin/env bash
# Golden-file regression suite for every cmd_wrap.pl compressor (Phase 2.2).
# Runs every tests/golden/<case>/ fixture through cmd_wrap.pl and asserts:
#   1. Exact exit code preserved
#   2. Normalized stdout matches expected.txt byte-for-byte
#   3. Whenever a spill file is written, its raw bytes equal input.txt byte-for-byte
# CI runs this script read-only; maintainers regenerate fixtures with tests/update_golden.sh.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

PY="python3"
command -v "$PY" >/dev/null 2>&1 || PY="python"

"$PY" - "$ROOT" "$T" <<'PYEOF'
import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
tmp = Path(sys.argv[2])
wrap = root / "scripts" / "cmd_wrap.pl"
golden_dir = root / "tests" / "golden"
spill_dir = tmp / "spills"

cases = sorted(p for p in golden_dir.iterdir() if p.is_dir() and (p / "meta.json").is_file())
assert len(cases) >= 66, f"expected >=66 golden cases (22 cats x 3 incl. buried_error), got {len(cases)}"

env = dict(os.environ)
env["PLUGIN_DATA"] = str(tmp / "data")
env["AGY_TS_SPILL_DIR"] = str(spill_dir)

passed = 0
failed = 0

def normalize_spill(text: str, sdir: Path) -> tuple[str, Path | None]:
    m = re.search(r"full: ([^\]]+)\]", text)
    if not m:
        return text, None
    sp = Path(m.group(1))
    norm = text.replace(m.group(1), f"<SPILL_DIR>/{sp.name}")
    return norm, sp

for cdir in cases:
    meta = json.loads((cdir / "meta.json").read_text(encoding="utf-8"))
    cat = meta["cat"]
    rc = int(meta.get("rc", 0))
    raw_in = (cdir / "input.txt").read_bytes()
    exp_out = (cdir / "expected.txt").read_bytes().decode("utf-8")
    exp_spill = bool(meta.get("expect_spill", False))

    p = subprocess.run(
        ["perl", str(wrap), "--cat", cat, "--test-stdin", "--test-rc", str(rc)],
        input=raw_in,
        capture_output=True,
        env=env,
        timeout=10,
    )
    if p.returncode != rc:
        failed += 1
        print(f"FAIL: golden:{cdir.name} :: rc={p.returncode} expected {rc}")
        continue
    out_txt = p.stdout.decode("utf-8", errors="replace")
    norm_out, sp_path = normalize_spill(out_txt, spill_dir)
    if norm_out != exp_out:
        failed += 1
        diff = "".join(difflib.unified_diff(
            exp_out.splitlines(keepends=True),
            norm_out.splitlines(keepends=True),
            fromfile="expected.txt", tofile="actual", n=2,
        ))
        print(f"FAIL: golden:{cdir.name} :: output mismatch\n{diff[:600]}")
        continue
    if exp_spill:
        if sp_path is None or not sp_path.is_file():
            failed += 1
            print(f"FAIL: golden:{cdir.name} :: expected spill file missing")
            continue
        if sp_path.read_bytes() != raw_in:
            failed += 1
            print(f"FAIL: golden:{cdir.name} :: spill bytes != input.txt")
            continue
    if cdir.name.endswith("_buried_error"):
        must_sig = str(meta.get("must_contain", "ERROR: BURIED_MUST_KEEP"))
        if must_sig not in out_txt and sp_path is None:
            failed += 1
            print(f"FAIL: golden:{cdir.name} :: buried error {must_sig!r} lost without spill pointer")
            continue
        if must_sig not in out_txt:
            failed += 1
            print(f"FAIL: golden:{cdir.name} :: buried error {must_sig!r} omitted from compressed output")
            continue
    passed += 1
    print(f"PASS: golden:{cdir.name}")

print(f"PASS={passed} FAIL={failed}")
sys.exit(0 if failed == 0 else 1)
PYEOF
