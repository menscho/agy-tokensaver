#!/usr/bin/env bash
# Windows / PowerShell run_command wrapping: hook classification, rewrite form, POSIX isolation,
# lossless PowerShell table/list/JSON compaction, signal-preserving cap, and (when
# powershell.exe exists) LIVE execution of rewritten command lines.
# Usage: bash tests/test_cmd_windows.sh   (exit 0 = all pass)
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
S="$ROOT/scripts"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export PLUGIN_DATA="$T/pdata"; mkdir -p "$PLUGIN_DATA"
PASS=0; FAIL=0; SKIP=0
ok()  { PASS=$((PASS+1)); }
bad() { FAIL=$((FAIL+1)); printf 'FAIL: %s\n  got: %s\n' "$1" "${2:0:400}"; }
ALLOW='^\{"decision":"allow"\}$'
# JSON payload with a Windows Cwd (backslashes JSON-escaped); $1 must already be JSON-safe
wj() { printf '{"toolInput":{"CommandLine":"%s","Cwd":"%s"}}' "$1" "${2:-C:\\\\Users\\\\dev}"; }
hk() { printf '%s' "$1" | env ${HK_ENV:-} perl "$S/cmd_hook.pl" 2>&1; }
has() { [[ "$2" =~ $3 ]] && ok || bad "$1" "$2"; }
hasnt() { [[ "$2" =~ $3 ]] && bad "$1" "$2" || ok; }
WINHOST=0; case "$(perl -e 'print $^O')" in MSWin32|cygwin|msys) WINHOST=1;; esac

