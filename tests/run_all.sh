#!/usr/bin/env bash
# Unified test runner for Linux, macOS, and Windows (Git Bash).
# Runs every test suite in tests/ plus the vendor SHA-256 integrity check.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="python3"
command -v "$PY" >/dev/null 2>&1 || PY="python"

SUITES=(
  tests/run_tests.sh
  tests/test_cmd_windows.sh
  tests/test_read.sh
  tests/test_rollover.sh
  tests/test_rules.sh
  tests/test_tools.sh
  tests/test_headroom_plexer.sh
  tests/test_json_parsing.sh
  tests/test_failopen.sh
  tests/test_cmd_semantics.sh
  tests/test_spill_safety.sh
  tests/test_golden.sh
  tests/test_buried_error_prop.sh
)

FAILED=0
for t in "${SUITES[@]}"; do
  echo "=== $t ==="
  if ! bash "$t"; then
    echo "SUITE FAILED: $t"
    FAILED=$((FAILED + 1))
  fi
done

echo "=== tests/test_python_unit.py ==="
if ! "$PY" -m unittest tests/test_python_unit.py; then
  echo "SUITE FAILED: tests/test_python_unit.py"
  FAILED=$((FAILED + 1))
fi

echo "=== version sync & vendor SHA-256 check ==="
"$PY" - <<'PYEOF' || FAILED=$((FAILED + 1))
import hashlib, json, re
from pathlib import Path
readme = Path("mcp_app/vendor/README.md").read_text(encoding="utf-8")
m = re.search(r"SHA-256:\*\*\s*`([0-9a-f]{64})`", readme)
assert m, "SHA-256 missing in mcp_app/vendor/README.md"
expected = m.group(1)
actual = hashlib.sha256(Path("mcp_app/vendor/ext-apps-with-deps.js").read_bytes()).hexdigest()
assert actual == expected, f"vendor hash mismatch: expected {expected} got {actual}"
print(f"PASS: vendor/ext-apps-with-deps.js SHA-256 matches ({actual[:16]}...)")

ver = json.loads(Path("plugin.json").read_text(encoding="utf-8"))["version"].lstrip("v")
root_readme = Path("README.md").read_text(encoding="utf-8")
assert f"release-v{ver}" in root_readme, f"README badge missing release-v{ver}"
assert f"agy-tokensaver-v{ver}.zip" in root_readme, f"README install zip missing v{ver}"
changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")
cl_m = re.search(r"^##\s+\[(\d+\.\d+\.\d+)\]", changelog, re.M)
assert cl_m and cl_m.group(1) == ver, f"CHANGELOG top version {cl_m.group(1) if cl_m else None} != plugin.json {ver}"
dash = Path("mcp_app/dashboard.html").read_text(encoding="utf-8")
assert f"agy-tokensaver v{ver}" in dash, f"dashboard.html headline missing v{ver}"
assert f"Release Notes (v{ver})" in dash, f"dashboard.html notes missing v{ver}"
print(f"PASS: version v{ver} synced across plugin.json, README.md, CHANGELOG.md, and dashboard.html")
PYEOF

if [[ $FAILED -ne 0 ]]; then
  echo "SUMMARY: $FAILED suite(s) failed"
  exit 1
fi
echo "SUMMARY: all ${#SUITES[@]} test suites + vendor hash check passed"
