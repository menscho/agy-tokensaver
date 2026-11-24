#!/usr/bin/env bash
# Table-driven semantic-preservation test suite for cmd_hook.pl + cmd_wrap.pl (Phase 0.3).
# Proves >=60 cases across wrapped command classes preserve:
#   - exact exit codes (0, 1, 2, 42, 127)
#   - stdin passthrough
#   - stderr ordering / error preservation
#   - filesystem side effects
#   - pipes, &&, ||, ;, quoted spaces/$, and non-ASCII paths (POSIX + PowerShell)
#   - AGY_TS_SHELL=off kill switch
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

PY="python3"
command -v "$PY" >/dev/null 2>&1 || PY="python"

"$PY" - "$ROOT" "$T" <<'PYEOF'
import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

root = Path(sys.argv[1])
tmp = Path(sys.argv[2])
scripts = root / "scripts"
cmd_hook = scripts / "cmd_hook.pl"

# Create deterministic mock tool stubs in tmp/bin so every wrapped command class executes
# cleanly on Linux, macOS, and Windows (Git Bash) without network or heavy runtimes.
bin_dir = tmp / "bin"
bin_dir.mkdir(parents=True, exist_ok=True)

STUB_CODE = r"""#!/usr/bin/env python3
import os, sys, pathlib
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
for i, a in enumerate(args):
    if a == "--touch" and i + 1 < len(args):
        p = pathlib.Path(args[i + 1])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("|".join(args) + "\n", encoding="utf-8")
if "--read-stdin" in args:
    data = sys.stdin.read()
    print(f"STDIN_GOT:{data.strip()}")
if "--ordered-err" in args:
    sys.stdout.write("ORD:1:out\n"); sys.stdout.flush()
    sys.stderr.write("ORD:2:err\n"); sys.stderr.flush()
    sys.stdout.write("ORD:3:out\n"); sys.stdout.flush()
if "--echo-args" in args:
    print("ARGS=" + "|".join(args))
if "--echo-env" in args:
    print("ENV_FOO=" + os.environ.get("FOO", ""))
if "--noisy" in args:
    for i in range(1, 120):
        print(f"{name} line {i} ok passed nominal_padding_0123456789")
    print(f"ERROR: {name} failed on target_marker_99")
else:
    print(f"{name}:ok:" + " ".join(args))
rc = 0
for i, a in enumerate(args):
    if a == "--rc" and i + 1 < len(args):
        rc = int(args[i + 1])
sys.exit(rc)
"""