# ---------- hook: PowerShell mode classification (Windows host) ----------
if [[ $WINHOST -eq 1 ]]; then
  o=$(hk "$(wj 'Get-ChildItem -Recurse')")
  has  "gci -Recurse wraps as pwsh via here-string" "$o" "^\{\"decision\":\"allow\",\"overwrite\":\{\"CommandLine\":\"@'\\\\nGet-ChildItem -Recurse\\\\n'@ \| & '[A-Za-z]:\\\\\\\\[^']*perl[^']*' '[A-Za-z]:\\\\\\\\[^']*cmd_wrap\.pl' --cat pwsh --pwsh-stdin\"\}\}$"
  hasnt "no POSIX bash form on Windows" "$o" "-- '"
  has  "gci | Where { \$_ } wraps" "$(hk "$(wj 'Get-ChildItem | Where-Object { $_.Name -like \"*.ts\" }')")" "--cat pwsh"
  has  "Get-Process | Format-List wraps" "$(hk "$(wj 'Get-Process | Format-List Id, Name')")" "--cat pwsh"
  has  "npm test wraps as test" "$(hk "$(wj 'npm test')")" "--cat test --pwsh-stdin"
  has  "uv run pytest wraps as test" "$(hk "$(wj 'uv run --with x pytest -q tests')")" "$ALLOW"   # -q => skip
  has  "uv run python -m pytest wraps" "$(hk "$(wj 'uv run python -m pytest tests')")" "--cat test"
  has  "pip install wraps as pkg" "$(hk "$(wj 'pip install requests')")" "--cat pkg"
  has  "zig build wraps as build" "$(hk "$(wj 'zig build')")" "--cat build"
  has  "git clone wraps as git_net" "$(hk "$(wj 'git clone https://x/y.git')")" "--cat git_net"
  has  "npm run build; gci dist -> chain" "$(hk "$(wj 'npm run build; Get-ChildItem dist')")" "--cat chain"
  has  "exit inside a string is not a top-level exit" "$(hk "$(wj 'npm test; node -e \"process.exit(1)\"')")" "--cat test"
  # measured net-negative after the 94 B overwrite notice -> never wrapped alone
  has  "python -c alone not wrapped" "$(hk "$(wj 'python -c \"print(1)\"')")" "$ALLOW"
  has  "node -e alone not wrapped" "$(hk "$(wj 'node -e \"console.log(1)\"')")" "$ALLOW"
  has  "curl.exe alone not wrapped" "$(hk "$(wj 'curl.exe -s https://x')")" "$ALLOW"
  has  "Select-String alone not wrapped" "$(hk "$(wj 'Select-String -Path x -Pattern y')")" "$ALLOW"
  has  "git status not wrapped" "$(hk "$(wj 'git status')")" "$ALLOW"
  # safety skips
  has  "top-level exit skipped (exit code remap)" "$(hk "$(wj 'Get-ChildItem; exit 3')")" "$ALLOW"
  has  "cd skipped (session state)" "$(hk "$(wj 'cd src; npm test')")" "$ALLOW"
  has  "Set-Location skipped" "$(hk "$(wj 'Set-Location src; Get-ChildItem')")" "$ALLOW"
  has  "\$env: assignment skipped" "$(hk "$(wj '$env:CI=1; npm test')")" "$ALLOW"
  has  "function definition skipped" "$(hk "$(wj 'function f { npm test }; f')")" "$ALLOW"
  has  "dot-source skipped" "$(hk "$(wj '. .\\\\env.ps1; npm test')")" "$ALLOW"
  has  "Start-Process skipped" "$(hk "$(wj 'Start-Process npm test')")" "$ALLOW"
  has  "Read-Host skipped" "$(hk "$(wj 'Read-Host x; Get-ChildItem')")" "$ALLOW"
  has  "npm run dev (server) skipped" "$(hk "$(wj 'npm run dev')")" "$ALLOW"
  has  "python REPL skipped" "$(hk "$(wj 'python; Get-ChildItem')")" "$ALLOW"
  has  "unknown native skipped" "$(hk "$(wj 'ffmpeg -i a.mp4 b.mp4; Get-ChildItem')")" "$ALLOW"
  has  "bare curl (Invoke-WebRequest alias) skipped" "$(hk "$(wj 'curl https://x; Get-ChildItem')")" "$ALLOW"
  has  "unterminated string skipped" "$(hk "$(wj 'Get-ChildItem \"x')")" "$ALLOW"
  has  "already wrapped not re-wrapped" "$(hk "$(wj "& 'perl' 'C:\\\\p\\\\cmd_wrap.pl' --cat pwsh --pwsh-stdin")")" "$ALLOW"
  big=$(printf 'Get-ChildItem %.0s' $(seq 1 700))
  has  "huge command (>9000 chars) skipped" "$(hk "$(wj "$big")")" "$ALLOW"
  # non-ASCII or a line starting with '@ -> base64 form (a here-string could not carry it exactly)
  o=$(hk "$(wj 'Get-ChildItem Königsberg')")
  has  "non-ASCII uses --pwsh-b64" "$o" "--cat pwsh --pwsh-b64 '[A-Za-z0-9+/=]+'\"\}\}$"
  sc=$(perl -e 'print "Get-ChildItem; \@\x27\\nx\\n\x27\@ | Out-Null"')
  has  "embedded here-string uses --pwsh-b64" "$(hk "$(wj "$sc")")" "--pwsh-b64"
  b=$(hk "$(wj 'Get-ChildItem Königsberg')" | sed -n "s/.*--pwsh-b64 '\([^']*\)'.*/\1/p")
  d=$(printf '%s' "$b" | base64 -d 2>/dev/null)
  [[ "$d" == "Get-ChildItem Königsberg" ]] && ok || bad "b64 payload == exact UTF-8 command" "$d"

  # ---------- POSIX isolation: Git-Bash style Cwd keeps the old POSIX behaviour ----------
  has "POSIX Cwd keeps bash form" "$(hk "$(wj 'pytest -x' '/tmp/x')")" "--cat test -- 'pytest -x'"
  # Git Bash hands POSIX paths to Windows programs as C:/x; such payloads carry a POSIX top-level cwd
  has "C:/ Cwd + POSIX top-level cwd = POSIX" "$(hk '{"toolCall":{"name":"run_command","args":{"CommandLine":"pytest -x","Cwd":"C:/tmp/x"}},"cwd":"/"}')" "-- 'pytest -x'"
  has "D:/ Cwd alone (Antigravity) = PowerShell" "$(hk "$(wj 'pytest -x' 'D:/_ Projects/x')")" "--pwsh-stdin"
  has "D:/ Cwd + Windows top-level cwd = PowerShell" "$(hk '{"toolInput":{"CommandLine":"pytest -x","Cwd":"D:/p"},"cwd":"D:/p"}')" "--pwsh-stdin"
  has "no Cwd = PowerShell" "$(printf '{"toolInput":{"CommandLine":"npm test"}}' | perl "$S/cmd_hook.pl")" "--pwsh-stdin"
  has "AGY_TS_SHELL=posix forces bash form" "$(AGY_TS_SHELL=posix hk "$(wj 'pytest -x')")" "-- 'pytest -x'"
  has "AGY_TS_SHELL=off disables" "$(AGY_TS_SHELL=off hk "$(wj 'npm test')")" "$ALLOW"
  has "PowerShell syntax never reaches bash form" "$(hk "$(wj 'pytest $env:ARGS')")" "--pwsh-stdin|$ALLOW"
