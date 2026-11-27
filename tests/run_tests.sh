#!/usr/bin/env bash
# token-saver regression suite: read/write/find/grep guards, telemetry, handoff, rollover, mode=off bypass.
# Usage: bash tests/run_tests.sh   (exit 0 = all pass)
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
S="$ROOT/scripts"
PY="$(command -v python3 || command -v python)"
T="$(mktemp -d)"
if command -v cygpath >/dev/null 2>&1 && "$PY" -c 'import os,sys; sys.exit(0 if os.name=="nt" else 1)'; then T="$(cygpath -m "$T")"; fi
trap 'rm -rf "$T"' EXIT
export PLUGIN_DATA="$T/pdata"; mkdir -p "$PLUGIN_DATA"   # never pollute real telemetry
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); }
bad()  { FAIL=$((FAIL+1)); printf 'FAIL: %s\n  got: %s\n' "$1" "$2"; }

# hook <script> <json> <regex> <name>   (regex must match output)
hook()  { local o; o=$(printf '%s' "$2" | "$PY" -S "$S/$1" 2>&1); [[ "$o" =~ $3 ]] && ok || bad "$4" "$o"; }
# nhook: regex must NOT match
nhook() { local o; o=$(printf '%s' "$2" | "$PY" -S "$S/$1" 2>&1); [[ "$o" =~ $3 ]] && bad "$4" "$o" || ok; }
ALLOW='^\{"decision":"allow"\}$'

# ---------- read_hook ----------
seq 1 2000 | sed 's/^/line of code number /' > "$T/big.py"
printf 'x\n' > "$T/small.py"
printf '{"lockfileVersion":3}\n%.0s' $(seq 1 3000) > "$T/package-lock.json"
head -c 20000 /dev/urandom > "$T/blob.dat"
rj() { printf '{"toolInput":{"AbsolutePath":"%s"%s}}' "$1" "${2:-}"; }
hook read_hook.py "$(rj "$T/small.py")"                     "$ALLOW"               "small read allowed"
hook read_hook.py "$(rj "$T/big.py")"                       '"EndLine":250'        "big unbounded read capped (lossless default)"
hook read_hook.py "$(rj "$T/big.py" ',"StartLine":10,"EndLine":200')" "$ALLOW"     "bounded read allowed"
hook read_hook.py "$(rj "$T/package-lock.json")"            '"decision":"deny"'    "lockfile blocked"
hook read_hook.py "$(rj "$T/blob.dat")"                     '"decision":"deny"'    "binary blocked"
hook read_hook.py "$(rj "$T/nope.py")"                      "$ALLOW"               "missing file allowed"
hook read_hook.py "$(rj "$T/big.py" ',"ByteOffset":100')"   "$ALLOW"               "ByteOffset passthrough"

# ---------- write_hook ----------
seq 1 400 | sed 's/^/    value = compute_something(index) # line /' > "$T/w.py"
wj() { printf '{"toolInput":{"TargetFile":"%s","Overwrite":true,"CodeContent":"%s"}}' "$1" "$2"; }
hook write_hook.py "$(wj "$T/w.py" 'def f():\n    # ... existing code ...\n    pass')" '"decision":"deny"' "lazy truncation blocked"
hook write_hook.py "$(wj "$T/new.py" 'print(1)')"                                    "$ALLOW"            "new file allowed"

# ---------- find_hook ----------
hook find_hook.py '{"toolInput":{"SearchDirectory":"/x","Pattern":"*.py","Excludes":["a"]}}' "$ALLOW" "find explicit excludes kept"

# ---------- post_invocation / stop: never model-visible text ----------
hook post_invocation_alerts.py '{"invocationNum":1}' '^\{\}$' "alerts early-turn silent"
hook stop_telemetry.py '{}' '^\{\}$' "stop silent"

# --- Guards + Telemetry + Inspect ---

