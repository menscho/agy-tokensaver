#!/usr/bin/env bash
# Edge-case JSON::PP parsing tests for all hooks (Phase 0.1).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
S="$ROOT/scripts"
PY="$(command -v python3 || command -v python)"
T="$(mktemp -d)"
if command -v cygpath >/dev/null 2>&1 && "$PY" -c 'import os,sys; sys.exit(0 if os.name=="nt" else 1)'; then T="$(cygpath -m "$T")"; fi
trap 'rm -rf "$T"' EXIT
export PLUGIN_DATA="$T/data"

PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); echo "PASS: $1"; }
bad() { FAIL=$((FAIL+1)); echo "FAIL: $1 :: $2"; }

ALLOW='^\{"decision":"allow"\}$'
DENY='"decision":"deny"'

check() {
  local hook="$1" inp="$2" pat="$3" name="$4"
  local out rc
  out=$(printf '%s' "$inp" | "$PY" -S "$S/$hook" 2>"$T/err.txt"); rc=$?
  local err; err=$(cat "$T/err.txt")
  if [[ $rc -eq 0 && -z "$err" && "$out" =~ $pat ]]; then
    ok "$name"
  else
    bad "$name" "rc=$rc err=[$err] out=[$out]"
  fi
}

# Fixture files
printf 'x = 1\n' > "$T/safe.py"
printf '{"lockfileVersion":3}\n%.0s' $(seq 1 3000) > "$T/package-lock.json"
printf 'café content\n' > "$T/café.py"
printf '{"lockfileVersion":3}\n%.0s' $(seq 1 3000) > "$T/😀-package-lock.json"
mkdir -p "$T/café_dir/node_modules"

# 1. Key appearing inside a string value must NOT spoof JSON keys
check read_hook.py "$(printf '{"toolInput":{"Path":"%s","Note":"\\"Path\\":\\"%s\\""}}' "$T/safe.py" "$T/package-lock.json")" "$ALLOW" \
  "read_hook: key inside string value ignored"

# 2. Key nested inside another object must NOT leak into toolInput
check read_hook.py "$(printf '{"meta":{"Path":"%s"},"toolInput":{"Path":"%s"}}' "$T/package-lock.json" "$T/safe.py")" "$ALLOW" \
  "read_hook: sibling meta.Path ignored"
check read_hook.py "$(printf '{"toolInput":{"nested":{"Path":"%s"}}}' "$T/package-lock.json")" "$ALLOW" \
  "read_hook: deeply nested Path ignored"

# 3. \uXXXX escapes and UTF-16 surrogate pairs
check read_hook.py "$(printf '{"toolInput":{"Path":"%s/caf\\u00e9.py"}}' "$T")" "$ALLOW" \
  "read_hook: \\u00e9 decoded to UTF-8 path"
check find_hook.py "$(printf '{"toolInput":{"SearchDirectory":"%s/caf\\u00e9_dir"}}' "$T")" '"Excludes":\["node_modules"\]' \
  "find_hook: \\u00e9 directory path decoded and matched"
check read_hook.py "$(printf '{"toolInput":{"Path":"%s/\\uD83D\\uDE00-package-lock.json"}}' "$T")" '"EndLine":100' \
  "read_hook: UTF-16 surrogate pair decoded to valid UTF-8 emoji"

# 4. Duplicate keys (last-key-wins, matching JS JSON.parse / Python json.loads)
check read_hook.py "$(printf '{"toolInput":{"Path":"%s","Path":"%s"}}' "$T/safe.py" "$T/package-lock.json")" "$DENY" \
  "read_hook: duplicate key last-wins denies lockfile"
check read_hook.py "$(printf '{"toolInput":{"Path":"%s","Path":"%s"}}' "$T/package-lock.json" "$T/safe.py")" "$ALLOW" \
  "read_hook: duplicate key last-wins allows safe file"

# 5. Wrong types (numbers/bools/arrays/objects instead of expected types)
check read_hook.py '{"toolInput":{"Path":123,"StartLine":"bad","EndLine":[]}}' "$ALLOW" \
  "read_hook: wrong types fail open"
check find_hook.py "$(printf '{"toolInput":{"SearchDirectory":"%s/café_dir","Excludes":"not-array"}}' "$T")" '"Excludes":' \
  "find_hook: non-array Excludes ignored cleanly"

# 6. Invalid JSON and non-object JSON -> fail open
for bad_in in '{not json' '[]' '"just a string"' '42' 'null' '{"toolInput":['; do
  check read_hook.py "$bad_in" "$ALLOW" "read_hook: invalid/non-object JSON ($bad_in)"
done

# 7. Empty stdin -> fail open
for h in read_hook.py write_hook.py find_hook.py grep_hook.py; do
  check "$h" "" "$ALLOW" "$h: empty stdin allows"
done
for h in post_edit_hook.py post_invocation_alerts.py stop_telemetry.py; do
  check "$h" "" '^\{\}$' "$h: empty stdin returns {}"
done

# 8. 10 MB payload -> fail open well within timeout (<2000ms)
"$PY" - "$T/big10m.json" <<'PYEOF'
import sys
with open(sys.argv[1], "wb") as f:
    f.write(b'{"toolInput":{"Path":"/tmp/x","Pad":"')
    f.write(b'a' * (10 * 1024 * 1024))
    f.write(b'"}}')
PYEOF

for h in read_hook.py write_hook.py find_hook.py grep_hook.py; do
  t0=$("$PY" -c 'import time; print(int(time.time()*1000))')
  out=$("$PY" -S "$S/$h" < "$T/big10m.json" 2>"$T/err.txt"); rc=$?
  t1=$("$PY" -c 'import time; print(int(time.time()*1000))')
  ms=$((t1 - t0))
  err=$(cat "$T/err.txt")
  if [[ $rc -eq 0 && -z "$err" && "$out" =~ $ALLOW && $ms -lt 2000 ]]; then
    ok "$h: 10MB payload allowed in ${ms}ms"
  else
    bad "$h: 10MB payload" "rc=$rc ms=$ms err=[$err] out=[$out]"
  fi
done

printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
