#!/usr/bin/env bash
# token-saver regression suite: hook decisions, wrapper compression, exit codes, safety skips.
# Usage: bash tests/run_tests.sh   (exit 0 = all pass)
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
S="$ROOT/scripts"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export PLUGIN_DATA="$T/pdata"; mkdir -p "$PLUGIN_DATA"   # never pollute real telemetry
PASS=0; FAIL=0

ok()   { PASS=$((PASS+1)); }
bad()  { FAIL=$((FAIL+1)); printf 'FAIL: %s\n  got: %s\n' "$1" "$2"; }

# hook <script> <json> <regex> <name>   (regex must match output)
hook()  { local o; o=$(printf '%s' "$2" | perl "$S/$1" 2>&1); [[ "$o" =~ $3 ]] && ok || bad "$4" "$o"; }
# nhook: regex must NOT match
nhook() { local o; o=$(printf '%s' "$2" | perl "$S/$1" 2>&1); [[ "$o" =~ $3 ]] && bad "$4" "$o" || ok; }
cmdj()  { printf '{"toolName":"run_command","toolInput":{"CommandLine":"%s","Cwd":"%s"},"cwd":"%s"}' "$1" "${2:-$T}" "${2:-$T}"; }
ALLOW='^\{"decision":"allow"\}$'
WRAP='cmd_wrap\.pl.* --cat '

# ---------- cmd_hook: wraps ----------
hook cmd_hook.pl "$(cmdj 'pytest -x')"                 "--cat test -- 'pytest -x'"   "pytest wraps as test"
hook cmd_hook.pl "$(cmdj 'npm run build')"             "--cat build"                 "npm run build wraps"
hook cmd_hook.pl "$(cmdj 'git diff')"                  "--cat git_diff"              "git diff wraps"
hook cmd_hook.pl "$(cmdj 'cargo test && cargo build')" "--cat chain"                 "test+build chain"
hook cmd_hook.pl "$(cmdj "ssh host 'journalctl -u app -n 200'")" "--cat ssh"         "ssh remote wraps"
hook cmd_hook.pl "$(cmdj 'uv run pytest tests/')"      "--cat test"                  "uv run unwrap"
hook cmd_hook.pl "$(cmdj 'kubectl logs pod-1')"        "--cat logs"                  "kubectl logs wraps"
# ---------- cmd_hook: safety skips ----------
hook cmd_hook.pl "$(cmdj 'pytest | tee out')"          "$ALLOW" "pipe skipped"
hook cmd_hook.pl "$(cmdj 'pytest > out.txt')"          "$ALLOW" "redirect skipped"
hook cmd_hook.pl "$(cmdj 'pytest; echo hi')"           "$ALLOW" "semicolon with unknown segment skipped"
hook cmd_hook.pl "$(cmdj 'pytest &')"                  "$ALLOW" "background skipped"
hook cmd_hook.pl "$(cmdj 'echo $(git diff)')"          "$ALLOW" "cmd subst skipped"
hook cmd_hook.pl "$(cmdj 'pytest\\necho x')"           "$ALLOW" "multiline skipped"
hook cmd_hook.pl "$(cmdj 'kubectl logs -f pod')"       "$ALLOW" "follow skipped"
hook cmd_hook.pl "$(cmdj 'docker logs --follow c')"    "$ALLOW" "docker follow skipped"
hook cmd_hook.pl "$(cmdj 'journalctl -fu app')"        "$ALLOW" "journalctl -f skipped"
hook cmd_hook.pl "$(cmdj "ssh host 'tail -f /var/log/x'")" "$ALLOW" "ssh tail -f skipped"
hook cmd_hook.pl "$(cmdj 'ssh -t host top')"           "$ALLOW" "ssh tty skipped"
hook cmd_hook.pl "$(cmdj 'ssh host')"                  "$ALLOW" "ssh interactive skipped"
hook cmd_hook.pl "$(cmdj 'git log -p')"                "$ALLOW" "git log -p skipped"
hook cmd_hook.pl "$(cmdj 'git status')"                "$ALLOW" "git status skipped"
hook cmd_hook.pl "$(cmdj 'npm test -- --json')"        "$ALLOW" "--json skipped"
hook cmd_hook.pl "$(cmdj 'pytest --help')"             "$ALLOW" "--help skipped"
hook cmd_hook.pl "$(cmdj 'pytest && echo DONE')"       "$ALLOW" "unknown chain seg skipped"
hook cmd_hook.pl "$(cmdj 'find . -name x -delete')"    "$ALLOW" "find -delete skipped"
hook cmd_hook.pl "$(cmdj 'echo hello')"                "$ALLOW" "no keyword fast path"
hook cmd_hook.pl '{"toolInput":{"CommandLine":"pytest","IsDaemon":true}}' "$ALLOW" "daemon skipped"
hook cmd_hook.pl ''                                    "$ALLOW" "empty stdin"
hook cmd_hook.pl 'garbage{'                            "$ALLOW" "garbage stdin"
hook cmd_hook.pl "$(cmdj "perl /x/cmd_wrap.pl --cat test -- 'pytest'")" "$ALLOW" "no double wrap"