# --- read_hook v2.1
seq 1 1000 | sed 's/^/x = compute(value)  # some realistic python code line here/' > "$T/mid.py"
hook read_hook.py "$(rj "$T/mid.py" ',"StartLine":1,"EndLine":800')"   "$ALLOW" "explicit agent range respected"
hook read_hook.py "$(rj "$T/mid.py" ',"EndLine":600')"                 "$ALLOW" "explicit EndLine respected"
hook read_hook.py "$(rj "$T/mid.py")"                                  '"EndLine":250' "unbounded still capped"
{ echo "<style>$(head -c 5000 /dev/zero | tr '\0' 'a')</style>"; seq 1 2000 | sed 's/^/<p>normal html line<\/p>/'; } > "$T/page.html"
hook read_hook.py "$(rj "$T/page.html" ',"StartLine":900,"EndLine":950')" "$ALLOW" "narrow bounded read ignores long line elsewhere"
mkdir -p "$T/b/.system_generated/steps/3"; { head -c 30000 /dev/zero | tr '\0' 'm'; echo; } > "$T/b/.system_generated/steps/3/output.txt"
nhook read_hook.py "$(rj "$T/b/.system_generated/steps/3/output.txt" ',"StartLine":1,"EndLine":300')" '"decision":"deny"' "spillback never denied as dense"
mkdir -p "$T/build/proj"; seq 1 1500 | sed 's/^/fn line() { let x = 1; } \/\/ rust/' > "$T/build/proj/main.rs"
nhook read_hook.py "$(rj "$T/build/proj/main.rs")" '"decision":"deny"' "user source under build/ not denied"
mkdir -p "$T/node_modules/pkg"; seq 1 1500 | sed 's/^/Some readme prose line here./' > "$T/node_modules/pkg/README.md"
nhook read_hook.py "$(rj "$T/node_modules/pkg/README.md")" '"decision":"deny"' "vendor docs not denied"
echo "plain text" > "$T/notes.dat"
hook read_hook.py "$(rj "$T/notes.dat")" "$ALLOW" "text .dat not denied"
hook read_hook.py "$(rj "$T/blob.dat")" '"decision":"deny"' "binary .dat still denied"
o=$("$PY" "$S/stats.py" --days 1 2>&1); [[ "$o" != *Traceback* ]] && ok || bad "stats.py runs (no inspect shadowing)" "${o:0:200}"
echo '{"a":[1,2,3],"b":{"c":"d"}}' > "$T/x.json"; o=$("$PY" "$S/ts_inspect.py" json "$T/x.json" 2>&1); [[ $? -eq 0 && "$o" != *Traceback* ]] && ok || bad "ts_inspect.py json runs" "$o"

# --- write_hook v2.1: near-identical overwrite is logged, not denied (tokens already spent)
{ cat "$T/w.py"; echo "    extra = 1"; } > "$T/w2.txt"
c=$("$PY" -c 'import json,sys; print(json.dumps(open(sys.argv[1]).read())[1:-1])' "$T/w2.txt")
hook write_hook.py "$(wj "$T/w.py" "$c")" "$ALLOW" "huge near-identical overwrite allowed"
# --- find_hook v2.1: only inject excludes that exist; respect Pattern
mkdir -p "$T/proj/node_modules" "$T/proj/src"
hook  find_hook.py "{\"toolInput\":{\"SearchDirectory\":\"$T/proj\",\"Pattern\":\"*.js\"}}" '"Excludes":\["node_modules"\]' "find excludes only existing dirs"
hook  find_hook.py "{\"toolInput\":{\"SearchDirectory\":\"$T/proj/src\",\"Pattern\":\"*.js\"}}" "$ALLOW" "no excluded dirs -> no overwrite"
hook  find_hook.py "{\"toolInput\":{\"SearchDirectory\":\"$T/proj\",\"Pattern\":\"node_modules/react/*.js\"}}" "$ALLOW" "Pattern targeting node_modules respected"
# --- alerts: checkpoint suppresses false cache-drop alert
{ for n in 1 2 3; do echo "{\"type\":\"PLANNER_RESPONSE\",\"input_tokens\":1000,\"cache_read_tokens\":90000,\"output_tokens\":10,\"created_at\":\"2026-01-01T00:00:0${n}Z\"}"; done
  echo '{"type":"CHECKPOINT"}'; echo '{"type":"PLANNER_RESPONSE","input_tokens":30000,"cache_read_tokens":60000,"output_tokens":10,"created_at":"2026-01-01T00:00:09Z"}'; } > "$T/tx.jsonl"
hook post_invocation_alerts.py "{\"invocationNum\":9,\"transcriptPath\":\"$T/tx.jsonl\",\"conversationId\":\"t1\"}" '^\{\}$' "checkpoint suppresses cache-drop alert"

