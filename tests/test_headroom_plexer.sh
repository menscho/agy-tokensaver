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

printf 'PASS=%d FAIL=%d\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
