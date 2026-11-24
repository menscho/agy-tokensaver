#!/usr/bin/env bash
# Property / fuzz test (Phase 7.3):
# For random outputs across all 22 compressor categories containing a marked error line
# buried in the middle, cmd_wrap.pl output MUST always contain either the literal error
# marker line OR a [full: <spill_file>] pointer to the byte-exact raw output on disk.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

PY="python3"
command -v "$PY" >/dev/null 2>&1 || PY="python"

"$PY" - "$ROOT" "$T" <<'PYEOF'
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
tmp = Path(sys.argv[2])
wrap = root / "scripts" / "cmd_wrap.pl"
spill_dir = tmp / "spills"

env = dict(os.environ)
env["PLUGIN_DATA"] = str(tmp / "data")
env["AGY_TS_SPILL_DIR"] = str(spill_dir)

cats = [
    "archive", "build", "cat_large", "chain", "cloud", "find",
    "git_diff", "git_log", "git_net", "http", "iac", "k8s_yaml",
    "lint", "logs", "pkg", "ps", "pwsh", "script", "search",
    "ssh", "table", "test",
]

rng = random.Random(2026112407)
passed = 0
failed = 0

def make_noise_payload(cat: str, marker: str, n_before: int, n_after: int, ansi: bool, crlf: bool) -> bytes:
    nl = "\r\n" if crlf else "\n"
    if cat == "http" and rng.random() < 0.5:
        rows = []
        tot = n_before + n_after + 1
        err_idx = n_before
        for i in range(tot):
            rows.append({
                "id": f"item_{i:04d}",
                "status": "error" if i == err_idx else "ok",
                "msg": marker if i == err_idx else ("pad_" + "x" * rng.randint(40, 90)),
            })
        return (json.dumps(rows, indent=2) + "\n").encode("utf-8")

    lines: list[str] = []
    for i in range(n_before + n_after):
        if i == n_before:
            err_line = marker
            if ansi:
                err_line = f"\x1b[31m{marker}\x1b[0m"
            lines.append(err_line)
        pad = "p" * rng.randint(25, 95)
        if cat == "search":
            lines.append(f"src/mod_{i % 7}.py:{i + 1}: match_{i} {pad}")
        elif cat == "find":
            lines.append(f"./pkg_{i % 9}/sub/file_{i}.ts")
        elif cat == "table":
            lines.append(f"| svc-{i:03d} | healthy | {pad[:30]} |")
        elif cat == "test":
            lines.append(f"tests/test_suite.py::test_case_{i:04d} PASSED")
        elif cat == "git_log":
            lines.append(f"commit {i:040x}")
            lines.append("Author: Dev <dev@example.test>")
            lines.append(f"    chore: change {i} {pad[:40]}")
        else:
            lines.append(f"[{i:04d}] INFO worker step={i} status=ok payload={pad}")
    return (nl.join(lines) + nl).encode("utf-8")

# 3 randomized fuzz cases per category = 66 property checks
for trial in range(66):
    cat = cats[trial % len(cats)]
    marker = f"ERROR: PROP_ERR_{trial:03d}_{rng.randrange(100000, 999999)}"
    n_before = rng.randint(40, 220)
    n_after = rng.randint(40, 220)
    rc = rng.choice((0, 1, 2))
    raw_in = make_noise_payload(cat, marker, n_before, n_after, ansi=bool(trial % 3 == 0), crlf=bool(trial % 4 == 0))

    p = subprocess.run(
        ["perl", str(wrap), "--cat", cat, "--test-stdin", "--test-rc", str(rc)],
        input=raw_in,
        capture_output=True,
        env=env,
        timeout=10,
    )
    if p.returncode != rc:
        failed += 1
        print(f"FAIL: prop:{cat}#{trial} :: rc={p.returncode} expected {rc}")
        continue

    out = p.stdout.decode("utf-8", errors="replace")
    has_marker = marker in out
    m_sp = re.search(r"full: ([^\]]+)\]", out)
    has_valid_spill = False
    if m_sp:
        sp = Path(m_sp.group(1))
        has_valid_spill = sp.is_file() and (sp.read_bytes() == raw_in)

    if not has_marker and not has_valid_spill:
        failed += 1
        print(f"FAIL: prop:{cat}#{trial} :: dropped BOTH buried error marker {marker} AND spill file pointer")
        continue

    passed += 1

print(f"PASS={passed} FAIL={failed} (66 randomized trials across {len(cats)} categories)")
sys.exit(0 if failed == 0 else 1)
PYEOF
