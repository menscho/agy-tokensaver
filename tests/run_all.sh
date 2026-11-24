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

echo "=== vendor SHA-256 check ==="
"$PY" - <<'PYEOF' || FAILED=$((FAILED + 1))
import hashlib, re
from pathlib import Path
readme = Path("mcp_app/vendor/README.md").read_text(encoding="utf-8")
m = re.search(r"SHA-256:\*\*\s*`([0-9a-f]{64})`", readme)
assert m, "SHA-256 missing in mcp_app/vendor/README.md"
expected = m.group(1)
actual = hashlib.sha256(Path("mcp_app/vendor/ext-apps-with-deps.js").read_bytes()).hexdigest()
assert actual == expected, f"vendor hash mismatch: expected {expected} got {actual}"
print(f"PASS: vendor/ext-apps-with-deps.js SHA-256 matches ({actual[:16]}...)")
PYEOF

if [[ $FAILED -ne 0 ]]; then
  echo "SUMMARY: $FAILED suite(s) failed"
  exit 1
fi
echo "SUMMARY: all ${#SUITES[@]} test suites + vendor hash check passed"