# --- read_hook: bounded slices, dedupe, YAML/JSONL caps ---
seq 1 300 | sed 's/^/def func_line(): return 12345  # realistic python code/' > "$T/dup.py"
rdj() { printf '{"toolInput":{"Path":"%s"%s},"stepIdx":%d,"conversationId":"dupconv"}' "$1" "$2" "$3"; }
hook read_hook.py "$(rdj "$T/dup.py" ',"StartLine":1,"EndLine":200' 10)"   "$ALLOW"            "initial bounded read allowed"
hook read_hook.py "$(rdj "$T/dup.py" ',"StartLine":50,"EndLine":120' 18)"  '"decision":"deny"' "superset-covered slice blocked within 12 steps"
hook read_hook.py "$(rdj "$T/dup.py" ',"StartLine":201,"EndLine":280' 19)" "$ALLOW"            "non-overlapping slice allowed"
hook read_hook.py "$(rdj "$T/dup.py" ',"StartLine":50,"EndLine":120' 35)"  "$ALLOW"            "stale >12 steps allowed"

# --- handoff_pack.py: exact conversation-id targeting (never mixes open chats; isolated on Windows too)
mkdir -p "$T/fakehome/.gemini/antigravity/brain/conv-A/.system_generated/logs" "$T/fakehome/.gemini/antigravity/brain/conv-B/.system_generated/logs" "$T/tmp"
echo '{"type":"USER_INPUT","content":"<USER_REQUEST>Fix auth bug in conv A</USER_REQUEST>"}' > "$T/fakehome/.gemini/antigravity/brain/conv-A/.system_generated/logs/transcript.jsonl"
sleep 0.05
echo '{"type":"USER_INPUT","content":"<USER_REQUEST>Unrelated chat B</USER_REQUEST>"}' > "$T/fakehome/.gemini/antigravity/brain/conv-B/.system_generated/logs/transcript.jsonl"
FAKE_HOME="$T/fakehome"
FAKE_TMP="$T/tmp"
if [[ "$(python3 -c 'import os; print(os.name)')" == "nt" ]] && command -v cygpath >/dev/null 2>&1; then
    FAKE_HOME="$(cygpath -w "$FAKE_HOME")"
    FAKE_TMP="$(cygpath -w "$FAKE_TMP")"
fi
ho=$(HOME="$FAKE_HOME" USERPROFILE="$FAKE_HOME" TMPDIR="$FAKE_TMP" TEMP="$FAKE_TMP" TMP="$FAKE_TMP" python3 "$S/handoff_pack.py" "$T" --conversation-id conv-A 2>&1)
[[ "$ho" == *"Fix auth bug in conv A"* && "$ho" != *"Unrelated chat B"* ]] && ok || bad "handoff targets exact conversation-id" "$ho"

# --- core.py: SHA-256 verification rejects mismatch and parses valid SHA256SUMS.txt
python3 - "$ROOT/mcp_app" <<'PYEOF' && ok || bad "sha256 verify valid" "failed"
import hashlib, sys
sys.path.insert(0, sys.argv[1])
import core
blob = b"release-zip-bytes"
h = hashlib.sha256(blob).hexdigest()
parsed = core.parse_sha256sums(f"{h}  agy-tokensaver-v1.0.0.zip\n", "agy-tokensaver-v1.0.0.zip")
assert parsed == h and core.verify_sha256_bytes(blob, parsed)
PYEOF
python3 - "$ROOT/mcp_app" <<'PYEOF' && ok || bad "sha256 verify rejects tamper" "failed"
import hashlib, sys
sys.path.insert(0, sys.argv[1])
import core
blob = b"release-zip-bytes"
h = hashlib.sha256(blob).hexdigest()
assert not core.verify_sha256_bytes(b"tampered", h)
assert core.parse_sha256sums("not-a-hash  agy-tokensaver-v1.0.0.zip\n", "agy-tokensaver-v1.0.0.zip") is None
PYEOF

# --- mode=off / AGY_TS_HOOKS=off disables PreToolUse hooks cleanly ---
seq 1 500 | sed 's/^/key_name: some_yaml_config_value_here_1234567890/' > "$T/big.yaml"
hook read_hook.py "$(rj "$T/big.yaml")"                             '"EndLine":120' "large unbounded yaml capped"
seq 1 250 | sed 's/^/{"key":"val_1234567890_abcdefghij_1234567890"}/' > "$T/mid10k.jsonl"
hook read_hook.py "$(rj "$T/mid10k.jsonl")"                         '"EndLine":120' "data file >8KB unbounded capped"
o=$(AGY_TS_HOOKS=off "$PY" -S "$S/read_hook.py" <<< "$(rj "$T/package-lock.json")"); [[ "$o" =~ $ALLOW ]] && ok || bad "AGY_TS_HOOKS=off allows lockfile" "$o"
printf '{"mode":"off"}\n' > "$PLUGIN_DATA/hud_config.json"
hook read_hook.py "$(rj "$T/package-lock.json")" "$ALLOW" "mode=off hud_config allows lockfile"
rm -f "$PLUGIN_DATA/hud_config.json"

