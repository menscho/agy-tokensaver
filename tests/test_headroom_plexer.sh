#!/usr/bin/env bash
# Regression tests for Headroom- and @plexer/cli-inspired token-saving features:
#   - JSON SmartCrusher (lossless CSV+schema, nested flatten, blob offload, error-preserving sampling)
#   - Dense machine-generated line elider
#   - Repeated multi-line block folding
#   - Large diff +/-2 context trimming
#   - Windows-safe grep path hoisting + SSH remote grep path hoisting
#   - PostToolUse edit tracker (editseq bump, search cache clear, read_hook invalidation)
#   - Duplicate search & unchanged git_diff guard with 1-retry escape hatch
#   - WaitMsBeforeAsync floor bump on fast queries
#   - initialNumSteps compaction reset in read_hook.pl
#   - ts_inspect.py outline (Python AST + JS/Rust regex)
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
S="$ROOT/scripts"
PY="$(command -v python3 || command -v python)"
W="perl $S/cmd_wrap.pl"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export PLUGIN_DATA="$T/pdata"; mkdir -p "$PLUGIN_DATA"
export AGY_TS_SHELL=posix
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); }
bad() { FAIL=$((FAIL+1)); printf 'FAIL: %s\n  got: %s\n' "$1" "$2"; }
hook() { local o; o=$(printf '%s' "$2" | perl "$S/$1" 2>&1); [[ "$o" =~ $3 ]] && ok || bad "$4" "$o"; }
ALLOW='^\{"decision":"allow"\}$'