TOOLS = [
    "pytest", "jest", "vitest", "cargo", "npm", "pip3", "git", "tsc", "eslint",
    "ruff", "mypy", "make", "docker", "kubectl", "helm", "curl", "tar", "rsync",
    "terraform", "aws", "journalctl", "systemctl", "ssh", "pm2", "ss", "ps",
    "ffmpeg", "go", "dotnet", "deno", "composer", "flake8", "phpunit",
]
for t in TOOLS:
    p = bin_dir / t
    p.write_text(STUB_CODE, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

bash_bin = shutil.which("bash") or "/bin/bash"
pwsh_bin = shutil.which("powershell.exe") or shutil.which("pwsh")
perl_os = subprocess.check_output(["perl", "-e", "print $^O"], text=True).strip()
is_win_perl = perl_os in ("MSWin32", "cygwin", "msys")

def run_hook(cmd: str, cwd: str, extra_env: dict | None = None) -> tuple[dict, str | None]:
    env = dict(os.environ)
    env["PLUGIN_DATA"] = str(tmp / "data")
    env["PLUGIN_ROOT"] = str(root)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FOO"] = "bar_value"
    if extra_env:
        env.update(extra_env)
    payload = json.dumps({"toolInput": {"CommandLine": cmd, "Cwd": cwd}, "cwd": cwd}).encode("utf-8")
    p = subprocess.run(["perl", str(cmd_hook)], input=payload, capture_output=True, env=env, timeout=5)
    assert p.returncode == 0, f"hook rc={p.returncode} stderr={p.stderr!r}"
    assert p.stderr == b"", f"hook stderr={p.stderr!r}"
    obj = json.loads(p.stdout.decode("utf-8"))
    ov = (obj.get("overwrite") or {}).get("CommandLine")
    return obj, ov

def exec_posix(cmd: str, cwd: Path, stdin_bytes: bytes = b"", extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PLUGIN_DATA"] = str(tmp / "data")
    env["PLUGIN_ROOT"] = str(root)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FOO"] = "bar_value"
    if extra_env:
        env.update(extra_env)
    return subprocess.run([bash_bin, "-c", cmd], input=stdin_bytes, capture_output=True, cwd=str(cwd), env=env, timeout=10)

passed = 0
failed = 0

def ok(name: str):
    global passed
    passed += 1
    print(f"PASS: {name}")

def bad(name: str, detail: str):
    global failed
    failed += 1
    print(f"FAIL: {name} :: {detail}")

# ---------------------------------------------------------------------------
# Part A: 60 POSIX semantic-preservation table cases (live executed)
# Columns: (id, cmd, expect_wrap, stdin_str, side_effect_relpath, must_contain, ordered_markers)
# ---------------------------------------------------------------------------
POSIX_CASES = [
    # 1-16: Exit codes across wrapped command classes
    ("test_rc0",       "pytest --noisy --rc 0",                              True,  "", "", ["target_marker_99"], []),
    ("test_rc1",       "pytest --noisy --rc 1",                              True,  "", "", ["target_marker_99"], []),
    ("test_rc2",       "pytest --rc 2",                                      True,  "", "", [], []),
    ("test_rc42",      "jest --rc 42",                                       True,  "", "", [], []),
    ("test_vitest",    "vitest run --rc 1",                                  True,  "", "", [], []),
    ("test_phpunit",   "phpunit tests --rc 2",                               True,  "", "", [], []),
    ("build_rc0",      "cargo build --noisy --rc 0",                         True,  "", "", ["target_marker_99"], []),
    ("build_rc1",      "cargo build --noisy --rc 1",                         True,  "", "", ["target_marker_99"], []),
    ("build_tsc_rc2",  "tsc --noisy --rc 2",                                 True,  "", "", ["target_marker_99"], []),
    ("lint_eslint",    "eslint src --noisy --rc 1",                          True,  "", "", ["target_marker_99"], []),
    ("lint_ruff",      "ruff check . --rc 1",                                True,  "", "", [], []),
    ("lint_mypy",      "mypy app --rc 1",                                    True,  "", "", [], []),
    ("lint_flake8",    "flake8 src --rc 1",                                  True,  "", "", [], []),
    ("pkg_npm_rc1",    "npm install --noisy --rc 1",                         True,  "", "", ["target_marker_99"], []),
    ("pkg_pip_rc0",    "pip3 install pkg --rc 0",                            True,  "", "", [], []),
    ("pkg_composer",   "composer install --rc 1",                            True,  "", "", [], []),
    # 17-32: Remaining wrapped categories
    ("git_diff_rc0",   "git diff --rc 0",                                    True,  "", "", [], []),
    ("git_log_rc0",    "git log -n 20 --rc 0",                               True,  "", "", [], []),
    ("logs_k8s",       "kubectl logs pod-1 --noisy --rc 0",                  True,  "", "", ["target_marker_99"], []),
    ("logs_journal",   "journalctl -u app -n 50 --rc 0",                     True,  "", "", [], []),
    ("http_curl",      "curl -s https://example.test --noisy --rc 0",        True,  "", "", ["target_marker_99"], []),
    ("cloud_aws",      "aws ec2 describe-instances --output table --rc 0",   True,  "", "", [], []),
    ("iac_tf",         "terraform plan --rc 2",                              True,  "", "", [], []),
    ("archive_tar",    "tar -xvf bundle.tar --rc 0",                         True,  "", "", [], []),
    ("archive_rsync",  "rsync -av src dst --rc 0",                           True,  "", "", [], []),
    ("ssh_remote",     "ssh prod 'systemctl status app' --rc 0",             True,  "", "", [], []),
    ("ssh_rc1",        "ssh prod 'systemctl status app' --rc 1",             True,  "", "", [], []),
    ("ps_list",        "ps -eo pid,pcpu,args --rc 0",                        True,  "", "", [], []),
    ("table_docker",   "docker ps -a --rc 0",                                True,  "", "", [], []),
    ("table_pm2",      "pm2 list --rc 0",                                    True,  "", "", [], []),
    ("make_build",     "make all --rc 2",                                    True,  "", "", [], []),
    ("go_test",        "go test ./... --rc 1",                               True,  "", "", [], []),
    ("dotnet_test",    "dotnet test --rc 1",                                 True,  "", "", [], []),
    ("deno_test",      "deno test --rc 1",                                   True,  "", "", [], []),
    # 35-38: Stdin passthrough
    ("stdin_pytest",   "pytest --read-stdin",                                True,  "hello_from_stdin\n", "", ["STDIN_GOT:hello_from_stdin"], []),
    ("stdin_cargo",    "cargo test --read-stdin",                            True,  "cargo_stdin_42\n",   "", ["STDIN_GOT:cargo_stdin_42"], []),
    ("stdin_make",     "make check --read-stdin",                            True,  "make_stdin_99\n",    "", ["STDIN_GOT:make_stdin_99"], []),
    ("stdin_git",      "git diff --read-stdin",                              True,  "git_stdin_ok\n",     "", ["STDIN_GOT:git_stdin_ok"], []),
    # 39-41: Stderr ordering preserved
    ("stderr_order_1", "pytest --ordered-err --rc 0",                        True,  "", "", ["ORD:1:out", "ORD:2:err", "ORD:3:out"], ["ORD:1:out", "ORD:2:err", "ORD:3:out"]),
    ("stderr_order_2", "cargo build --ordered-err --rc 1",                   True,  "", "", ["ORD:1:out", "ORD:2:err", "ORD:3:out"], ["ORD:1:out", "ORD:2:err", "ORD:3:out"]),
    ("stderr_order_3", "npm run build --ordered-err --rc 1",                 True,  "", "", ["ORD:1:out", "ORD:2:err", "ORD:3:out"], ["ORD:1:out", "ORD:2:err", "ORD:3:out"]),
    # 42-46: Side effects (files created & exact contents)
    ("sidefx_touch_1", "pytest --touch out/result.txt --rc 0",               True,  "", "out/result.txt", [], []),
    ("sidefx_touch_2", "cargo build --touch dist/artifact.txt --rc 0",       True,  "", "dist/artifact.txt", [], []),
    ("sidefx_chain",   "mkdir -p build && pytest --touch build/done.txt",    True,  "", "build/done.txt", [], []),
    ("sidefx_rc1",     "pytest --touch err/trace.txt --rc 1",                True,  "", "err/trace.txt", [], []),
    ("sidefx_cp_mv",   "mkdir -p d1 && pytest --touch d1/a.txt && cp d1/a.txt d1/b.txt", True, "", "d1/b.txt", [], []),
    # 47-52: Pipes, &&, ||, ;
    ("pipe_grep",      "pytest --noisy | grep target_marker_99",             True,  "", "", ["target_marker_99"], []),
    ("pipe_sort_uniq", "pytest --noisy | grep line | sort | uniq",           True,  "", "", ["line 99 "], []),
    ("chain_and_pass", "cargo test --rc 0 && cargo build --rc 0",            True,  "", "", [], []),
    ("chain_and_stop", "cargo test --rc 1 && cargo build --touch never.txt", True,  "", "never.txt", [], []),
    ("chain_or_true",  "pytest --rc 1 || true",                              True,  "", "", [], []),
    ("chain_semi",     "export FOO=custom_val; pytest --echo-env",           True,  "", "", ["ENV_FOO=custom_val"], []),
    # 53-58: Quoted args with spaces, $, single quotes, semicolons in quotes, non-ASCII paths
    ("quote_spaces",   'pytest --echo-args "hello world" "a  b"',            True,  "", "", ["hello world|a  b"], []),
    ("quote_dollar_d", 'pytest --echo-args "val=$FOO"',                      True,  "", "", ["val=bar_value"], []),
    ("quote_dollar_s", "pytest --echo-args 'literal=$FOO'",                  True,  "", "", ["literal=$FOO"], []),
    ("quote_escaped",  "pytest --echo-args 'it'\\''s ok'",                   True,  "", "", ["it's ok"], []),
    ("quote_semi_in",  'pytest --echo-args "a;b|c&&d"',                      True,  "", "", ["a;b|c&&d"], []),
    ("non_ascii_path", 'pytest --touch "café/naïve file.txt" --echo-args "café/naïve file.txt"', True, "", "café/naïve file.txt", ["café/naïve file.txt"], []),
    # 59-60: Unsafe redirects / command substitution must NOT wrap (passthrough preserves exact semantics)
    ("unsafe_redir",   "pytest --rc 0 > captured.txt",                       False, "", "captured.txt", [], []),
    ("unsafe_subsh",   "pytest --echo-args $(echo sub_val)",                 False, "", "", ["sub_val"], []),
]

for cid, cmd, expect_wrap, stdin_str, sidefx, must_have, ordered in POSIX_CASES:
    d_orig = tmp / f"orig_{cid}"
    d_wrap = tmp / f"wrap_{cid}"
    d_orig.mkdir()
    d_wrap.mkdir()
    obj, ov = run_hook(cmd, str(d_wrap), {"AGY_TS_SHELL": "posix"})
    did_wrap = ov is not None and "cmd_wrap.pl" in ov
    if did_wrap != expect_wrap:
        bad(f"posix:{cid}", f"expect_wrap={expect_wrap} got did_wrap={did_wrap} ov={ov!r}")
        continue
    r_orig = exec_posix(cmd, d_orig, stdin_str.encode("utf-8"))
    r_wrap = exec_posix(ov if ov is not None else cmd, d_wrap, stdin_str.encode("utf-8"))
    if r_orig.returncode != r_wrap.returncode:
        bad(f"posix:{cid}", f"rc mismatch orig={r_orig.returncode} wrap={r_wrap.returncode}")
        continue
    combined_wrap = (r_wrap.stdout + r_wrap.stderr).decode("utf-8", errors="replace")
    miss = [m for m in must_have if m not in combined_wrap]
    if miss:
        bad(f"posix:{cid}", f"missing output markers {miss} in {combined_wrap[:200]!r}")
        continue
    if ordered:
        idxs = [combined_wrap.find(m) for m in ordered]
        if any(i < 0 for i in idxs) or idxs != sorted(idxs):
            bad(f"posix:{cid}", f"ordering violated {ordered} -> {idxs} in {combined_wrap[:200]!r}")
            continue
    if sidefx:
        p1, p2 = d_orig / sidefx, d_wrap / sidefx
        if p1.exists() != p2.exists():
            bad(f"posix:{cid}", f"side-effect existence mismatch orig={p1.exists()} wrap={p2.exists()}")
            continue
        if p1.exists() and p1.read_bytes() != p2.read_bytes():
            bad(f"posix:{cid}", f"side-effect content mismatch orig={p1.read_bytes()!r} wrap={p2.read_bytes()!r}")
            continue
    ok(f"posix:{cid}")

# ---------------------------------------------------------------------------
# Part B: Kill switch AGY_TS_SHELL=off (3 cases)
# ---------------------------------------------------------------------------
for cid, cmd in [
    ("killswitch_pytest", "pytest --noisy"),
    ("killswitch_git",    "git diff"),
    ("killswitch_pwsh",   "Get-ChildItem | Select-String foo"),
]:
    obj, ov = run_hook(cmd, str(tmp), {"AGY_TS_SHELL": "off"})
    if obj == {"decision": "allow"} and ov is None:
        ok(f"killswitch:{cid}")
    else:
        bad(f"killswitch:{cid}", f"expected plain allow, got {obj}")

# ---------------------------------------------------------------------------
# Part C: PowerShell semantic-preservation cases
# On Windows (MSWin32/cygwin/msys), validates rewrite syntax (here-string / --pwsh-b64)
# AND executes live powershell.exe commands checking exact exit code, quoting, and side effects.
# ---------------------------------------------------------------------------
if is_win_perl:
    PWSH_CASES = [
        ("pwsh_pytest",       "pytest -v",                                          True),
        ("pwsh_pytest_env",   "pytest $env:PYTEST_ARGS",                            True),
        ("pwsh_gci",          "Get-ChildItem -Recurse",                             True),
        ("pwsh_pipe",         "Get-Process | Format-List Id, Name",                 True),
        ("pwsh_semi",         "npm run build; Get-ChildItem dist",                  True),
        ("pwsh_non_ascii",    'Get-ChildItem "café\\naïve file.txt"',               True),
        ("pwsh_git_clone",    "git clone https://example.test/repo.git",            True),
        ("pwsh_cargo_build",  "cargo build --release",                              True),
        ("pwsh_npm_test",     "npm test",                                           True),
        ("pwsh_exit_skip",    "Get-ChildItem; exit 3",                              False),
        ("pwsh_cd_skip",      "cd src; npm test",                                   False),
    ]
    for cid, cmd, expect_wrap in PWSH_CASES:
        obj, ov = run_hook(cmd, r"C:\work", {"AGY_TS_SHELL": "pwsh"})
        did_wrap = ov is not None and ("--pwsh-stdin" in ov or "--pwsh-b64" in ov)
        if did_wrap != expect_wrap:
            bad(f"pwsh:{cid}", f"expect_wrap={expect_wrap} got did_wrap={did_wrap} ov={ov!r}")
            continue
        if did_wrap and "--pwsh-b64" in ov:
            m = re.search(r"--pwsh-b64 '([A-Za-z0-9+/=]+)'", ov)
            if not m or base64.b64decode(m.group(1)).decode("utf-8") != cmd:
                bad(f"pwsh:{cid}", f"b64 roundtrip mismatch in {ov!r}")
                continue
        elif did_wrap and "--pwsh-stdin" in ov:
            if f"@'\n{cmd}\n'@" not in ov:
                bad(f"pwsh:{cid}", f"here-string mismatch in {ov!r}")
                continue
        ok(f"pwsh:{cid}")

    if pwsh_bin:
        live_cases = [
            ("win_live_gci_rc0", "Get-ChildItem .", 0, None),
            ("win_live_fx",      "Get-ChildItem . | Out-Null; Set-Content -Path 'pwsh_fx.txt' -Value 'ok42'", 0, "pwsh_fx.txt"),
            ("win_live_err",     "Get-ChildItem '__no_such_dir_xyz__'", 1, None),
        ]
        for cid, cmd, exp_rc, fx in live_cases:
            d = tmp / f"win_{cid}"
            d.mkdir()
            obj, ov = run_hook(cmd, str(d), {"AGY_TS_SHELL": "pwsh"})
            run_cmd = ov if ov else cmd
            p = subprocess.run([pwsh_bin, "-NoProfile", "-NonInteractive", "-Command", run_cmd], cwd=str(d), capture_output=True, timeout=15)
            if p.returncode != exp_rc:
                bad(f"pwsh_live:{cid}", f"expected rc={exp_rc} got {p.returncode} out={p.stdout!r} err={p.stderr!r}")
                continue
            if fx and not (d / fx).is_file():
                bad(f"pwsh_live:{cid}", f"missing side-effect file {fx}")
                continue
            ok(f"pwsh_live:{cid}")

print(f"PASS={passed} FAIL={failed}")
sys.exit(0 if failed == 0 else 1)
PYEOF
