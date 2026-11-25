#!/usr/bin/env bash
# Regression tests for the "uncovered tools" hooks: grep_hook.pl (+ hooks.json wiring) and
# scripts/replay_ext.py replay handlers.
# Usage: bash tests/test_tools.sh   (exit 0 = all pass)
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
S="$ROOT/scripts"
PY="$(command -v python3 || command -v python)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export PLUGIN_DATA="$T/pdata"; mkdir -p "$PLUGIN_DATA"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); }
bad() { FAIL=$((FAIL+1)); printf 'FAIL: %s\n  got: %s\n' "$1" "$2"; }
hook()  { local o; o=$(printf '%s' "$2" | "$PY" -S "$S/$1" 2>&1); [[ "$o" =~ $3 ]] && ok || bad "$4" "$o"; }
ALLOW='^\{"decision":"allow"\}$'

# A Windows-style absolute path to a real temp file (Windows Python accepts C:/... and C:\...).
WT=""
if command -v cygpath >/dev/null 2>&1; then WT="$(cygpath -m "$T")"; fi   # e.g. C:/Users/x/AppData/Local/Temp/tmp.abc
mkdir -p "$T/proj/sub"
printf '<!DOCTYPE html>\n<html>\n<script>class SoundEngine {}</script>\n' > "$T/proj/index.html"
printf 'x\n' > "$T/proj/we[ird].txt"
gj() { printf '{"toolCall":{"name":"grep_search","args":{"Query":"%s","SearchPath":"%s","MatchPerLine":true}},"lastUserInput":"see \\"SearchPath\\":\\"C:/nope\\""}' "$1" "$2"; }

# ---------- grep_hook: safety / fail-open ----------
hook grep_hook.py ''                                   "$ALLOW" "grep: empty stdin"
hook grep_hook.py 'garbage{"SearchPath'                "$ALLOW" "grep: garbage stdin"
hook grep_hook.py "$(gj x /etc/passwd)"                "$ALLOW" "grep: POSIX path untouched"
hook grep_hook.py "$(gj x 'C:/definitely/not/here.html')" "$ALLOW" "grep: missing file untouched"
hook grep_hook.py '{"toolCall":{"name":"grep_search","args":{"Query":"x"}}}' "$ALLOW" "grep: no SearchPath"

if [[ -n "$WT" ]]; then
  # ---------- grep_hook: single-file SearchPath -> parent dir + Includes ----------
  hook grep_hook.py "$(gj DOCTYPE "$WT/proj/index.html")" \
       "^\\{\"decision\":\"allow\",\"overwrite\":\\{\"SearchPath\":\"$WT/proj\",\"Includes\":\\[\"index.html\"\\]\\}\\}$" \
       "grep: file -> dir + Includes (forward slashes)"
  BS="${WT//\//\\\\}"   # JSON-escaped backslash form C:\\Users\\...
  hook grep_hook.py "$(gj DOCTYPE "$BS\\\\proj\\\\index.html")" '"SearchPath":"[A-Za-z]:\\\\.*\\\\proj","Includes":\["index.html"\]' \
       "grep: file -> dir + Includes (backslashes, re-escaped)"
  hook grep_hook.py "$(gj x "$WT/proj")"               "$ALLOW" "grep: directory SearchPath untouched"
  hook grep_hook.py "$(gj x "$WT/proj/")"              "$ALLOW" "grep: directory with trailing slash untouched"
  hook grep_hook.py "$(gj x "$WT/proj/we[ird].txt")"   "$ALLOW" "grep: glob metachar basename untouched"
  # Includes already present (pointing elsewhere) is replaced by the file's own name
  o=$(printf '{"toolCall":{"name":"grep_search","args":{"Query":"x","SearchPath":"%s","Includes":["*.js"]}}}' "$WT/proj/index.html" | "$PY" -S "$S/grep_hook.py")
  [[ "$o" == *'"Includes":["index.html"]'* ]] && ok || bad "grep: Includes replaced by basename" "$o"
  # Output must be valid JSON
  o=$(printf '%s' "$(gj DOCTYPE "$WT/proj/index.html")" | "$PY" -S "$S/grep_hook.py")
  printf '%s' "$o" | "$PY" -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null && ok || bad "grep: output is valid JSON" "$o"
else
  echo "note: cygpath not available; Windows-path grep_hook cases skipped"
fi

# ---------- hooks.json wiring ----------
"$PY" - "$ROOT/hooks.json" <<'EOF' && ok || bad "hooks.json: grep_search wired to grep_hook.py" "see above"
import json, re, sys
h = json.load(open(sys.argv[1]))
g = h["token-saver-grep"]["PreToolUse"][0]
assert re.fullmatch(g["matcher"], "grep_search")
assert g["hooks"][0]["command"] == "python3 -S ./scripts/grep_hook.py"
f = h["token-saver-find"]["PreToolUse"][0]["matcher"]
assert re.fullmatch(f, "find_by_name") and re.fullmatch(f, "find") and not re.fullmatch(f, "find_all_references")
EOF

# ---------- replay_ext handlers ----------
if [[ -n "$WT" ]]; then
"$PY" - "$S" "$WT" <<'EOF' && ok || bad "replay_ext: grep/find handlers" "see above"
import os, sys
sys.path.insert(0, sys.argv[1]); wt = sys.argv[2]
import replay_ext as rx
hdr = "Created At: 2026-01-01T00:00:00Z\nCompleted At: 2026-01-01T00:00:00Z\n"
rec = hdr + "No results found"
args = {"Query": "SoundEngine", "SearchPath": wt + "/proj/index.html", "MatchPerLine": True}
d, tag, retry = rx.handle("grep_search", args, rec, env=dict(os.environ))
body = rx.grep_file(args["SearchPath"], args)
assert tag == "grep:file_fix" and retry == 0, (d, tag)
assert '"LineNumber":3' in body and "SoundEngine" in body, body
assert d == len(rec.encode()) - (len((hdr + body).encode()) + rx.NOTICE_BYTES + rx.NOTICE_KEY_BYTES), d
assert d < 0  # honest: the fixed search returns data instead of a false "No results"
# regex + case-insensitive + Go-style escaping
b2 = rx.grep_file(args["SearchPath"], {"Query": "<!doctype", "CaseInsensitive": True})
assert "\\u003c!DOCTYPE html\\u003e" in b2, b2
# directory SearchPath: no hook change -> None
assert rx.handle("grep_search", {"Query": "x", "SearchPath": wt + "/proj"}, rec, env=dict(os.environ)) is None
# untouched tools
assert rx.handle("webview_browser", {"script": "return 1"}, "x") is None
assert rx.handle("list_dir", {"DirectoryPath": wt}, "x") is None
EOF
fi
mkdir -p "$T/fp/node_modules/a" "$T/fp/src"
"$PY" - "$S" "$T/fp" <<'EOF' && ok || bad "replay_ext: find excludes accounting" "see above"
import os, sys
sys.path.insert(0, sys.argv[1]); d = sys.argv[2]
import replay_ext as rx
rec = "Created At: x\nCompleted At: y\nFound 3 results\nsrc/a.js\nnode_modules/a/index.js\nnode_modules/a/b.js"
r = rx.handle("find_by_name", {"Pattern": "*.js", "SearchDirectory": d}, rec, env=dict(os.environ))
assert r and r[1] == "find:excludes", r
after = "Created At: x\nCompleted At: y\nFound 1 results\nsrc/a.js"
assert r[0] == len(rec.encode()) - len(after.encode()) - rx.NOTICE_BYTES, r
EOF

printf '\ntest_tools: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