else
  has "AGY_TS_SHELL=pwsh on POSIX host gives PowerShell form" "$(AGY_TS_SHELL=pwsh hk "$(wj 'npm test' '/tmp')")" "--pwsh-stdin|$ALLOW"
  has "POSIX host unchanged" "$(hk "$(wj 'pytest -x' '/tmp')")" "--cat test -- 'pytest -x'"
fi

# ---------- cmd_wrap: lossless PowerShell compaction (--test-stdin) ----------
W="perl $S/cmd_wrap.pl"
pad() { printf '%-119s\r\n' "$1"; }
{
  printf '\r\n'
  for d in 'C:\proj' 'C:\proj\src'; do
    printf '\r\n    Directory: %s\r\n\r\n\r\n' "$d"
    pad 'Mode                 LastWriteTime         Length Name'
    pad '----                 -------------         ------ ----'
    for i in $(seq 1 12); do pad "-a----        02.09.2026     17:4$((i%10))           $((i*137)) file  $i.ts"; done
    pad 'd-----        02.09.2026     16:30                sub dir'
  done
} > "$T/gci.txt"
o=$($W --cat pwsh --test-stdin < "$T/gci.txt")
grep -qxF 'Directory: C:\proj\src' <<<"$o" && ok || bad "gci: banner unindented" "$o"
c=$(grep -c '^Mode' <<<"$o"); [[ $c -eq 1 ]] && ok || bad "gci: repeated header dropped (1 left)" "$c"
hasnt "gci: no CR / trailing pad" "$o" $'\r| \n'
has   "gci: row padding collapsed" "$o" "-a----  02.09.2026  17:41  137 file  1.ts"
c=$(grep -c 'file  [0-9]*\.ts' <<<"$o"); [[ $c -eq 24 ]] && ok || bad "gci: every file row kept (24)" "$c"
has   "gci: names with double spaces verbatim (last column)" "$o" "file  12\.ts"
hasnt "gci: lossless => no spill" "$o" "full: "
[[ $(wc -c <<<"$o") -lt $(( $(wc -c < "$T/gci.txt") / 2 )) ]] && ok || bad "gci: >50% smaller" "$(wc -c <<<"$o")"

printf 'Id          : 42\r\nProcessName : pwsh\r\nPath        : C:\\x\\pwsh.exe\r\n\r\nId          : 43\r\nProcessName : node\r\nPath        : C:\\y\\node.exe\r\n' > "$T/fl.txt"
o=$($W --cat pwsh --test-stdin < "$T/fl.txt")
has "Format-List: name padding collapsed" "$o" "ProcessName : node"
has "Format-List: values verbatim" "$o" 'Path : C:\\y\\node\.exe'

