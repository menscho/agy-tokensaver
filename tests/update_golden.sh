#!/usr/bin/env bash
# Maintainer-only helper to regenerate expected.txt in tests/golden/<case>/.
# NEVER called by CI. Inspect `git diff tests/golden/` carefully after running.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

PY="python3"
command -v "$PY" >/dev/null 2>&1 || PY="python"

"$PY" - "$ROOT" "$T" <<'PYEOF'
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

env = dict(os.environ)
env["PLUGIN_DATA"] = str(tmp / "data")
env["AGY_TS_SPILL_DIR"] = str(spill_dir)

cases = sorted(p for p in golden_dir.iterdir() if p.is_dir() and (p / "meta.json").is_file())
for cdir in cases:
    meta = json.loads((cdir / "meta.json").read_text(encoding="utf-8"))
    cat = meta["cat"]
    rc = int(meta.get("rc", 0))
    raw_in = (cdir / "input.txt").read_bytes()
    p = subprocess.run(
        ["perl", str(wrap), "--cat", cat, "--test-stdin", "--test-rc", str(rc)],
        input=raw_in,
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert p.returncode == rc, f"{cdir.name}: rc={p.returncode} != {rc}"
    out_txt = p.stdout.decode("utf-8", errors="replace")
    m = re.search(r"full: ([^\]]+)\]", out_txt)
    if m:
        sp = Path(m.group(1))
        assert sp.is_file() and sp.read_bytes() == raw_in, f"{cdir.name}: spill mismatch"
        out_txt = out_txt.replace(m.group(1), f"<SPILL_DIR>/{sp.name}")
        meta["expect_spill"] = True
    else:
        meta["expect_spill"] = False
    (cdir / "expected.txt").write_text(out_txt, encoding="utf-8")
    (cdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
print(f"Updated {len(cases)} golden fixtures.")
PYEOF
