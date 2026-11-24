#!/usr/bin/env bash
# Maintainer-only helper to regenerate expected.txt in tests/golden/<case>/.
# NEVER called by CI. Inspect `git diff tests/golden/` carefully after running.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

PY="python3"
command -v "$PY" >/dev/null 2>&1 || PY="python"

"$PY" - "$ROOT" "$T" "$@" <<'PYEOF'
import json
import os
import re
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
tmp = Path(sys.argv[2])
seed_buried = "--seed-buried" in sys.argv[3:]
wrap = root / "scripts" / "cmd_wrap.pl"
golden_dir = root / "tests" / "golden"
spill_dir = tmp / "spills"

def _buried_payload(cat: str, marker: str) -> tuple[int, str]:
    pad = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if cat == "http":
        rows = [{"id": f"evt_{i:04d}", "status": ("error" if i == 110 else "ok"), "msg": (marker if i == 110 else f"ok_{pad}")} for i in range(220)]
        return 1, json.dumps(rows, indent=2) + "\n"
    if cat == "git_log":
        commits = [f"commit {i:040x}\nAuthor: Dev <dev@example.test>\nDate: Mon Nov 24 2026\n\n    {marker if i == 30 else 'chore: commit ' + str(i) + ' ' + pad}\n" for i in range(60)]
        return 0, "\n".join(commits) + "\n"
    if cat == "git_diff":
        pre = ["diff --git a/src/app.py b/src/app.py", "@@ -1,3 +1,3 @@", f"-{marker}_OLD", f"+{marker}"]
        lock = ["diff --git a/package-lock.json b/package-lock.json"] + [f'+  "pkg-{i}": "1.0.{i} {pad}"' for i in range(250)]
        return 0, "\n".join(pre + lock) + "\n"
    rows: list[str] = []
    for i in range(260):
        if i == 130:
            rows.append(marker)
        if cat == "test":
            rows.append(f"tests/test_app.py::test_case_{i:04d} PASSED  # {pad[:32]}")
        elif cat == "find":
            rows.append(f"./packages/mod_{i % 8}/src/file_{i:04d}.ts")
        elif cat == "search":
            rows.append(f"src/mod_{i % 6}.py:{i + 1}: match_{i} {pad[:48]}")
        elif cat == "table":
            rows.append(f"| svc-{i:04d} | running | {pad[:40]} |")
        elif cat == "archive":
            rows.append(f"inflating: pkg/dir_{i % 10}/file_{i:04d}.js {pad[:40]}")
        else:
            rows.append(f"2026-11-24T12:00:{i%60:02d}Z INFO step={i:04d} status=ok payload={pad}")
    return (0 if cat == "archive" else 1), "\n".join(rows) + "\n"

if seed_buried:
    cats = ["archive","build","cat_large","chain","cloud","find","git_diff","git_log","git_net","http","iac","k8s_yaml","lint","logs","pkg","ps","pwsh","script","search","ssh","table","test"]
    for c in cats:
        marker = f"ERROR: BURIED_MUST_KEEP_{c.upper()}"
        rc, body = _buried_payload(c, marker)
        cdir = golden_dir / f"{c}_buried_error"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "input.txt").write_bytes(body.encode("utf-8"))
        (cdir / "meta.json").write_text(json.dumps({"cat": c, "rc": rc, "expect_spill": False, "must_contain": marker}, indent=2) + "\n", encoding="utf-8")

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
