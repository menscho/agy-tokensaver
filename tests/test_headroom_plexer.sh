#!/usr/bin/env bash
# Regression tests for kept token-saving guards and diagnostics:
#   - PostToolUse edit tracker (editseq bump, search/loop reset, read_hook invalidation)
#   - initialNumSteps compaction reset in read_hook.pl
#   - ts_inspect.py outline + BM25/FTS5 query
#   - Opt-in Compact Reads, image dedupe, tail-anchored logs, dirty-hunk narrowing
#   - Auto-generated header block, SPDX banner skip, cross-session microskills
#   - HUD profiles (auto_handoff default), sorted MCP tools/list, rollover state migration
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
S="$ROOT/scripts"
PY="$(command -v python3 || command -v python)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export PLUGIN_DATA="$T/pdata"; mkdir -p "$PLUGIN_DATA"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); }
bad() { FAIL=$((FAIL+1)); printf 'FAIL: %s\n  got: %s\n' "$1" "$2"; }
hook() { local o; o=$(printf '%s' "$2" | perl "$S/$1" 2>&1); [[ "$o" =~ $3 ]] && ok || bad "$4" "$o"; }
ALLOW='^\{"decision":"allow"\}$'

# 1. initialNumSteps compaction reset + post_edit_hook read invalidation
"$PY" -c 'print(("def f(): return 1234567890 # padding text\n") * 200, end="")' > "$T/comp.py"
hook read_hook.pl "$(printf '{"toolInput":{"Path":"%s","StartLine":1,"EndLine":100},"stepIdx":10,"initialNumSteps":10,"conversationId":"creset"}' "$T/comp.py")" "$ALLOW" "initial read recorded"
hook read_hook.pl "$(printf '{"toolInput":{"Path":"%s","StartLine":1,"EndLine":100},"stepIdx":12,"initialNumSteps":10,"conversationId":"creset"}' "$T/comp.py")" '"decision":"deny"' "duplicate read denied before compaction"
hook read_hook.pl "$(printf '{"toolInput":{"Path":"%s","StartLine":1,"EndLine":100},"stepIdx":14,"initialNumSteps":2,"conversationId":"creset"}' "$T/comp.py")" "$ALLOW" "initialNumSteps drop resets read dedup"
hook read_hook.pl "$(printf '{"toolInput":{"Path":"%s","StartLine":1,"EndLine":100},"stepIdx":15,"initialNumSteps":2,"conversationId":"creset"}' "$T/comp.py")" '"decision":"deny"' "re-recorded after reset"
printf '{"toolInput":{"Path":"%s"},"conversationId":"creset"}' "$T/comp.py" | perl "$S/post_edit_hook.pl" >/dev/null
hook read_hook.pl "$(printf '{"toolInput":{"Path":"%s","StartLine":1,"EndLine":100},"stepIdx":16,"initialNumSteps":2,"conversationId":"creset"}' "$T/comp.py")" "$ALLOW" "post_edit_hook invalidates read_hook entry"

# 10. @plexer/cli ts_inspect.py outline (Python AST + Rust/JS fallback)
cat > "$T/sample.py" <<'EOF'
class AuthService:
    def verify(self, tok):
        return tok == "ok"

async def refresh_all():
    return []
EOF
o=$("$PY" "$S/ts_inspect.py" outline "$T/sample.py")
[[ "$o" == *"L1-3: class AuthService"* && "$o" == *"L2-3: def verify"* && "$o" == *"L5-6: async def refresh_all"* ]] && ok || bad "ts_inspect outline Python AST" "$o"

cat > "$T/sample.rs" <<'EOF'
pub struct Cache { size: usize }
pub async fn evict_lru(c: &mut Cache) {}
EOF
o=$("$PY" "$S/ts_inspect.py" outline "$T/sample.rs")
[[ "$o" == *"L1: pub struct Cache"* && "$o" == *"L2: pub async fn evict_lru"* ]] && ok || bad "ts_inspect outline Rust regex" "$o"

