#!/usr/bin/env bash
# read_hook.pl dedupe / overlap-trim regression suite (epoch-verified window, MD5 fingerprint,
# cap-then-reread, fail-open). Usage: bash tests/test_read.sh   (exit 0 = all pass)
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
S="$ROOT/scripts"
PY="$(command -v python3 || command -v python)"
T="$(mktemp -d)"
if command -v cygpath >/dev/null 2>&1 && "$PY" -c 'import os,sys; sys.exit(0 if os.name=="nt" else 1)'; then T="$(cygpath -m "$T")"; fi
trap 'rm -rf "$T"' EXIT
export PLUGIN_DATA="$T/pdata"; mkdir -p "$PLUGIN_DATA"
export HOME="$T/home"; mkdir -p "$HOME"          # fake ~/.gemini: never touch real transcripts
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); }
bad() { FAIL=$((FAIL+1)); printf 'FAIL: %s\n  got: %s\n' "$1" "$2"; }
hook()  { local o; o=$(printf "%s" "$1" | "$PY" -S "$S/read_hook.py" 2>&1); [[ "$o" =~ $2 ]] && ok || bad "$3" "$o"; }
ALLOW='^\{"decision":"allow"\}$'
DENY='"decision":"deny"'
# rd <file> <cid> <step> [extra json]
rd() { printf '{"toolInput":{"Path":"%s"%s},"stepIdx":%d,"conversationId":"%s"%s}' "$1" "${4:-}" "$3" "$2" "${5:-}"; }
# mktx <cid> <app> <lines...>: writes a fake transcript.jsonl
mktx() { local d="$HOME/.gemini/$2/brain/$1/.system_generated/logs"; mkdir -p "$d"; shift 2; printf '%s\n' "$@" > "$d/transcript.jsonl"; }
pl() { printf '{"step_index":%d,"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE"%s,"tool_calls":[{"name":"%s","args":{"Path":"\\"x\\""}}]}' "$1" "${3:-}" "$2"; }
gen() { printf '{"step_index":%d,"source":"MODEL","type":"GENERIC","status":"DONE","content":"%s"}' "$1" "${2:-ok}"; }
cp_() { printf '{"step_index":%d,"source":"SYSTEM","type":"CHECKPOINT","status":"DONE","content":"{{ CHECKPOINT 0 }}"}' "$1"; }

seq 1 300 | sed 's/^/def func_line(): return 12345  # realistic python code/' > "$T/a.py"   # ~21KB, 300 lines

# --- 1. legacy window still works without any transcript
hook "$(rd "$T/a.py" c1 10 ',"StartLine":1,"EndLine":200')"  "$ALLOW" "legacy: first read allowed"
hook "$(rd "$T/a.py" c1 20 ',"StartLine":50,"EndLine":120')" "$DENY"  "legacy: covered slice within 16 steps denied"
hook "$(rd "$T/a.py" c1 40 ',"StartLine":50,"EndLine":120')" "$ALLOW" "legacy: >16 steps without transcript allowed"