# --- Regression tests: duplicate-read byte truncation, honest slice telemetry, CRLF lockfile diff
"$PY" -c 'print(("x" * 299 + "\n") * 300, end="")' > "$T/wide300.txt"
hook read_hook.py "$(printf '{"toolInput":{"Path":"%s","StartLine":1,"EndLine":250},"stepIdx":10,"conversationId":"trunc1"}' "$T/wide300.txt")" "$ALLOW" "wide-line 1-250 initial read allowed"
hook read_hook.py "$(printf '{"toolInput":{"Path":"%s","StartLine":240,"EndLine":240},"stepIdx":12,"conversationId":"trunc1"}' "$T/wide300.txt")" "$ALLOW" "truncated line 240 past 46KB NOT falsely blocked"
hook read_hook.py "$(printf '{"toolInput":{"Path":"%s","StartLine":10,"EndLine":10},"stepIdx":13,"conversationId":"trunc1"}' "$T/wide300.txt")" '"decision":"deny"' "delivered line 10 within prefix blocked"
"$PY" - "$PLUGIN_DATA/events.jsonl" <<'PYEOF' && ok || bad "1-line duplicate telemetry uses slice bytes minus overhead" "check failed"
import json, sys
recs = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
ev = [r for r in recs if r.get("session_id") == "trunc1" and r.get("category") == "duplicate_read"][-1]
assert 290 <= ev["raw_bytes"] <= 310, f"expected ~300B slice raw_bytes, got {ev}"
assert 60 <= ev["kept_bytes"] <= 140, f"expected (short) denial message bytes counted, got {ev}"
assert 0 <= ev["est_tokens_saved"] < 100, f"inflated token savings on 1-line block: {ev}"
PYEOF

"$PY" -c 'print(("y" * 39 + "\n") * 500, end="")' > "$T/short40.txt"
hook read_hook.py "$(printf '{"toolInput":{"Path":"%s","StartLine":1,"EndLine":50},"stepIdx":1,"conversationId":"neg1"}' "$T/short40.txt")" "$ALLOW" "short-line 1-50 initial read allowed"
hook read_hook.py "$(printf '{"toolInput":{"Path":"%s","StartLine":10,"EndLine":10},"stepIdx":2,"conversationId":"neg1"}' "$T/short40.txt")" '"decision":"deny"' "short-line 10 duplicate blocked"
"$PY" - "$PLUGIN_DATA/events.jsonl" <<'PYEOF' && ok || bad "tiny 1-line duplicate logs negative net tokens" "check failed"
import json, sys
recs = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
ev = [r for r in recs if r.get("session_id") == "neg1" and r.get("category") == "duplicate_read"][-1]
assert 35 <= ev["raw_bytes"] <= 45, f"expected ~40B raw_bytes, got {ev}"
assert ev["est_tokens_saved"] < 0, f"expected negative net tokens for 40B blocked slice, got {ev}"
PYEOF

python3 - "$ROOT/mcp_app" <<'PYEOF' && ok || bad "rollover simulation exact pre-rollover & allows regressions" "failed"
import sys
sys.path.insert(0, sys.argv[1])
import core
# Below-cap session: zero modeling drift (sim == actual exactly)
turns_short = [(15000, 0, 200), (2000, 15000, 200), (1800, 17000, 200), (2200, 18800, 200)]
si, sc, ro = core._simulate_rollover_session(turns_short, cap=110_000, rediscovery_tax=8_000)
assert ro == 0 and si == sum(t[0] for t in turns_short) and sc == sum(t[1] for t in turns_short), (si, sc, ro)
# High rediscovery tax can regress (sim_quota > actual_quota), never clamped to 0
turns_long = [(20000, 0, 100)] + [(1000, 115000, 100)] * 14
si2, sc2, ro2 = core._simulate_rollover_session(turns_long, cap=110_000, rediscovery_tax=100_000)
aq = sum(t[0] + t[1] * 0.05 for t in turns_long)
sq = si2 + sc2 * 0.05
assert ro2 >= 1 and sq > aq, f"expected regression sq({sq}) > aq({aq}), ro={ro2}"
PYEOF

echo "PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]]