# ---------- cmd_wrap: exit codes / passthrough / compression ----------
W="perl $S/cmd_wrap.pl"
o=$($W --cat test -- 'echo hi; exit 3'); rc=$?
[[ $rc -eq 3 && "$o" == "hi" ]] && ok || bad "rc=3 propagated + tiny passthrough" "rc=$rc out=$o"
o=$($W --cat build -- 'kill -TERM $$'); rc=$?
[[ $rc -eq 143 ]] && ok || bad "signal rc 128+15" "rc=$rc"
o=$($W --cat test -- 'echo out; echo err >&2'); [[ "$o" == *out* && "$o" == *err* ]] && ok || bad "stderr captured" "$o"
o=$(cd "$T" && $W --cat test -- 'pwd'); [[ "$o" == "$T" ]] && ok || bad "cwd preserved" "$o"

# pytest failure keeps assertion + user frame + summary
{ for i in $(seq 1 300); do echo "tests/test_a.py::test_ok_$i PASSED"; done
  echo "    def test_bad():"; echo ">       assert add(1, 2) == 4"; echo "E       assert 3 == 4"
  echo "tests/test_a.py:42: AssertionError"; echo "FAILED tests/test_a.py::test_bad - assert 3 == 4"
  echo "==== 1 failed, 300 passed in 1.2s ===="; } > "$T/pyfail.txt"