python_json='{\n    "name": "refero",\n    "tags": [\n        "a",\n        "b",\n        "c"\n    ],\n    "links": {\n        "self": "https://api.github.com/x",\n        "git": "https://api.github.com/y"\n    },\n    "size": 4096,\n    "items": [\n        {\n            "id": 1,\n            "ok": true\n        },\n        {\n            "id": 2,\n            "ok": false\n        }\n    ]\n}\n'
printf "$python_json" > "$T/j.txt"
o=$($W --cat http --test-stdin < "$T/j.txt")
has "JSON: scalar array on one line" "$o" '"tags": \["a", "b", "c"\],'
has "JSON: re-indented one space per level" "$o" $'\n   "id": 1,'
hasnt "JSON: lossless => no spill" "$o" "full: "
k1=$(tr -d ' \n' < "$T/j.txt"); k2=$(tr -d ' \n' <<<"$o"); [[ "$k1" == "$k2" ]] && ok || bad "JSON: only whitespace changed" "$k2"

# Over Antigravity's native cap: head/tail + every error line, Windows spill path, raw on disk
{ for i in $(seq 1 300); do echo "row $i $(printf 'x%.0s' $(seq 1 60))"; done
  echo 'Traceback (most recent call last):'; echo '  File "C:\app\main.py", line 3, in <module>'; echo 'ValueError: boom in the middle'
  for i in $(seq 301 600); do echo "row $i $(printf 'x%.0s' $(seq 1 60))"; done; } > "$T/big.txt"
o=$($W --cat pwsh --pwsh-test --test-stdin --test-rc 1 < "$T/big.txt"); rc=$?
[[ $rc -eq 1 ]] && ok || bad "test-rc propagated" "$rc"
has "big: error line pinned" "$o" "ValueError: boom in the middle"
has "big: traceback header pinned" "$o" "Traceback \(most recent call last\)"
has "big: last line kept" "$o" "row 600 "
[[ $(wc -c <<<"$o") -lt 6500 ]] && ok || bad "big: byte budget (<6.5 KB vs ~10 KB native preview)" "$(wc -c <<<"$o")"
if [[ $WINHOST -eq 1 ]]; then
  has "big: spill pointer is a Windows path" "$o" 'full: [A-Za-z]:\\[^]]*agy-tokensaver-[^]]*\.log\]'
  sp=$(sed -n 's/.*full: \([^]]*\)\].*/\1/p' <<<"$o"); sp=$(cygpath -u "$sp" 2>/dev/null || echo "$sp")
  [[ -f "$sp" ]] && diff -q "$sp" "$T/big.txt" >/dev/null && ok || bad "big: spill file == full original" "$sp"
fi