# 1. Headroom JSON SmartCrusher: lossless CSV+schema + nested flatten (<=9.9KB)
"$PY" - <<'PYEOF' > "$T/j_small.json"
import json
data = {"env": "prod", "items": [
    {"id": i, "meta": {"region": "eu", "tier": "web"}, "status": 200, "ok": True}
    for i in range(1, 25)
]}
print(json.dumps(data, indent=2))
PYEOF
o=$($W --cat http --test-stdin < "$T/j_small.json")
[[ "$o" == *"[24]{"* && "$o" == *"meta.region"* && "$o" == *"# env: prod"* && "$o" != *"SmartCrusher:"* ]] && ok || bad "JSON lossless CSV+schema + flatten" "$(head -c 260 <<<"$o")"
(( ${#o} < $(wc -c < "$T/j_small.json") / 2 )) && ok || bad "JSON CSV+schema saves >50% bytes" "${#o} vs $(wc -c < "$T/j_small.json")"

# 2. Headroom JSON SmartCrusher: >9.9KB error-preserving sampling + outlier + blob offload
"$PY" - <<'PYEOF' > "$T/j_big.json"
import json
rows = []
for i in range(1, 520):
    r = {"id": f"req_{i:04d}", "service": "gateway-prod-eu-west-1", "status": 200, "msg": "request handled cleanly"}
    if i == 242:
        r["status"] = 500
        r["msg"] = "upstream fatal timeout error E_GW_500"
    if i == 388:
        r["rare_debug_flag"] = "outlier_388"
    if i == 5:
        r["payload"] = "<div>raw html fragment</div> " * 14
    rows.append(r)
print(json.dumps(rows, indent=2))
PYEOF
o=$($W --cat cloud --test-stdin < "$T/j_big.json")
[[ "$o" == *"[519]{"* && "$o" == *"E_GW_500"* && "$o" == *"outlier_388"* && "$o" == *"[blob:"* && "$o" == *"SmartCrusher:"* && "$o" == *"full: "* ]] && ok || bad "JSON SmartCrusher preserves errors, outliers, blobs + spill" "$(head -c 300 <<<"$o")"

# 3. Headroom dense machine-generated line elider (minified wall elided; normal code untouched)
"$PY" - <<'PYEOF' > "$T/dense.log"
print("INFO booting server")
for i in range(6):
    print(f"var __bundle_{i}=" + "".join(f"x{k}=1," for k in range(80)) + "end;")
print("ERROR boot_done")
PYEOF
o=$($W --cat logs --test-stdin < "$T/dense.log")
[[ "$o" == *"[elided "* && "$o" == *"ERROR boot_done"* && "$o" == *"full: "* ]] && ok || bad "dense line elider shrinks minified wall + spills" "$(head -c 260 <<<"$o")"

# 4. Headroom repeated multi-line block folding (lossless)
{ for i in $(seq 1 25); do printf '=== Health Check Worker Pool ===\nstatus: ok ready=12/12\nlatency_p99: 2.1ms\n'; done; } > "$T/blocks.log"
o=$($W --cat logs --test-stdin < "$T/blocks.log")
[[ "$o" == *"[block above (3L) repeated x25]"* ]] && ok || bad "repeated multi-line block folded" "$o"

# 5. Headroom large git_diff context trimming (+/-2 around +/- changes)
"$PY" - <<'PYEOF' > "$T/bigdiff.txt"
print("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1,220 +1,220 @@")
for i in range(1, 220):
    if i == 10:
        print("-old_alpha = 1\n+new_alpha = 2")
    elif i == 200:
        print("-old_omega = 3\n+new_omega = 4")
    else:
        print(f" # unchanged context line number {i:03d} with enough padding to cross native cap")
PYEOF
o=$($W --cat git_diff --test-stdin < "$T/bigdiff.txt")
[[ "$o" == *"+new_alpha = 2"* && "$o" == *"+new_omega = 4"* && "$o" == *"context lines trimmed"* ]] && ok || bad "large git_diff trims context to +/-2" "$(head -c 300 <<<"$o")"

# 6. Headroom Windows-safe grep path hoisting + SSH remote grep path hoisting
{ for i in $(seq 1 12); do printf 'C:\\Users\\dev\\repo\\src\\auth.py:%d:    verify_token(req)\n' "$i"; done; } > "$T/wingrep.txt"
o=$($W --cat search --test-stdin < "$T/wingrep.txt")
[[ "$o" == *'C:\Users\dev\repo\src\auth.py:'* && "$o" == *"  12:    verify_token(req)"* ]] && ok || bad "Windows C:\\ grep path hoisted" "$o"

{ for i in $(seq 1 15); do printf '/srv/webapps/prod/api/routes/handler.py:%d:def route_%d(): pass\n' "$i" "$i"; done; } > "$T/sshgrep.txt"
o=$($W --cat ssh --test-stdin < "$T/sshgrep.txt")
[[ "$o" == *"/srv/webapps/prod/api/routes/handler.py:"* && "$o" == *"  15:def route_15(): pass"* ]] && ok || bad "SSH remote grep paths hoisted" "$o"

# 7. @plexer/cli duplicate search / unchanged git_diff guard + 1-retry escape hatch + post_edit_hook reset
cj() { printf '{"toolInput":{"CommandLine":"%s","Cwd":"%s"},"stepIdx":%d,"conversationId":"plex1"}' "$1" "$T" "$2"; }
hook cmd_hook.pl "$(cj 'git diff' 1)" "--cat git_diff"  "first git diff allowed & wrapped"
hook cmd_hook.pl "$(cj 'git diff' 3)" '"decision":"deny"' "identical git diff blocked before edit"
hook cmd_hook.pl "$(cj 'git diff' 4)" "--cat git_diff"  "1-retry escape hatch allows insistent repeat"
hook cmd_hook.pl "$(cj 'git diff' 6)" '"decision":"deny"' "subsequent repeat blocked again"
# Edit via post_edit_hook.pl clears search cache and bumps editseq
printf '{"toolInput":{"Path":"%s/a.py"},"conversationId":"plex1"}' "$T" | perl "$S/post_edit_hook.pl" >/dev/null
hook cmd_hook.pl "$(cj 'git diff' 7)" "--cat git_diff"  "post_edit_hook resets git diff dedup"

# 8. @plexer/cli WaitMsBeforeAsync floor bump on fast queries (not on slow pytest)
hook cmd_hook.pl "$(printf '{"toolInput":{"CommandLine":"git diff","Cwd":"%s","WaitMsBeforeAsync":500}}' "$T")" '"WaitMsBeforeAsync":5000' "WaitMsBeforeAsync bumped 500->5000 on fast git diff"
o=$(printf '{"toolInput":{"CommandLine":"pytest tests/","Cwd":"%s","WaitMsBeforeAsync":500}}' "$T" | perl "$S/cmd_hook.pl")
[[ "$o" != *"WaitMsBeforeAsync"* ]] && ok || bad "WaitMsBeforeAsync untouched on pytest" "$o"

# 9. @plexer/cli initialNumSteps compaction reset + post_edit_hook read invalidation
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

# 12. POSIX ffmpeg & read-only python3 -c inspection wrapping
hook cmd_hook.pl "$(printf '{"toolInput":{"CommandLine":"ffmpeg -y -i in.mp4 out.mp4","Cwd":"%s"}}' "$T")" "--cat build" "ffmpeg wraps on POSIX"
hook cmd_hook.pl "$(printf '{"toolInput":{"CommandLine":"python3 -c '\''import json; data = [dict(i=x, v=x*2) for x in range(1000)]; print(json.dumps(data))'\''","Cwd":"%s"}}' "$T")" "--cat logs" "long read-only python3 -c wraps"
hook cmd_hook.pl "$(printf '{"toolInput":{"CommandLine":"python3 -c \"open(\\\"x.txt\\\", \\\"w\\\").write(\\\"a\\\"*500)\"","Cwd":"%s"}}' "$T")" "$ALLOW" "writing python3 -c skipped"

# 13. Merlin non-adjacent chunk deduplication (arXiv:2605.09990)
"$PY" - <<'PYEOF' > "$T/merlin.log"
blk = "HEADER: service worker diagnostics report\n  pool_size=32 active=12 queued=0 errors=0\n  memory_rss=412MB cpu_pct=14.2 uptime=86400s\n  region=eu-west-1 cluster=prod-primary-01\n"
print(blk)
print("INFO middle unique event alpha=1\n")
print(blk)
print("INFO middle unique event beta=2\n")
print(blk)
for i in range(40):
    print(f"INFO tick {i} unique_payload_{i}")
PYEOF
o=$($W --cat logs --test-stdin < "$T/merlin.log")
[[ "$o" == *"[exact dup of lines 1-4 (4L)]"* ]] && ok || bad "Merlin non-adjacent chunk dedupe" "$(head -c 350 <<<"$o")"

# 14. TokenEconomizer 3x failure-loop breaker (arXiv:2610.22732)
tj() { printf '{"toolInput":{"CommandLine":"pytest tests/test_auth.py","Cwd":"%s"},"stepIdx":%d,"conversationId":"loop1"}' "$T" "$1"; }
hook cmd_hook.pl "$(tj 1)" "--cat test" "loop breaker: 1st pytest allowed"
hook cmd_hook.pl "$(tj 2)" "--cat test" "loop breaker: 2nd pytest allowed"
hook cmd_hook.pl "$(tj 3)" '"decision":"deny"' "loop breaker: 3rd identical pytest without edits blocked"
hook cmd_hook.pl "$(tj 4)" "--cat test" "loop breaker: 1-retry escape hatch allows 4th"

# 15. Multiline diagnostic banner chain wraps cleanly
ml_cmd='echo "=== STATUS ===" && ps aux && echo "=== LOGS ===" && journalctl -n 50'
hook cmd_hook.pl "$("$PY" -c 'import json,sys; print(json.dumps({"toolInput":{"CommandLine":sys.argv[1],"Cwd":sys.argv[2]}}))' "$ml_cmd" "$T")" "--cat chain" "multiline diagnostic banner chain wraps"

# 16. 2-Mode config profile (max default small_loss -> zero_loss -> small_loss)
"$PY" - "$ROOT/mcp_app" "$T/cfgdata" <<'PYEOF' && ok || bad "2-mode profile default small_loss vs zero_loss" "failed"
import os, sys
os.environ["PLUGIN_DATA"] = sys.argv[2]
sys.path.insert(0, sys.argv[1])
import core
c0 = core.load_config()
assert c0["profile"] == "small_loss" and c0["mode"] == "smart_auto" and c0["compact_reads"] and c0["concise_output"], c0
c1 = core.save_config({"profile": "zero_loss"})
assert c1["profile"] == "zero_loss" and c1["mode"] == "prepare_only" and not c1["compact_reads"] and not c1["concise_output"], c1
c2 = core.save_config({"profile": "small_loss"})
assert c2["profile"] == "small_loss" and c2["mode"] == "smart_auto" and c2["compact_reads"] and c2["concise_output"], c2
PYEOF

# 17. Deterministic content-addressed spillback filename (arXiv:2601.06007 cache stability)
"$PY" -c 'for i in range(400): print(f"INFO line {i} payload_data_block_0123456789abcdef")' > "$T/spill_in.log"
s1=$($W --cat logs --test-stdin < "$T/spill_in.log" | grep -oE '[0-9a-f]{12}\.log' | head -n 1)
s2=$($W --cat logs --test-stdin < "$T/spill_in.log" | grep -oE '[0-9a-f]{12}\.log' | head -n 1)
[[ -n "$s1" && "$s1" == "$s2" ]] && ok || bad "deterministic content-addressed spillback path" "s1=$s1 s2=$s2"

# 18. Honest output breakdown (0% unchanged, no bogus +/-% from task size) + cache_health forensics
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

# 21. Duplicate image read guard (0% loss)
printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00\x00\x00\x01\x00\x08\x02\x00\x00\x00' > "$T/shot.png"
o1=$(perl "$S/read_hook.pl" <<< "$(printf '{"toolInput":{"Path":"%s"},"conversationId":"imgc1","stepIdx":1}' "$T/shot.png")")
o2=$(perl "$S/read_hook.pl" <<< "$(printf '{"toolInput":{"Path":"%s"},"conversationId":"imgc1","stepIdx":3}' "$T/shot.png")")
[[ "$o1" == '{"decision":"allow"}' && "$o2" == *'"decision":"deny"'* ]] && ok || bad "duplicate image read guard" "o1=$o1 o2=$o2"

# 22. New zero-loss command wrappers: ps -eo, ls -la && find chain, multi-cmd ssh dump
hook cmd_hook.pl "$(printf '{"toolInput":{"CommandLine":"ps -eo pid,pcpu,args | grep python","Cwd":"%s"}}' "$T")" "--cat ps" "ps -eo wraps"
hook cmd_hook.pl "$(printf '{"toolInput":{"CommandLine":"ls -la && find . -maxdepth 3","Cwd":"%s"}}' "$T")" "--cat find" "ls -la && find chain wraps"
hook cmd_hook.pl "$(printf '{"toolInput":{"CommandLine":"export FOO=1 && mkdir -p dist && pytest","Cwd":"%s"}}' "$T")" "--cat test" "export + mkdir + pytest chain wraps"
hook cmd_hook.pl "$(printf '{"toolInput":{"CommandLine":"ssh vps '\''uname -a; cat /etc/os-release; ls -la /var/log; uptime; free -m; df -h; cat /proc/meminfo'\''","Cwd":"%s"}}' "$T")" "--cat ssh" "multi-command ssh diagnostic dump wraps"

# 23. RTK compiler error grouping in compress_build
"$PY" - <<'PYEOF' > "$T/tsc.err"
for i in range(1, 80):
    print(f"src/app.ts:{i}:12 - error TS2322: Type 'string' is not assignable to type 'number'.")
print("Found 79 errors in 1 file.")
PYEOF
o=$($W --cat build --test-stdin --test-rc 1 < "$T/tsc.err")
[[ "$o" == *"Repeated compiler errors collapsed"* && "$o" == *"(+75 more)"* ]] && ok || bad "RTK compiler error grouping in compress_build" "$(head -c 300 <<<"$o")"

# 24. All 5 benchmark HUD modes selectable cleanly (strongest small_loss default)
"$PY" - "$ROOT/mcp_app" "$T/cfgdata5" <<'PYEOF' && ok || bad "all 5 HUD benchmark profiles" "failed"
import os, sys
os.environ["PLUGIN_DATA"] = sys.argv[2]
sys.path.insert(0, sys.argv[1])
import core
c = core.load_config()
assert c["profile"] == "small_loss" and c["mode"] == "smart_auto" and c["compact_reads"] and c["concise_output"], c
for prof, exp in [
    ("handoff_only", ("smart_auto", True, False)),
    ("concise_no_handoff", ("prepare_only", True, True)),
    ("compact_reads", ("prepare_only", True, False)),
    ("zero_loss", ("prepare_only", False, False)),
    ("small_loss", ("smart_auto", True, True)),
]:
    r = core.save_config({"profile": prof})
    assert (r["mode"], r["compact_reads"], r["concise_output"]) == exp, (prof, r)
PYEOF

# 25. Tail-anchored unbounded .log read (OpenAI Codex + LogDx-CI)
"$PY" -c 'for i in range(1, 401): print(f"2026-11-24 INFO line {i} payload_event_0123456789abcdef")' > "$T/app.log"
o=$(perl "$S/read_hook.pl" <<< "$(printf '{"toolInput":{"Path":"%s"}}' "$T/app.log")")
[[ "$o" == *'"StartLine":32'* && "$o" == *'"EndLine":40'* ]] && ok || bad "tail-anchored unbounded .log cap" "$o"

# 26. Post-edit dirty-hunk auto-narrowing (squeez/Cline)
"$PY" -c 'for i in range(1, 321): print(f"def fn_{i}(x): return x + {i}  # padding comment line")' > "$T/dirty.py"
o1=$(AGY_TS_COMPACT_READS=0 perl "$S/read_hook.pl" <<< "$(printf '{"toolInput":{"Path":"%s"},"conversationId":"dh1","stepIdx":1}' "$T/dirty.py")")
"$PY" -c 'p=open("'"$T/dirty.py"'").read().splitlines(); p[219]="def fn_220(x): return x * 999  # edited"; open("'"$T/dirty.py"'","w").write("\n".join(p)+"\n")'
o2=$(perl "$S/read_hook.pl" <<< "$(printf '{"toolInput":{"Path":"%s"},"conversationId":"dh1","stepIdx":3}' "$T/dirty.py")")
[[ "$o1" == *'"decision":"allow"'* && "$o2" == *'"StartLine":205,"EndLine":235'* ]] && ok || bad "post-edit dirty-hunk narrowing" "o1=$o1 o2=$o2"

# 27. ids_preserved footer surfaces omitted ERR_* / UUIDs (squeez)
"$PY" - <<'PYEOF' > "$T/ids.log"
for i in range(1, 500):
    if i == 250:
        print("INFO request_id=123e4567-e89b-12d3-a456-426614174000 code=ERR_QUOTA_BURST ok=true")
    else:
        print(f"INFO tick {i} normal_log_payload_0123456789abcdef0123456789abcdef")
PYEOF
o=$($W --cat logs --test-stdin < "$T/ids.log")
[[ "$o" == *"ids:"* && "$o" == *"ERR_QUOTA_BURST"* ]] && ok || bad "ids_preserved footer in cmd_wrap" "$(tail -n 3 <<<"$o")"

# 28. ts_inspect.py query BM25/FTS5 chunk retrieval (context-mode)
o=$("$PY" "$S/ts_inspect.py" query "$T/ids.log" ERR_QUOTA_BURST)
[[ "$o" == *"L241-270"* || "$o" == *"ERR_QUOTA_BURST"* ]] && ok || bad "ts_inspect query BM25/FTS5" "$(head -c 250 <<<"$o")"

# 29. Universal doom_loop guard in grep_hook.pl (OpenCode + IAL-Scan)
gj() { printf '{"toolInput":{"SearchPath":"%s","Query":"needle_xyz"},"conversationId":"gl1","stepIdx":%d}' "$T" "$1"; }
o1=$(perl "$S/grep_hook.pl" <<< "$(gj 1)")
o2=$(perl "$S/grep_hook.pl" <<< "$(gj 2)")
o3=$(perl "$S/grep_hook.pl" <<< "$(gj 3)")
o4=$(perl "$S/grep_hook.pl" <<< "$(gj 4)")
[[ "$o1" == '{"decision":"allow"}' && "$o2" == '{"decision":"allow"}' && "$o3" == *'"decision":"deny"'* && "$o4" == '{"decision":"allow"}' ]] && ok || bad "grep_hook doom_loop 3x + retry escape" "o3=$o3 o4=$o4"

# 30. audit.py --discover runs cleanly
o=$("$PY" "$S/audit.py" "$ROOT" --discover 2>&1)
[[ $? -eq 0 && "$o" == *"Transcript Discovery"* ]] && ok || bad "audit.py --discover" "$(tail -n 5 <<<"$o")"

# 31. read_board poll-guard (board_hook.pl: 3x identical since cursor blocked, 4th retry allowed)
bj() { printf '{"toolInput":{"since":48},"conversationId":"bd1","stepIdx":%d}' "$1"; }
o1=$(perl "$S/board_hook.pl" <<< "$(bj 1)")
o2=$(perl "$S/board_hook.pl" <<< "$(bj 2)")
o3=$(perl "$S/board_hook.pl" <<< "$(bj 3)")
o4=$(perl "$S/board_hook.pl" <<< "$(bj 4)")
[[ "$o1" == '{"decision":"allow"}' && "$o2" == '{"decision":"allow"}' && "$o3" == *'"decision":"deny"'* && "$o4" == '{"decision":"allow"}' ]] && ok || bad "board_hook 3x poll-guard + retry escape" "o3=$o3 o4=$o4"

# 32. Auto-generated header file blocked when unbounded >24KB
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
[[ "$o" == *'"StartLine":20,"EndLine":139'* ]] && ok || bad "SPDX license banner StartLine skip" "$o"

# 34. CHANGELOG.md compact cap to 80L in Mode 2
"$PY" -c 'for i in range(1, 300): print(f"- v0.{i}.0: release changelog entry with details and notes 1234567890")' > "$T/CHANGELOG.md"
o=$(perl "$S/read_hook.pl" <<< "$(printf '{"toolInput":{"Path":"%s"}}' "$T/CHANGELOG.md")")
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