# 11. Opt-in Compact Reads mode (default OFF vs ON)
"$PY" -c 'print(("def fn_item(x): return x * 42  # realistic source code line\n") * 260, end="")' > "$T/mid15k.py"
o=$(AGY_TS_COMPACT_READS=0 perl "$S/read_hook.pl" <<< "$(printf '{"toolInput":{"Path":"%s"}}' "$T/mid15k.py")")
[[ "$o" == '{"decision":"allow"}' ]] && ok || bad "compact_reads OFF leaves 15KB code uncapped" "$o"
o=$(AGY_TS_COMPACT_READS=1 perl "$S/read_hook.pl" <<< "$(printf '{"toolInput":{"Path":"%s"}}' "$T/mid15k.py")")
[[ "$o" == *'"EndLine":120'* ]] && ok || bad "compact_reads ON caps 15KB code to 120L" "$o"
o=$(AGY_TS_COMPACT_READS=1 perl "$S/read_hook.pl" <<< "$(printf '{"toolInput":{"Path":"%s","StartLine":50,"EndLine":220}}' "$T/mid15k.py")")
[[ "$o" == '{"decision":"allow"}' ]] && ok || bad "compact_reads ON leaves bounded slice untouched" "$o"

# 4. 2-Mode & 5-profile config (auto_handoff default -> small_loss -> zero_loss)
"$PY" - "$ROOT/mcp_app" "$T/cfgdata" <<'PYEOF' && ok || bad "config profile default auto_handoff vs small_loss vs zero_loss" "failed"
import os, sys
os.environ["PLUGIN_DATA"] = sys.argv[2]
sys.path.insert(0, sys.argv[1])
import core
c0 = core.load_config()
assert c0["profile"] == "auto_handoff" and c0["mode"] == "smart_auto" and not c0["compact_reads"] and not c0["concise_output"], c0
c1 = core.save_config({"profile": "small_loss"})
assert c1["profile"] == "small_loss" and c1["mode"] == "smart_auto" and c1["compact_reads"] and c1["concise_output"], c1
c2 = core.save_config({"profile": "zero_loss"})
assert c2["profile"] == "zero_loss" and c2["mode"] == "prepare_only" and not c2["compact_reads"] and not c2["concise_output"], c2
PYEOF

# 5. Honest output breakdown (0% unchanged) + cache_health forensics
"$PY" - "$ROOT/mcp_app" "$T/cfgdata2" <<'PYEOF' && ok || bad "honest output breakdown + cache_health" "failed"
import os, sys
os.environ["PLUGIN_DATA"] = sys.argv[2]
sys.path.insert(0, sys.argv[1])
import core
mk = lambda ts, ot, rb: {
    "turns": 10, "tot_in": 50000, "tot_cr": 200000, "tot_out": ot, "drops": 1,
    "drop_rebuild_tok": rb, "idle_drop_tok": rb, "midrun_drop_tok": 0,
    "actual_quota": 50000 + 200000*0.05 + ot*6, "uncached_baseline_quota": 250000 + ot*6,
    "sim_in": 50000, "sim_cr": 200000, "sim_quota": 50000 + 200000*0.05 + ot*6,
    "peak_ctx": 40000, "last_ts": ts,
}
sessions = [mk(i, 5000 if i < 4 else 15000, 30000) for i in range(8)]
sav = core.compute_savings_analytics(sessions, {}, [], {"concise_output": False})
assert sav["breakdown"]["output"]["saved_pct"] == 0.0 and sav["breakdown"]["output"]["saved"] == 0, sav["breakdown"]["output"]
assert sav["cache_health"]["rebuild_tokens"] == 240000 and sav["cache_health"]["idle_drop_tokens"] == 240000, sav["cache_health"]
PYEOF

# 19. Snapshot-only rollover state migrates to snapshotted_convs and does NOT block Mode 2 smart_auto
"$PY" - "$ROOT/mcp_app" "$T/cfgdata3" <<'PYEOF' && ok || bad "snapshot vs smart_auto rollover separation" "failed"
import json, os, sys
from pathlib import Path
os.environ["PLUGIN_DATA"] = sys.argv[2]
sys.path.insert(0, sys.argv[1])
import core
d = Path(sys.argv[2]); d.mkdir(parents=True, exist_ok=True)
(d / "rollover_history.json").write_text(json.dumps([{"from_conv": "abcd1234", "kind": "snapshot"}]))
(d / "rollover_state.json").write_text(json.dumps({"rolled_convs": {"abcd1234-rest": 1700000000}, "last_rollover_ts": 1700000000}))
rs = core._load_rollover_state()
assert "abcd1234-rest" not in rs["rolled_convs"] and "abcd1234-rest" in rs["snapshotted_convs"], rs
PYEOF

# 20. Deterministic alphabetically sorted MCP tool list (arXiv:2601.06007)
"$PY" - "$ROOT/mcp_app/server.py" "$T/cfgdata4" <<'PYEOF' && ok || bad "alphabetically sorted MCP tools/list" "failed"
import json, os, subprocess, sys
env = dict(os.environ, PLUGIN_DATA=sys.argv[2])
p = subprocess.Popen([sys.executable, sys.argv[1]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
p.stdin.write(json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}) + "\n"); p.stdin.flush()
r = json.loads(p.stdout.readline())
p.terminate(); p.wait(timeout=5)
names = [t["name"] for t in r["result"]["tools"]]
assert names == sorted(names) and len(names) >= 5, names
PYEOF