# ---------- LIVE: rewritten command lines executed by Windows PowerShell 5.1 ----------
PS=$(command -v powershell.exe 2>/dev/null || true)
if [[ $WINHOST -eq 1 && -n "$PS" ]]; then
  L="$T/live"; mkdir -p "$L/src/sub dir" "$L/Königsberg"
  for i in $(seq 1 15); do echo "x$i" > "$L/src/f$i.ts"; echo y > "$L/src/sub dir/n $i.txt"; done
  echo ü > "$L/Königsberg/Straße.md"
  LW=$(cygpath -w "$L")
  # Run <cmd> the way Antigravity does (UTF-8 prelude + command) and report the exact exit code
  runps() { # $1 = PowerShell text
    local enc
    enc=$(perl -e 'my $s = "try { \$OutputEncoding = [System.Text.UTF8Encoding]::new(\$false); [Console]::OutputEncoding = \$OutputEncoding } catch {}\n" . $ARGV[0] . "\nexit \$LASTEXITCODE"; utf8::decode($s); my $u = pack("v*", map { ord } split //, $s); my $b = ""; $b .= pack("u", substr($u, $_*45, 45)) =~ s/^.//r =~ s/\n$//r for 0 .. int((length($u)-1)/45); $b =~ tr{` -_}{AA-Za-z0-9+/}; my $p = (3 - length($u) % 3) % 3; substr($b, -$p) = "=" x $p if $p; print $b' "$1")
    (cd "$L" && "$PS" -NoLogo -NoProfile -NonInteractive -EncodedCommand "$enc" 2>/dev/null)
  }
  rewrite() { # JSON-safe command -> rewritten command line (unescaped)
    printf '{"toolInput":{"CommandLine":"%s","Cwd":"%s"}}' "$1" "${LW//\\/\\\\}" | perl "$S/cmd_hook.pl" |
      perl -ne 'if (/"CommandLine":"((?:[^"\\]|\\.)*)"/) { my $s = $1; $s =~ s/\\(["\\\/nrt])/$1 eq "n" ? "\n" : $1 eq "r" ? "\r" : $1 eq "t" ? "\t" : $1/ge; print $s }'
  }
  live() { # name, json-safe cmd, expected rc, regex on output
    local r o rc
    r=$(rewrite "$2")
    if [[ -z "$r" || "$r" != *cmd_wrap.pl* ]]; then bad "live $1: not wrapped" "$r"; return; fi
    o=$(runps "$r"); rc=$?
    [[ $rc -eq $3 ]] && ok || bad "live $1: exit code $3" "rc=$rc out=${o:0:300}"
    [[ "$o" =~ $4 ]] && ok || bad "live $1: output" "$o"
  }
  live "gci -Recurse" 'Get-ChildItem -Recurse' 0 "Straße\.md.*f15\.ts|f15\.ts.*Straße\.md"
  o=$(runps "$(rewrite 'Get-ChildItem -Recurse')"); c=$(grep -c '^Mode' <<<"$o")
  [[ $c -eq 1 ]] && ok || bad "live gci: header once" "$c"
  live "Select-Object table survives (formatter flush)" 'Get-ChildItem -Recurse -Filter *.md | Select-Object FullName, Length' 0 "FullName"
  live "quotes + \$_" 'Get-ChildItem -Recurse -Filter *.ts | ForEach-Object { \"$($_.Name)='\''q'\''`\"dq`\"\" } | Sort-Object' 0 "f1\.ts='q'\"dq\""
  live "umlaut command (b64)" 'Get-ChildItem Königsberg | ForEach-Object { $_.Name }' 0 "Straße\.md"
  live "cmdlet error is shown, exit 1" 'Get-ChildItem; Get-Item C:\\\\does\\\\not\\\\exist' 1 "does\\\\not\\\\exist"
  live "cmdlet-only success, exit 0" 'Get-ChildItem src' 0 "f15\.ts"
  if command -v python >/dev/null 2>&1 && python -c 'import sys' >/dev/null 2>&1; then
    live "exit code 0" 'Get-ChildItem src; python -c \"import sys; sys.exit(0)\"' 0 "f15\.ts"
    live "exit code 1" 'Get-ChildItem src; python -c \"import sys; sys.exit(1)\"' 1 "f15\.ts"
    live "native exit code 3 kept" 'Get-ChildItem src; python -c \"import sys; sys.exit(3)\"' 3 "f15\.ts"
    live "python traceback kept" "Get-ChildItem src; python -c \\\"print('start'); 1/0\\\"" 1 "start.*ZeroDivisionError"
    live "python UTF-8 emoji output" "Get-ChildItem src; python -c \\\"print('Grüße 👇')\\\"" 0 "Grüße 👇"
    live "here-string piped to python" "Get-ChildItem src; @'\\nprint('here ok')\\n'@ | python -" 0 "here ok"
  else SKIP=$((SKIP+6)); fi
else
  SKIP=$((SKIP+1)); echo "SKIP: live PowerShell checks (not a Windows host with powershell.exe)"
fi

echo "PASS=$PASS FAIL=$FAIL SKIP=$SKIP"
[[ $FAIL -eq 0 ]]