# --- 2. transcript-verified same epoch: any distance
mktx c2 antigravity "$(pl 9 view_file)" "$(gen 10)" "$(pl 11 run_command)" "$(gen 12)" "$(pl 59 view_file)"
hook "$(rd "$T/a.py" c2 10 ',"StartLine":1,"EndLine":200')"  "$ALLOW" "verified: first read allowed"
hook "$(rd "$T/a.py" c2 60 ',"StartLine":50,"EndLine":120')" "$DENY"  "verified: covered slice 50 steps later denied"
o=$(printf '%s' "$(rd "$T/a.py" c2 60 ',"StartLine":50,"EndLine":120')" | "$PY" -S "$S/read_hook.py")
[[ "$o" == *unchanged*"in context"* && ${#o} -lt 160 ]] && ok || bad "dedupe reason short and recognisable" "$o"
[[ "$o" == *"from step 10, lines 1-200."* && "$o" != *"your read of"* ]] && ok || bad "dedupe reason names covering step+range once" "$o"

# CHECKPOINT between the reads -> allowed (content was truncated from context)
mktx c3 antigravity "$(pl 9 view_file)" "$(gen 10)" "$(cp_ 30)" "$(pl 31 run_command)" "$(gen 32)" "$(pl 59 view_file)"
hook "$(rd "$T/a.py" c3 10 ',"StartLine":1,"EndLine":200')"  "$ALLOW" "checkpoint: first read allowed"
hook "$(rd "$T/a.py" c3 60 ',"StartLine":50,"EndLine":120')" "$ALLOW" "checkpoint between reads -> re-read allowed"
hook "$(rd "$T/a.py" c3 14 ',"StartLine":50,"EndLine":120')" "$DENY"  "read before checkpoint step still deduped when verified"

# >40% context drop without CHECKPOINT record -> allowed
mktx c4 antigravity "$(pl 9 view_file ',"input_tokens":1000,"cache_read_tokens":90000')" "$(gen 10)" \
     "$(pl 31 run_command ',"input_tokens":20000,"cache_read_tokens":10000')" "$(gen 32)" "$(pl 59 view_file ',"input_tokens":500,"cache_read_tokens":31000')"
hook "$(rd "$T/a.py" c4 10 ',"StartLine":1,"EndLine":200')"  "$ALLOW" "drop: first read allowed"
hook "$(rd "$T/a.py" c4 60 ',"StartLine":50,"EndLine":120')" "$ALLOW" "context drop between reads -> allowed"

# stale transcript (latest planner did not issue this call) -> legacy window only
mktx c5 antigravity-cli "$(pl 9 view_file)" "$(gen 10)" "$(pl 11 run_command)"
hook "$(rd "$T/a.py" c5 10 ',"StartLine":1,"EndLine":200')"  "$ALLOW" "stale: first read allowed"
hook "$(rd "$T/a.py" c5 60 ',"StartLine":50,"EndLine":120')" "$ALLOW" "stale transcript -> no wide dedupe"
# escaped "CHECKPOINT" inside tool content must not count as compaction; transcriptPath honoured
mkdir -p "$T/tx"; printf '%s\n' "$(pl 9 view_file)" "$(gen 10 'x \"type\":\"CHECKPOINT\" y')" "$(pl 59 read_file)" > "$T/tx/t.jsonl"
hook "$(rd "$T/a.py" c6 10 ',"StartLine":1,"EndLine":200')"  "$ALLOW" "txpath: first read allowed"
hook "$(rd "$T/a.py" c6 60 ',"StartLine":50,"EndLine":120' ",\"transcriptPath\":\"$T/tx/t.jsonl\"")" "$DENY" "transcriptPath honoured; escaped CHECKPOINT ignored"

# --- 3. same size + same mtime but different content -> MD5 says changed -> allowed
seq 1 300 | sed 's/^/def func_line(): return 12345  # realistic python code/' > "$T/b.py"
hook "$(rd "$T/b.py" c7 10 ',"StartLine":1,"EndLine":200')"  "$ALLOW" "md5: first read allowed"
cp -p "$T/b.py" "$T/b.ref"
"$PY" -c 'import sys; p=sys.argv[1]; open(p,"w",encoding="utf-8").write(open(p,encoding="utf-8").read().replace("12345","54321"))' "$T/b.py"
touch -r "$T/b.ref" "$T/b.py"
hook "$(rd "$T/b.py" c7 12 ',"StartLine":50,"EndLine":120')" "$ALLOW" "same size+mtime edit detected by fingerprint"

# --- 4. overlap trim: covered prefix -> serve only the missing lines (no deny, no retry)
hook "$(rd "$T/a.py" c8 10 ',"StartLine":1,"EndLine":200')"   "$ALLOW" "trim: first read allowed"
hook "$(rd "$T/a.py" c8 12 ',"StartLine":150,"EndLine":300')" '"overwrite":\{"StartLine":201,"EndLine":300\}' "covered prefix trimmed to 201-300"
hook "$(rd "$T/a.py" c8 14 ',"StartLine":190,"EndLine":205')" "$DENY"  "trimmed tail recorded (190-205 now covered)"
hook "$(rd "$T/a.py" c8 15 ',"StartLine":195,"EndLine":215')" "$DENY"  "union of two reads covers request"
# small overlap (< 1.5KB) is left alone
hook "$(rd "$T/a.py" c9 10 ',"StartLine":1,"EndLine":100')"   "$ALLOW" "small-overlap: first read"
hook "$(rd "$T/a.py" c9 12 ',"StartLine":95,"EndLine":180')"  "$ALLOW" "tiny overlap not trimmed"
# gap in the middle (two uncovered blocks) -> untouched
hook "$(rd "$T/a.py" c10 10 ',"StartLine":50,"EndLine":100')" "$ALLOW" "hole: first read"
hook "$(rd "$T/a.py" c10 12 ',"StartLine":1,"EndLine":200')"  "$ALLOW" "covered middle only -> request untouched"

# --- 5. auto-cap then unbounded re-read -> next chunk instead of a dead-end deny
seq 1 2000 | sed 's/^/line of code number /' > "$T/big.py"
hook "$(rd "$T/big.py" c11 10)" '"StartLine":1,"EndLine":250' "big unbounded read capped to 1-250 (lossless default)"
hook "$(rd "$T/big.py" c11 12)" '"StartLine":251,"EndLine":500' "repeat unbounded read serves 251-500"
hook "$(rd "$T/big.py" c11 14 ',"StartLine":100,"EndLine":400')" "$DENY" "1-500 now covered"

# --- 6. full-file read then unbounded re-read -> deny; <=512B never deduped; 0.5KB-4KB deduped
hook "$(rd "$T/a.py" c12 10)" "$ALLOW" "full read allowed"
hook "$(rd "$T/a.py" c12 12)" "$DENY"  "unchanged full re-read denied"
o=$(printf '%s' "$(rd "$T/a.py" c12 12)" | "$PY" -S "$S/read_hook.py")
[[ "$o" == *"'a.py' is unchanged and still in context from step 10.\""* ]] && ok || bad "full re-read reason has no redundant range" "$o"
printf 'x = 1\n%.0s' $(seq 1 60) > "$T/tiny.py"
hook "$(rd "$T/tiny.py" c12 13)" "$ALLOW" "tiny file (<=512B) first read"
hook "$(rd "$T/tiny.py" c12 14)" "$ALLOW" "tiny (<=512B) re-read never denied"
printf 'x = 1\n%.0s' $(seq 1 100) > "$T/small.py"
hook "$(rd "$T/small.py" c12 13)" "$ALLOW" "small file (600B) first read"
hook "$(rd "$T/small.py" c12 14)" "$DENY"  "small (600B) unchanged re-read denied"
seq 1 60 | sed 's/^/def small_mod_fn(): return 12345  # 2.5KB file/' > "$T/mid2k.py"
hook "$(rd "$T/mid2k.py" c12 15)" "$ALLOW" "mid (2.5KB) first read allowed"
hook "$(rd "$T/mid2k.py" c12 16)" "$DENY"  "mid (1KB-4KB) unchanged re-read denied"

# --- 6b. spillback tasks/*.log + byte-aware wide spillback cap + blanket bounded data cap
mkdir -p "$T/.system_generated/tasks"
seq 1 300 | sed 's/^/2026-11-27 INFO long spillback output line payload_0123456789abcdefghijklmnopqrstuvwxyz_0123456789abcdef_/' > "$T/.system_generated/tasks/task-99.log"
hook "$(rd "$T/.system_generated/tasks/task-99.log" c14 10 ',"StartLine":1,"EndLine":220')" '"overwrite":\{"StartLine":1,"EndLine":' "wide tasks/*.log spillback byte-capped"
seq 1 500 | sed 's/^/{"id": 12345, "event": "metric_sample", "payload": "0123456789abcdefghijklmnopqrstuvwxyz"} /' > "$T/bigdata.jsonl"
hook "$(rd "$T/bigdata.jsonl" c14 12 ',"StartLine":1,"EndLine":500')" '"overwrite":\{"StartLine":1,"EndLine":120\}' "blanket 1-500 data read capped"

# --- 7. fail-open: corrupt history / no ids
mkdir -p "$PLUGIN_DATA/reads"; printf 'garbage\t\t\nmore\tjunk\n' > "$PLUGIN_DATA/reads/c13.tsv"
hook "$(rd "$T/a.py" c13 10 ',"StartLine":1,"EndLine":200')" "$ALLOW" "corrupt history -> allow"
hook "{\"toolInput\":{\"Path\":\"$T/a.py\",\"StartLine\":1,\"EndLine\":200}}" "$ALLOW" "no stepIdx/conversationId -> allow"
hook "{\"toolInput\":{\"Path\":\"$T/a.py\",\"StartLine\":1,\"EndLine\":200}}" "$ALLOW" "no ids twice -> still allow (no dedupe)"

printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