# 6. Duplicate image read guard (0% loss)
printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00\x00\x00\x01\x00\x08\x02\x00\x00\x00' > "$T/shot.png"
o1=$(perl "$S/read_hook.pl" <<< "$(printf '{"toolInput":{"Path":"%s"},"conversationId":"imgc1","stepIdx":1}' "$T/shot.png")")
o2=$(perl "$S/read_hook.pl" <<< "$(printf '{"toolInput":{"Path":"%s"},"conversationId":"imgc1","stepIdx":3}' "$T/shot.png")")
[[ "$o1" == '{"decision":"allow"}' && "$o2" == *'"decision":"deny"'* ]] && ok || bad "duplicate image read guard" "o1=$o1 o2=$o2"

# 7. All HUD benchmark modes selectable cleanly (auto_handoff default)
"$PY" - "$ROOT/mcp_app" "$T/cfgdata5" <<'PYEOF' && ok || bad "all HUD benchmark profiles" "failed"
import os, sys
os.environ["PLUGIN_DATA"] = sys.argv[2]
sys.path.insert(0, sys.argv[1])
import core
c = core.load_config()
assert c["profile"] == "auto_handoff" and c["mode"] == "smart_auto" and not c["compact_reads"] and not c["concise_output"], c
for prof, exp in [
    ("auto_handoff", ("smart_auto", False, False)),
    ("compact_reads", ("prepare_only", True, False)),
    ("concise_no_handoff", ("prepare_only", True, True)),
    ("handoff_only", ("smart_auto", True, False)),
    ("small_loss", ("smart_auto", True, True)),
    ("zero_loss", ("prepare_only", False, False)),
]:
    r = core.save_config({"profile": prof})
    assert (r["mode"], r["compact_reads"], r["concise_output"]) == exp, (prof, r)
PYEOF

# 8. Tail-anchored unbounded >46KB .log read (small <46KB logs stay whole)
"$PY" -c 'for i in range(1, 861): print(f"2026-11-24 INFO line {i:03d} payload_event_0123456789abcde")' > "$T/app.log"
o=$(perl "$S/read_hook.pl" <<< "$(printf '{"toolInput":{"Path":"%s"}}' "$T/app.log")")
[[ "$o" == *'"StartLine":712'* && "$o" == *'"EndLine":861'* ]] && ok || bad "tail-anchored unbounded >46KB .log cap" "$o"
"$PY" -c 'for i in range(1, 201): print(f"2026-11-24 INFO line {i:03d} small_log_payload_0123456789abcdef")' > "$T/small.log"
o=$(perl "$S/read_hook.pl" <<< "$(printf '{"toolInput":{"Path":"%s"}}' "$T/small.log")")
[[ "$o" == '{"decision":"allow"}' ]] && ok || bad "small <46KB .log stays uncapped" "$o"

# 9. Post-edit dirty-hunk auto-narrowing
"$PY" -c 'for i in range(1, 321): print(f"def fn_{i}(x): return x + {i}  # padding comment line")' > "$T/dirty.py"
o1=$(AGY_TS_COMPACT_READS=0 perl "$S/read_hook.pl" <<< "$(printf '{"toolInput":{"Path":"%s"},"conversationId":"dh1","stepIdx":1}' "$T/dirty.py")")
"$PY" -c 'import sys; p=open(sys.argv[1]).read().splitlines(); p[219]="def fn_220(x): return x * 999  # edited"; open(sys.argv[1],"w").write("\n".join(p)+"\n")' "$T/dirty.py"
o2=$(perl "$S/read_hook.pl" <<< "$(printf '{"toolInput":{"Path":"%s"},"conversationId":"dh1","stepIdx":3}' "$T/dirty.py")")
[[ "$o1" == *'"decision":"allow"'* && "$o2" == *'"StartLine":205,"EndLine":235'* ]] && ok || bad "post-edit dirty-hunk narrowing" "o1=$o1 o2=$o2"

# 10. ts_inspect.py query BM25/FTS5 chunk retrieval
"$PY" - <<'PYEOF' > "$T/ids.log"
for i in range(1, 500):
    if i == 250:
        print("INFO request_id=123e4567-e89b-12d3-a456-426614174000 code=ERR_QUOTA_BURST ok=true")
    else:
        print(f"INFO tick {i} normal_log_payload_0123456789abcdef0123456789abcdef")