o=$($W --cat test --test-stdin --test-rc 1 < "$T/pyfail.txt"); rc=$?
[[ $rc -eq 1 && "$o" == *"assert 3 == 4"* && "$o" == *"tests/test_a.py:42"* && "$o" == *"1 failed, 300 passed"* ]] && ok || bad "pytest fail signal kept" "$o"
[[ ${#o} -lt $(wc -c < "$T/pyfail.txt") ]] && ok || bad "pytest fail compressed" "${#o}"

# pytest success keeps summary
o=$($W --cat test --test-stdin --test-rc 0 < <(grep -v -e FAILED -e assert -e Error -e 'def ' "$T/pyfail.txt" | sed 's/1 failed, //'))
[[ "$o" == *"300 passed"* ]] && ok || bad "pytest pass summary kept" "$o"

# git diff hunks byte-identical (minus index lines), lockfile omitted
{ echo "diff --git a/src/x.py b/src/x.py"; echo "index abc1234..def5678 100644"; echo "--- a/src/x.py"; echo "+++ b/src/x.py"; echo "@@ -1,3 +1,3 @@"
  echo "    def f():   # three   spaces"; echo "-        return  1"; echo "+        return  2"; echo ""
  echo "diff --git a/package-lock.json b/package-lock.json"; echo "--- a/package-lock.json"; echo "+++ b/package-lock.json"
  for i in $(seq 1 400); do echo "+    \"dep$i\": \"1.0.$i\","; done; } > "$T/diff.txt"
o=$($W --cat git_diff --test-stdin < "$T/diff.txt")
[[ "$o" == *"    def f():   # three   spaces"* && "$o" == *"+        return  2"* && "$o" == *"omitted lockfile"* && "$o" != *"index abc1234"* ]] && ok || bad "git diff hunks preserved" "$(head -c 400 <<<"$o")"

# error lines never dropped in ssh/logs
{ for i in $(seq 1 400); do echo "2026-01-01T00:00:0$((i%10)) INFO request id=$i ok"; done
  echo "2026-01-01T00:00:09 ERROR db timeout code=E1234"; for i in $(seq 1 100); do echo "2026-01-01T00:01:0$((i%10)) INFO tick"; done; } > "$T/log.txt"
for c in ssh logs; do
  o=$($W --cat $c --test-stdin < "$T/log.txt"); [[ "$o" == *"ERROR db timeout code=E1234"* ]] && ok || bad "$c keeps ERROR" "$(head -c 300 <<<"$o")"
done

# net-savings guard: never larger than input
printf 'a\nb\nc\n' > "$T/small.txt"
for c in test pkg build lint git_diff git_log search find ssh logs table cloud chain http archive iac; do
  o=$($W --cat $c --test-stdin < "$T/small.txt"); [[ ${#o} -le 6 ]] && ok || bad "small passthrough $c" "$o"
done

# ---------- read_hook ----------
seq 1 2000 | sed 's/^/line of code number /' > "$T/big.py"
printf 'x\n' > "$T/small.py"
printf '{"lockfileVersion":3}\n%.0s' $(seq 1 3000) > "$T/package-lock.json"
head -c 20000 /dev/urandom > "$T/blob.dat"
rj() { printf '{"toolInput":{"AbsolutePath":"%s"%s}}' "$1" "${2:-}"; }
hook read_hook.pl "$(rj "$T/small.py")"                     "$ALLOW"               "small read allowed"
hook read_hook.pl "$(rj "$T/big.py")"                       '"EndLine":250'        "big unbounded read capped (lossless default)"
hook read_hook.pl "$(rj "$T/big.py" ',"StartLine":10,"EndLine":200')" "$ALLOW"     "bounded read allowed"
hook read_hook.pl "$(rj "$T/package-lock.json")"            '"decision":"deny"'    "lockfile blocked"
hook read_hook.pl "$(rj "$T/blob.dat")"                     '"decision":"deny"'    "binary blocked"
hook read_hook.pl "$(rj "$T/nope.py")"                      "$ALLOW"               "missing file allowed"
hook read_hook.pl "$(rj "$T/big.py" ',"ByteOffset":100')"   "$ALLOW"               "ByteOffset passthrough"

# ---------- write_hook ----------
seq 1 400 | sed 's/^/    value = compute_something(index) # line /' > "$T/w.py"
wj() { printf '{"toolInput":{"TargetFile":"%s","Overwrite":true,"CodeContent":"%s"}}' "$1" "$2"; }
hook write_hook.pl "$(wj "$T/w.py" 'def f():\n    # ... existing code ...\n    pass')" '"decision":"deny"' "lazy truncation blocked"
hook write_hook.pl "$(wj "$T/new.py" 'print(1)')"                                    "$ALLOW"            "new file allowed"

# ---------- find_hook ----------
hook find_hook.pl '{"toolInput":{"SearchDirectory":"/x","Pattern":"*.py","Excludes":["a"]}}' "$ALLOW" "find explicit excludes kept"

# ---------- post_invocation / stop: never model-visible text ----------
hook post_invocation_alerts.pl '{"invocationNum":1}' '^\{\}$' "alerts early-turn silent"
hook stop_telemetry.pl '{}' '^\{\}$' "stop silent"

# =====================================================================
# v2.1 additions
# =====================================================================
j() { python3 -c 'import json,sys; print(json.dumps({"toolCall":{"name":"run_command","args":{"CommandLine":sys.argv[1],"Cwd":sys.argv[2]}},"cwd":"/"}))' "$1" "${2:-$T}"; }
# --- Cwd (per-call arg) is honoured; ~ expansion for stat predictors
head -c 60000 /dev/zero | tr '\0' 'a' | fold -w 100 > "$T/big.log"
hook cmd_hook.pl "$(j 'cat big.log')"                   "--cat cat_large"  "Cwd used for cat stat"
mkdir -p "$T/home"; seq 1 10 > "$T/home/small.txt"
o=$(HOME="$T/home" bash -c "printf '%s' '$(j 'grep -n 5 ~/small.txt')' | perl '$S/cmd_hook.pl'"); [[ "$o" =~ $ALLOW ]] && ok || bad "~ small grep skipped" "$o"
# --- ; and newline chains classified, original string preserved exactly
hook cmd_hook.pl "$(j 'export PATH=/x:$PATH; cargo test -p a')" "--cat test -- 'export PATH=/x:\\\$PATH; cargo test -p a'" "export; cargo test wraps verbatim"
hook cmd_hook.pl "$(j 'cargo fmt && cargo clippy && cargo test')" "--cat chain" "fmt+clippy+test chain"
hook cmd_hook.pl "$(j 'cd /tmp; pytest 2>/dev/null')"   "--cat test"   "2>/dev/null harmless"
hook cmd_hook.pl "$(j 'pytest > /dev/null')"            "$ALLOW"       "stdout redirect still skipped"
hook cmd_hook.pl "$(j $'cat <<EOF\nx\nEOF')"            "$ALLOW"       "heredoc still skipped"
hook cmd_hook.pl "$(j 'for f in a b; do pytest $f; done')" "$ALLOW"    "loop skipped"
hook cmd_hook.pl "$(j 'pytest $UNSET_LOCAL_VAR_X')"     "$ALLOW"       "unexported var skipped"
hook cmd_hook.pl "$(j 'cmd.exe /c "npx remotion bundle src/i.ts" < /dev/null')" "--cat build" "cmd.exe /c remotion"
# --- ssh: read-only skipped, scripts wrapped, streams skipped
hook cmd_hook.pl "$(j "ssh h 'sed -n \"90,320p\" /srv/a.py'")" "$ALLOW" "ssh sed -n skipped"
hook cmd_hook.pl "$(j "ssh h 'cat /etc/x | grep y'")"           "$ALLOW" "ssh cat|grep skipped"
hook cmd_hook.pl "$(j "ssh h 'tail -F /var/log/x'")"            "$ALLOW" "ssh tail -F skipped"
hook cmd_hook.pl "$(j "ssh h 'pm2 logs app'")"                  "$ALLOW" "ssh pm2 logs skipped"
hook cmd_hook.pl "$(j "ssh h 'watch df'")"                      "$ALLOW" "ssh watch skipped"
hook cmd_hook.pl "$(j $'ssh h \'\necho "=== P ==="\nps -ef\'')"  "--cat ssh" "multiline ssh script wraps"
hook cmd_hook.pl "$(j "ssh h 'systemctl status nginx'")"        "--cat ssh" "ssh status wraps"
# --- false positives removed
hook cmd_hook.pl "$(j 'vite')"                          "$ALLOW" "bare vite dev server"
hook cmd_hook.pl "$(j 'make dev')"                      "$ALLOW" "make dev"
hook cmd_hook.pl "$(j 'gradle bootRun')"                "$ALLOW" "gradle bootRun"
hook cmd_hook.pl "$(j 'kubectl logs p --follow=true')"  "$ALLOW" "kubectl --follow=true"
hook cmd_hook.pl "$(j 'aws ec2 describe-instances')"    "$ALLOW" "aws JSON default"
hook cmd_hook.pl "$(j 'aws ec2 describe-instances --output table')" "--cat cloud" "aws table"
hook cmd_hook.pl "$(j 'kubectl get cm -A -o yaml')"     "$ALLOW" "kubectl -o yaml"
hook cmd_hook.pl "$(j 'git show HEAD:README.md')"       "$ALLOW" "git show rev:path"
hook cmd_hook.pl "$(j 'grep -rl foo src')"              "--cat find" "grep -l -> find"
# --- missed wins
hook cmd_hook.pl "$(j 'pm2 list')"                      "--cat table" "pm2 list"
hook cmd_hook.pl "$(j 'docker ps -a')"                  "--cat table" "docker ps"
hook cmd_hook.pl "$(j 'pnpm vitest run')"               "--cat test"  "pnpm vitest"
hook cmd_hook.pl "$(j 'python manage.py test')"         "--cat test"  "manage.py test"
hook cmd_hook.pl "$(j 'go mod tidy')"                   "--cat pkg"   "go mod tidy"
nhook cmd_hook.pl "$(j 'pytest')"                       '/\./scripts' "no /./ in wrapper path"
long=$(printf 'pytest %070000d' 0); hook cmd_hook.pl "$(j "$long")" "$ALLOW" "60KB+ command skipped (E2BIG)"

# --- wrapper: zero-loss + recoverability
{ for i in $(seq 1 40); do echo "    if a || b | c:  # $i"; echo '    FMT = "%-10s    %5d"'; echo; echo; echo "trailing  "; done; } > "$T/code.txt"
o=$($W --cat ssh --test-stdin < "$T/code.txt"); [[ "$o" == "$(cat "$T/code.txt")" ]] && ok || bad "ssh code verbatim (|| / spaces / blanks)" "$(head -c 200 <<<"$o")"
o=$($W --cat table --test-stdin < "$T/code.txt"); [[ "$o" == *'if a || b | c:'* && "$o" == *'"%-10s    %5d"'* ]] && ok || bad "table pad never alters code" "$(head -c 200 <<<"$o")"
{ echo "┌────┬──────────┬────────┐"; echo "│ id │ name     │ status │"; echo "├────┼──────────┼────────┤"; for i in $(seq 1 30); do echo "│ $i  │ app$i     │ online │"; done; echo "└────┴──────────┴────────┘"; } > "$T/pm2.txt"
o=$($W --cat table --test-stdin < "$T/pm2.txt"); [[ "$o" == *"| 7 | app7 | online |"* && "$o" != *"┌"* && "$o" != *"full:"* ]] && ok || bad "pm2 box table lossless compaction" "$(head -c 200 <<<"$o")"
for i in $(seq 1 251); do echo "$((i*3)):2024-11-01 value $i"; done > "$T/g1.txt"
o=$($W --cat search --test-stdin < "$T/g1.txt"); [[ "$o" != *"across"* && "$o" == *"3:2024-11-01 value 1"* ]] && ok || bad "single-file grep -n not grouped by line no" "$(head -c 200 <<<"$o")"
seq 1 500 | sed 's/^/README line /' > "$T/show.txt"
o=$($W --cat git_diff --test-stdin < "$T/show.txt"); [[ "$o" == "$(cat "$T/show.txt")" ]] && ok || bad "git show file content verbatim" "${o:0:200}"
printf 'diff --git a/x b/x\r\n--- a/x\r\n+++ b/x\r\n@@ -1 +1 @@\r\n-a\r\n+b\r\n' > "$T/crlf.diff"; for i in $(seq 1 30); do printf '+line %d\r\n' $i >> "$T/crlf.diff"; done
o=$($W --cat git_diff --test-stdin < "$T/crlf.diff" | od -c | grep -c '\\r'); [[ $o -gt 0 ]] && ok || bad "git_diff keeps CRLF" "$o"
{ for i in $(seq 1 40); do echo "t$i PASSED"; done; echo "Traceback (most recent call last):"; echo '  File "/app/main.py", line 10, in <module>'; echo "    run()"
  echo '  File "/usr/lib/python3/site-packages/requests/api.py", line 59, in get'; echo "    return request(x)"; echo '  File "/app/main.py", line 20, in run'; echo "    boom()"; echo "ValueError: bad"
  for i in $(seq 1 30); do echo "padding line $i xxxxxxxxxxxxxxxxxxxx"; done; } > "$T/tb.txt"
o=$($W --cat test --test-stdin --test-rc 1 < "$T/tb.txt"); [[ "$o" == *'requests/api.py", line 59'* && "$o" == *"return request(x)"* ]] && ok || bad "lone library frame kept whole" "$o"
for i in $(seq 1 30); do echo "10:00:$((10+i)) ERROR job id=$i failed"; done > "$T/jobs.txt"
o=$($W --cat logs --test-stdin < "$T/jobs.txt"); [[ "$o" == *"id=17 failed"* ]] && ok || bad "small log (<native cap) keeps every distinct line" "$o"
{ for i in $(seq 1 600); do echo "2026-01-01T00:00:00 INFO worker id=$i heartbeat ok status nominal"; done; echo "=== SECTION B ==="; echo "2026-01-01 ERROR disk full code=E77"; for i in $(seq 1 300); do echo "2026-01-01T00:00:01 DEBUG tick $i padding padding"; done; } > "$T/biglog.txt"
o=$($W --cat logs --test-stdin < "$T/biglog.txt"); [[ "$o" == *"ERROR disk full code=E77"* && "$o" == *"agy-tokensaver-"* && "$o" == *"full: "* ]] && ok || bad "big log keeps ERROR + spill note" "$(tail -c 300 <<<"$o")"
o=$($W --cat ssh --test-stdin < "$T/biglog.txt"); [[ "$o" == *"=== SECTION B ==="* && "$o" == *"code=E77"* ]] && ok || bad "big ssh keeps section header + error" "$(tail -c 300 <<<"$o")"
sp=$(grep -o 'full: [^]]*' <<<"$o" | cut -c7-); [[ -f "$sp" ]] && diff -q "$sp" "$T/biglog.txt" >/dev/null && ok || bad "spill file == full original output" "$sp"
{ echo "token:"; head -c 1500 /dev/urandom | base64 -w0; echo; for i in $(seq 1 30); do echo "r$i"; done; } > "$T/tok.txt"
o=$($W --cat cloud --test-stdin < "$T/tok.txt"); [[ "$o" != *"<BASE64:"* || "$o" == *"full: "* ]] && ok || bad "base64 scrub never unrecoverable" "${o:0:200}"
for i in $(seq 1 5000); do printf 'x\b'; done > "$T/bs.txt"; s0=$(date +%s%N); $W --cat build --test-stdin < "$T/bs.txt" >/dev/null; el=$(( ($(date +%s%N)-s0)/1000000 ))
[[ $el -lt 1500 ]] && ok || bad "backspace storm bounded" "${el}ms"
o=$($W --cat cat_large --test-stdin < <(printf 'é%.0s' $(seq 1 300); echo)); iconv -f UTF-8 -t UTF-8 <<<"$o" >/dev/null 2>&1 && ok || bad "line cap keeps valid UTF-8" "${o:0:80}"
# TTY: child stays in foreground pgrp => prompts don't hang (GNU script(1) + timeout on Linux)
if [[ "$(uname -s)" == "Linux" ]] && command -v script >/dev/null && command -v timeout >/dev/null; then
  o=$(printf 'hello\n' | timeout 5 script -qfc "perl $S/cmd_wrap.pl --cat test -- 'read x; echo got=\$x'" /dev/null 2>&1); rc=$?
  [[ $rc -ne 124 && "$o" == *"got=hello"* ]] && ok || bad "tty prompt does not hang" "rc=$rc $o"
fi
# Telemetry is honest: passthrough records negative net (notice cost), never inflated
$W --cat test -- 'echo hi' >/dev/null; t=$(tail -1 "$PLUGIN_DATA/events.jsonl")
[[ "$t" == *'"est_tokens_saved":-'* ]] && ok || bad "passthrough telemetry negative" "$t"

# --- read_hook v2.1
seq 1 1000 | sed 's/^/x = compute(value)  # some realistic python code line here/' > "$T/mid.py"
hook read_hook.pl "$(rj "$T/mid.py" ',"StartLine":1,"EndLine":800')"   "$ALLOW" "explicit agent range respected"
hook read_hook.pl "$(rj "$T/mid.py" ',"EndLine":600')"                 "$ALLOW" "explicit EndLine respected"
hook read_hook.pl "$(rj "$T/mid.py")"                                  '"EndLine":250' "unbounded still capped"
{ echo "<style>$(head -c 5000 /dev/zero | tr '\0' 'a')</style>"; seq 1 2000 | sed 's/^/<p>normal html line<\/p>/'; } > "$T/page.html"
hook read_hook.pl "$(rj "$T/page.html" ',"StartLine":900,"EndLine":950')" "$ALLOW" "narrow bounded read ignores long line elsewhere"
mkdir -p "$T/b/.system_generated/steps/3"; { head -c 30000 /dev/zero | tr '\0' 'm'; echo; } > "$T/b/.system_generated/steps/3/output.txt"
nhook read_hook.pl "$(rj "$T/b/.system_generated/steps/3/output.txt" ',"StartLine":1,"EndLine":300')" '"decision":"deny"' "spillback never denied as dense"
mkdir -p "$T/build/proj"; seq 1 1500 | sed 's/^/fn line() { let x = 1; } \/\/ rust/' > "$T/build/proj/main.rs"
nhook read_hook.pl "$(rj "$T/build/proj/main.rs")" '"decision":"deny"' "user source under build/ not denied"
mkdir -p "$T/node_modules/pkg"; seq 1 1500 | sed 's/^/Some readme prose line here./' > "$T/node_modules/pkg/README.md"
nhook read_hook.pl "$(rj "$T/node_modules/pkg/README.md")" '"decision":"deny"' "vendor docs not denied"
echo "plain text" > "$T/notes.dat"
hook read_hook.pl "$(rj "$T/notes.dat")" "$ALLOW" "text .dat not denied"
hook read_hook.pl "$(rj "$T/blob.dat")" '"decision":"deny"' "binary .dat still denied"
o=$(python3 "$S/stats.py" --days 1 2>&1); [[ "$o" != *Traceback* ]] && ok || bad "stats.py runs (no inspect shadowing)" "${o:0:200}"
echo '{"a":[1,2,3],"b":{"c":"d"}}' > "$T/x.json"; o=$(python3 "$S/ts_inspect.py" json "$T/x.json" 2>&1); [[ $? -eq 0 && "$o" != *Traceback* ]] && ok || bad "ts_inspect.py json runs" "$o"

# --- write_hook v2.1: near-identical overwrite is logged, not denied (tokens already spent)
{ cat "$T/w.py"; echo "    extra = 1"; } > "$T/w2.txt"
c=$(python3 -c 'import json,sys; print(json.dumps(open(sys.argv[1]).read())[1:-1])' "$T/w2.txt")
hook write_hook.pl "$(wj "$T/w.py" "$c")" "$ALLOW" "huge near-identical overwrite allowed"
# --- find_hook v2.1: only inject excludes that exist; respect Pattern
mkdir -p "$T/proj/node_modules" "$T/proj/src"
hook  find_hook.pl "{\"toolInput\":{\"SearchDirectory\":\"$T/proj\",\"Pattern\":\"*.js\"}}" '"Excludes":\["node_modules"\]' "find excludes only existing dirs"
hook  find_hook.pl "{\"toolInput\":{\"SearchDirectory\":\"$T/proj/src\",\"Pattern\":\"*.js\"}}" "$ALLOW" "no excluded dirs -> no overwrite"
hook  find_hook.pl "{\"toolInput\":{\"SearchDirectory\":\"$T/proj\",\"Pattern\":\"node_modules/react/*.js\"}}" "$ALLOW" "Pattern targeting node_modules respected"
# --- alerts: checkpoint suppresses false cache-drop alert
{ for n in 1 2 3; do echo "{\"type\":\"PLANNER_RESPONSE\",\"input_tokens\":1000,\"cache_read_tokens\":90000,\"output_tokens\":10,\"created_at\":\"2026-01-01T00:00:0${n}Z\"}"; done
  echo '{"type":"CHECKPOINT"}'; echo '{"type":"PLANNER_RESPONSE","input_tokens":30000,"cache_read_tokens":60000,"output_tokens":10,"created_at":"2026-01-01T00:00:09Z"}'; } > "$T/tx.jsonl"
hook post_invocation_alerts.pl "{\"invocationNum\":9,\"transcriptPath\":\"$T/tx.jsonl\",\"conversationId\":\"t1\"}" '^\{\}$' "checkpoint suppresses cache-drop alert"

# =====================================================================
# v2.5.0 additions (free zero-loss savings)
# =====================================================================
hook cmd_hook.pl "$(j 'pytest -x || true')"                  "--cat test"   "pytest || true wraps"
hook cmd_hook.pl "$(j "ssh h 'journalctl -n 100' || true")" "--cat ssh"    "ssh || true wraps"
hook cmd_hook.pl "$(j 'find . -name "*.py" | grep foo')"    "--cat search" "safe find|grep pipe wraps"
hook cmd_hook.pl "$(j 'ps aux | grep python | sort')"       "--cat ps"     "safe ps|grep|sort pipe wraps"
hook cmd_hook.pl "$(j 'pytest | head -n 20')"               "$ALLOW"       "user-bounded | head skipped"
hook cmd_hook.pl "$(j 'pytest | wc -l')"                    "$ALLOW"       "user-bounded | wc skipped"
hook cmd_hook.pl "$(j 'pytest || echo FAIL')"               "$ALLOW"       "non-trivial || echo skipped"
hook cmd_hook.pl "$(j 'systemctl status nginx')"            "--cat logs"   "systemctl status wraps"
hook cmd_hook.pl "$(j 'tmux capture-pane -p -t dev')"       "--cat logs"   "tmux capture-pane wraps"
hook cmd_hook.pl "$(j 'strings /bin/ls')"                   "--cat search" "strings wraps"
hook cmd_hook.pl "$(j 'uv.exe run pytest tests/')"          "--cat test"   "uv.exe run unwrap"

seq 1 300 | sed 's/^/def func_line(): return 12345  # realistic python code/' > "$T/dup.py"
rdj() { printf '{"toolInput":{"Path":"%s"%s},"stepIdx":%d,"conversationId":"dupconv"}' "$1" "$2" "$3"; }
hook read_hook.pl "$(rdj "$T/dup.py" ',"StartLine":1,"EndLine":200' 10)"   "$ALLOW"            "initial bounded read allowed"
hook read_hook.pl "$(rdj "$T/dup.py" ',"StartLine":50,"EndLine":120' 18)"  '"decision":"deny"' "superset-covered slice blocked within 12 steps"
hook read_hook.pl "$(rdj "$T/dup.py" ',"StartLine":201,"EndLine":280' 19)" "$ALLOW"            "non-overlapping slice allowed"
hook read_hook.pl "$(rdj "$T/dup.py" ',"StartLine":50,"EndLine":120' 35)"  "$ALLOW"            "stale >12 steps allowed"

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

# --- New zero-loss hook optimizations (chained pipes, multi-dir ls, test_*.py, yaml cap)
hook cmd_hook.pl "$(j 'cd /tmp && find . -name "*.py" | grep foo')" "--cat search" "chained safe filter pipe wraps"
mkdir -p "$T/ls1" "$T/ls2"; for i in $(seq 1 25); do touch "$T/ls1/f$i" "$T/ls2/g$i"; done
hook cmd_hook.pl "$(j "ls -la $T/ls1 $T/ls2")"                      "--cat table"  "multi-dir ls -la wraps when combined >37"
hook cmd_hook.pl "$(j 'python3 tests/test_core.py')"                "--cat logs"   "python3 test_*.py wraps"
seq 1 500 | sed 's/^/key_name: some_yaml_config_value_here_1234567890/' > "$T/big.yaml"
hook read_hook.pl "$(rj "$T/big.yaml")"                             '"EndLine":120' "large unbounded yaml capped"
hook cmd_hook.pl "$(j 'pytest $env:PYTEST_ARGS')"                   "$ALLOW"        "PowerShell \$env: skipped"
hook cmd_hook.pl "$(j 'Get-ChildItem | Select-String foo')"         "$ALLOW"        "PowerShell cmdlet skipped"
seq 1 250 | sed 's/^/{"key":"val_1234567890_abcdefghij_1234567890"}/' > "$T/mid10k.jsonl"
hook read_hook.pl "$(rj "$T/mid10k.jsonl")"                         '"EndLine":120' "data file >8KB unbounded capped"
{ echo "diff --git a/mcp_app/vendor/ext-apps.js b/mcp_app/vendor/ext-apps.js"; echo "--- a/mcp_app/vendor/ext-apps.js"; echo "+++ b/mcp_app/vendor/ext-apps.js"
  for i in $(seq 1 300); do echo "+var line_$i = 'minified vendor bundle payload';"; done; } > "$T/vdiff.txt"
o=$($W --cat git_diff --test-stdin < "$T/vdiff.txt"); [[ "$o" == *"omitted lockfile"* ]] && ok || bad "vendor bundle omitted in git_diff" "$(head -c 200 <<<"$o")"

# --- Regression tests: duplicate-read byte truncation, honest slice telemetry, CRLF lockfile diff
python3 -c 'print(("x" * 299 + "\n") * 300, end="")' > "$T/wide300.txt"
hook read_hook.pl "$(printf '{"toolInput":{"Path":"%s","StartLine":1,"EndLine":250},"stepIdx":10,"conversationId":"trunc1"}' "$T/wide300.txt")" "$ALLOW" "wide-line 1-250 initial read allowed"
hook read_hook.pl "$(printf '{"toolInput":{"Path":"%s","StartLine":240,"EndLine":240},"stepIdx":12,"conversationId":"trunc1"}' "$T/wide300.txt")" "$ALLOW" "truncated line 240 past 46KB NOT falsely blocked"
hook read_hook.pl "$(printf '{"toolInput":{"Path":"%s","StartLine":10,"EndLine":10},"stepIdx":13,"conversationId":"trunc1"}' "$T/wide300.txt")" '"decision":"deny"' "delivered line 10 within prefix blocked"
python3 - "$PLUGIN_DATA/events.jsonl" <<'PYEOF' && ok || bad "1-line duplicate telemetry uses slice bytes minus overhead" "check failed"
import json, sys
recs = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
ev = [r for r in recs if r.get("session_id") == "trunc1" and r.get("category") == "duplicate_read"][-1]
assert 290 <= ev["raw_bytes"] <= 310, f"expected ~300B slice raw_bytes, got {ev}"
assert ev["kept_bytes"] >= 100, f"expected denial message bytes counted, got {ev}"
assert 0 <= ev["est_tokens_saved"] < 100, f"inflated token savings on 1-line block: {ev}"
PYEOF

python3 -c 'print(("y" * 39 + "\n") * 500, end="")' > "$T/short40.txt"
hook read_hook.pl "$(printf '{"toolInput":{"Path":"%s","StartLine":1,"EndLine":50},"stepIdx":1,"conversationId":"neg1"}' "$T/short40.txt")" "$ALLOW" "short-line 1-50 initial read allowed"
hook read_hook.pl "$(printf '{"toolInput":{"Path":"%s","StartLine":10,"EndLine":10},"stepIdx":2,"conversationId":"neg1"}' "$T/short40.txt")" '"decision":"deny"' "short-line 10 duplicate blocked"
python3 - "$PLUGIN_DATA/events.jsonl" <<'PYEOF' && ok || bad "tiny 1-line duplicate logs negative net tokens" "check failed"
import json, sys
recs = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
ev = [r for r in recs if r.get("session_id") == "neg1" and r.get("category") == "duplicate_read"][-1]
assert 35 <= ev["raw_bytes"] <= 45, f"expected ~40B raw_bytes, got {ev}"
assert ev["est_tokens_saved"] < 0, f"expected negative net tokens for 40B blocked slice, got {ev}"
PYEOF

python3 -c '
out = (
    "diff --git a/service.py b/service.py\r\n"
    "--- a/service.py\r\n+++ b/service.py\r\n@@ -1,2 +1,2 @@\r\n-        return 500\r\n+        return 401\r\n"
    "diff --git a/package-lock.json b/package-lock.json\r\n"
    "--- a/package-lock.json\r\n+++ b/package-lock.json\r\n"
    + "".join(f"+    \"dep-{i}\": \"1.0.{i}\",\r\n" for i in range(1, 350))
)
import sys; sys.stdout.buffer.write(out.encode("utf-8"))
' > "$T/crlf_diff.txt"
o=$($W --cat git_diff --test-stdin < "$T/crlf_diff.txt")
[[ "$o" == *"+        return 401"* && "$o" == *"omitted lockfile"* ]] && ok || bad "CRLF git_diff omits lockfile and keeps code hunk" "$(head -c 200 <<<"$o")"

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
