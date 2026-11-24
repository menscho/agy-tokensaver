#!/usr/bin/env bash
# Edge-case JSON::PP parsing tests for all hooks (Phase 0.1).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
S="$ROOT/scripts"
T="$(mktemp -d)"
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
  out=$(printf '%s' "$inp" | perl "$S/$hook" 2>"$T/err.txt"); rc=$?
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
check read_hook.pl "$(printf '{"toolInput":{"Path":"%s","Note":"\\"Path\\":\\"%s\\""}}' "$T/safe.py" "$T/package-lock.json")" "$ALLOW" \
  "read_hook: key inside string value ignored"
check cmd_hook.pl '{"toolInput":{"CommandLine":"pytest -k \"\\\"IsDaemon\\\":true\""}}' "--cat test" \
  "cmd_hook: IsDaemon inside CommandLine string does not skip wrap"
check swarm_hook.pl '{"toolInput":{"action":"status","brief":"notes with \"action\":\"add\""}}' "$ALLOW" \
  "swarm_hook: action:add inside string value does not trigger overwrite"

# 2. Key nested inside another object must NOT leak into toolInput
check read_hook.pl "$(printf '{"meta":{"Path":"%s"},"toolInput":{"Path":"%s"}}' "$T/package-lock.json" "$T/safe.py")" "$ALLOW" \
  "read_hook: sibling meta.Path ignored"
check read_hook.pl "$(printf '{"toolInput":{"nested":{"Path":"%s"}}}' "$T/package-lock.json")" "$ALLOW" \
  "read_hook: deeply nested Path ignored"
check cmd_hook.pl '{"meta":{"IsDaemon":true},"toolInput":{"CommandLine":"pytest -x"}}' "--cat test" \
  "cmd_hook: sibling meta.IsDaemon ignored"

# 3. Escaped quotes and backslashes
check cmd_hook.pl '{"toolInput":{"CommandLine":"pytest -k \"foo \\\"bar\\\"\""}}' "--cat test" \
  "cmd_hook: escaped quotes in CommandLine"
check swarm_hook.pl '{"toolInput":{"action":"add","brief":"fix \"quoted\" \\\\ path"}}' '"overwrite":\{"brief":' \
  "swarm_hook: escaped quotes and backslashes preserved"

# 4. \uXXXX escapes and UTF-16 surrogate pairs
check cmd_hook.pl '{"toolInput":{"CommandLine":"\u0070\u0079\u0074\u0065\u0073\u0074 -x"}}' "--cat test" \
  "cmd_hook: \\uXXXX escaped pytest bypasses fast-path and wraps"
check read_hook.pl "$(printf '{"toolInput":{"Path":"%s/caf\\u00e9.py"}}' "$T")" "$ALLOW" \
  "read_hook: \\u00e9 decoded to UTF-8 path"
check find_hook.pl "$(printf '{"toolInput":{"SearchDirectory":"%s/caf\\u00e9_dir"}}' "$T")" '"Excludes":\["node_modules"\]' \
  "find_hook: \\u00e9 directory path decoded and matched"
check swarm_hook.pl '{"toolInput":{"action":"add","brief":"ship \uD83D\uDE00 feature"}}' 'ship 😀 feature' \
  "swarm_hook: UTF-16 surrogate pair decoded to valid UTF-8 emoji"

# 5. Duplicate keys (last-key-wins, matching JS JSON.parse / Python json.loads)
check read_hook.pl "$(printf '{"toolInput":{"Path":"%s","Path":"%s"}}' "$T/safe.py" "$T/package-lock.json")" "$DENY" \
  "read_hook: duplicate key last-wins denies lockfile"
check read_hook.pl "$(printf '{"toolInput":{"Path":"%s","Path":"%s"}}' "$T/package-lock.json" "$T/safe.py")" "$ALLOW" \
  "read_hook: duplicate key last-wins allows safe file"
check cmd_hook.pl '{"toolInput":{"CommandLine":"echo ok","CommandLine":"pytest -x"}}' "--cat test" \
  "cmd_hook: duplicate key last-wins wraps pytest"

# 6. Wrong types (numbers/bools/arrays/objects instead of expected types)
check read_hook.pl '{"toolInput":{"Path":123,"StartLine":"bad","EndLine":[]}}' "$ALLOW" \
  "read_hook: wrong types fail open"
check cmd_hook.pl '{"toolInput":{"CommandLine":12345,"IsDaemon":"true"}}' "$ALLOW" \
  "cmd_hook: non-string CommandLine fails open"
check cmd_hook.pl '{"toolInput":{"CommandLine":"pytest -x","IsDaemon":"true"}}' "--cat test" \
  "cmd_hook: string \"true\" IsDaemon is not boolean true"
check find_hook.pl "$(printf '{"toolInput":{"SearchDirectory":"%s/café_dir","Excludes":"not-array"}}' "$T")" '"Excludes":' \
  "find_hook: non-array Excludes ignored cleanly"

# 7. Invalid JSON and non-object JSON -> fail open
for bad_in in '{not json' '[]' '"just a string"' '42' 'null' '{"toolInput":['; do
  check read_hook.pl "$bad_in" "$ALLOW" "read_hook: invalid/non-object JSON ($bad_in)"
  check cmd_hook.pl  "$bad_in" "$ALLOW" "cmd_hook: invalid/non-object JSON ($bad_in)"
done

# 8. Empty stdin -> fail open
for h in read_hook.pl cmd_hook.pl write_hook.pl find_hook.pl grep_hook.pl board_hook.pl swarm_hook.pl; do
  check "$h" "" "$ALLOW" "$h: empty stdin allows"
done
for h in post_edit_hook.pl post_invocation_alerts.pl stop_telemetry.pl; do
  check "$h" "" '^\{\}$' "$h: empty stdin returns {}"
done

# 9. 10 MB payload -> fail open well within timeout (<2000ms)
python3 - "$T/big10m.json" <<'PYEOF'
import sys
with open(sys.argv[1], "wb") as f:
    f.write(b'{"toolInput":{"Path":"/tmp/x","Pad":"')
    f.write(b'a' * (10 * 1024 * 1024))
    f.write(b'"}}')
PYEOF

for h in read_hook.pl cmd_hook.pl write_hook.pl find_hook.pl grep_hook.pl board_hook.pl swarm_hook.pl; do
  t0=$(python3 -c 'import time; print(int(time.time()*1000))')
  out=$(perl "$S/$h" < "$T/big10m.json" 2>"$T/err.txt"); rc=$?
  t1=$(python3 -c 'import time; print(int(time.time()*1000))')
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