PYEOF
o=$("$PY" "$S/ts_inspect.py" query "$T/ids.log" ERR_QUOTA_BURST)
[[ "$o" == *"L241-270"* || "$o" == *"ERR_QUOTA_BURST"* ]] && ok || bad "ts_inspect query BM25/FTS5" "$(head -c 250 <<<"$o")"

# 11. Universal doom_loop guard in grep_hook.pl + find_hook.pl + post_edit_hook editseq reset
gj() { printf '{"toolInput":{"SearchPath":"%s","Query":"needle_xyz"},"conversationId":"gl1","stepIdx":%d}' "$T" "$1"; }
o1=$(perl "$S/grep_hook.pl" <<< "$(gj 1)")
o2=$(perl "$S/grep_hook.pl" <<< "$(gj 2)")
o3=$(perl "$S/grep_hook.pl" <<< "$(gj 3)")
o4=$(perl "$S/grep_hook.pl" <<< "$(gj 4)")
[[ "$o1" == '{"decision":"allow"}' && "$o2" == '{"decision":"allow"}' && "$o3" == *'"decision":"deny"'* && "$o4" == '{"decision":"allow"}' ]] && ok || bad "grep_hook doom_loop 3x + retry escape" "o3=$o3 o4=$o4"
printf '{"toolInput":{"Path":"%s/dirty.py"},"conversationId":"gl1"}' "$T" | perl "$S/post_edit_hook.pl" >/dev/null
o5=$(perl "$S/grep_hook.pl" <<< "$(gj 5)")
[[ "$o5" == '{"decision":"allow"}' ]] && ok || bad "post_edit_hook resets grep_hook loop guard" "$o5"

# 30. audit.py --discover runs cleanly
o=$("$PY" "$S/audit.py" "$ROOT" --discover 2>&1)
[[ $? -eq 0 && "$o" == *"Transcript Discovery"* ]] && ok || bad "audit.py --discover" "$(tail -n 5 <<<"$o")"

# 31. Auto-generated header file blocked when unbounded >24KB
{ echo "// Code generated by protoc-gen-go. DO NOT EDIT."; seq 1 900 | sed 's/^/func generatedFieldAccessor(x int) int { return x + 1 } \/\/ pad /'; } > "$T/types.go"
o=$(perl "$S/read_hook.pl" <<< "$(printf '{"toolInput":{"Path":"%s"}}' "$T/types.go")")
[[ "$o" == *'"decision":"deny"'* && "$o" == *"auto-generated"* ]] && ok || bad "auto-generated header gate" "$o"

# 33. SPDX/License banner auto-skip advances StartLine past leading comments (0% loss)
"$PY" - <<'PYEOF' > "$T/licensed.rs"
print("// SPDX-License-Identifier: Apache-2.0")
for i in range(18):
    print(f"// Copyright (c) 2026 Example Corp. Licensed under Apache License line {i}.")
for i in range(1, 350):
    print(f"pub fn handler_{i}(x: usize) -> usize {{ x + {i} }} // realistic rust code line")
PYEOF
o=$(perl "$S/read_hook.pl" <<< "$(printf '{"toolInput":{"Path":"%s"}}' "$T/licensed.rs")")
[[ "$o" == *'"StartLine":20,"EndLine":269'* ]] && ok || bad "SPDX license banner StartLine skip" "$o"

# 34. CHANGELOG.md compact cap to 80L in Mode 2 (opt-in)
"$PY" -c 'for i in range(1, 300): print(f"- v0.{i}.0: release changelog entry with details and notes 1234567890")' > "$T/CHANGELOG.md"
o=$(AGY_TS_COMPACT_READS=1 perl "$S/read_hook.pl" <<< "$(printf '{"toolInput":{"Path":"%s"}}' "$T/CHANGELOG.md")")
[[ "$o" == *'"StartLine":1,"EndLine":80'* ]] && ok || bad "CHANGELOG.md compact cap to 80L" "$o"

# 35. TokenEconomizer cross-session microskills promotion (>=2 confirmations)
"$PY" - "$ROOT/scripts" "$T/msdata" <<'PYEOF' && ok || bad "TokenEconomizer cross-session microskills" "failed"
import os, sys
from pathlib import Path
os.environ["PLUGIN_DATA"] = sys.argv[2]
sys.path.insert(0, sys.argv[1])
import handoff_pack as hp
ws = Path(sys.argv[2])
assert hp._update_microskills(ws, "pytest -q") == []
assert hp._update_microskills(ws, "pytest -q") == ["pytest -q"]
PYEOF

printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
