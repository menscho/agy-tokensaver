#!/usr/bin/env bash
# Spill-file safety, permissions, retention, purge, and byte-for-byte lossless tests (Phase 0.4).
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

PY="python3"
command -v "$PY" >/dev/null 2>&1 || PY="python"

"$PY" - "$ROOT" "$T" <<'PYEOF'
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
tmp = Path(sys.argv[2])
wrap = root / "scripts" / "cmd_wrap.pl"
inspect_py = root / "scripts" / "ts_inspect.py"
spill_dir = tmp / "spills"

env = dict(os.environ)
env["PLUGIN_DATA"] = str(tmp / "data")
env["AGY_TS_SPILL_DIR"] = str(spill_dir)

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

def run_wrap(cat: str, raw_bytes: bytes, rc: int = 0, extra_env: dict | None = None) -> bytes:
    e = dict(env)
    if extra_env:
        e.update(extra_env)
    p = subprocess.run(
        ["perl", str(wrap), "--cat", cat, "--test-stdin", "--test-rc", str(rc)],
        input=raw_bytes,
        capture_output=True,
        env=e,
        timeout=10,
    )
    assert p.returncode == rc, f"cmd_wrap expected rc={rc} got rc={p.returncode} err={p.stderr!r}"
    return p.stdout

# 1. Permissions test (0700 dir, 0600 file on Unix)
big_log = b"".join(
    f"\x1b[32m2026-11-24T12:00:{i%60:02d}Z\x1b[0m INFO worker tick={i} payload_padding_0123456789abcdef  \r\n".encode()
    for i in range(1, 500)
) + b"\x1b[31mERROR\x1b[0m fatal crash code=E_PERM_TEST\r\n"

out = run_wrap("logs", big_log)
m = re.search(rb"full: ([^\]]+)\]", out)
if not m:
    bad("spill_created", f"no spill path in {out[-200:]!r}")
    sys.exit(1)

sp = Path(m.group(1).decode("utf-8"))
if os.name != "nt":
    dir_mode = stat.S_IMODE(spill_dir.stat().st_mode)
    file_mode = stat.S_IMODE(sp.stat().st_mode)
    if dir_mode == 0o700:
        ok(f"perms: spill dir mode is 0700")
    else:
        bad("perms: spill dir mode", f"expected 0700 got {oct(dir_mode)}")
    if file_mode == 0o600:
        ok(f"perms: spill file mode is 0600")
    else:
        bad("perms: spill file mode", f"expected 0600 got {oct(file_mode)}")
else:
    ok("perms: spill dir/file created on Windows")

# 2. Byte-for-byte lossless check on ANSI + CRLF + trailing spaces
if sp.read_bytes() == big_log:
    ok("lossless: logs spill equals byte-exact original (incl. ANSI + CRLF + trailing spaces)")
else:
    bad("lossless: logs spill mismatch", f"orig={len(big_log)}B spill={len(sp.read_bytes())}B")

# 3. Symlink safety: symlink inside spill dir is never overwritten
if os.name != "nt":
    victim = tmp / "victim.txt"
    victim.write_text("DO_NOT_TOUCH", encoding="utf-8")
    # Pre-plant symlink at the deterministic spill filename for a known payload
    payload_sym = b"".join(f"INFO symtest line {i} padding_padding_padding_0123456789\n".encode() for i in range(450))
    # First get its deterministic hash name in a separate dir
    sd2 = tmp / "spills2"
    out2 = run_wrap("logs", payload_sym, extra_env={"AGY_TS_SPILL_DIR": str(sd2)})
    m2 = re.search(rb"full: ([^\]]+)\]", out2)
    if m2:
        fname = Path(m2.group(1).decode()).name
        sym_target = spill_dir / fname
        if sym_target.exists():
            sym_target.unlink()
        sym_target.symlink_to(victim)
        _ = run_wrap("logs", payload_sym)
        if victim.read_text(encoding="utf-8") == "DO_NOT_TOUCH":
            ok("symlink: spill write refuses symlink target")
        else:
            bad("symlink: spill overwrote symlink target", victim.read_text(encoding="utf-8"))
        sym_target.unlink()
    else:
        bad("symlink: could not obtain spill filename", repr(out2[-200:]))

# 4. Retention policy: default 7 days keeps 3d old, deletes 9d old; custom AGY_TS_SPILL_DAYS=2 deletes 3d old
now = time.time()
f_old9 = spill_dir / "old9d.log"
f_old3 = spill_dir / "old3d.log"
f_old9.write_bytes(b"old9")
f_old3.write_bytes(b"old3")
os.utime(f_old9, (now - 9 * 86400, now - 9 * 86400))
os.utime(f_old3, (now - 3 * 86400, now - 3 * 86400))

_ = run_wrap("logs", big_log, extra_env={"AGY_TS_SPILL_CLEANUP": "1", "AGY_TS_SPILL_DAYS": "7"})
if not f_old9.exists() and f_old3.exists():
    ok("retention: default 7d deletes 9d file and keeps 3d file")
else:
    bad("retention: default 7d", f"old9={f_old9.exists()} old3={f_old3.exists()}")

_ = run_wrap("logs", big_log, extra_env={"AGY_TS_SPILL_CLEANUP": "1", "AGY_TS_SPILL_DAYS": "2"})
if not f_old3.exists():
    ok("retention: AGY_TS_SPILL_DAYS=2 deletes 3d file")
else:
    bad("retention: AGY_TS_SPILL_DAYS=2", "old3 still exists")

# 5. ts_inspect.py purge (--days N and full purge)
f_keep = spill_dir / "keep_fresh.log"
f_stale = spill_dir / "stale_10d.log"
f_keep.write_bytes(b"fresh")
f_stale.write_bytes(b"stale")
os.utime(f_stale, (now - 10 * 86400, now - 10 * 86400))

p1 = subprocess.run([sys.executable, str(inspect_py), "purge", "--days", "7"], env=env, capture_output=True, text=True)
if p1.returncode == 0 and not f_stale.exists() and f_keep.exists():
    ok("purge: ts_inspect.py purge --days 7 removes stale and keeps fresh")
else:
    bad("purge: --days 7", f"rc={p1.returncode} out={p1.stdout!r} stale={f_stale.exists()} keep={f_keep.exists()}")

p2 = subprocess.run([sys.executable, str(inspect_py), "purge"], env=env, capture_output=True, text=True)
rem = [x for x in spill_dir.iterdir() if x.is_file()]
if p2.returncode == 0 and len(rem) == 0:
    ok("purge: ts_inspect.py purge clears all remaining spill files")
else:
    bad("purge: full", f"rc={p2.returncode} remaining={rem}")

# 6. Byte-for-byte spill equality across EVERY compressor category in %DISPATCH
def make_input(cat: str) -> tuple[bytes, int]:
    if cat == "git_diff":
        body = [b"diff --git a/package-lock.json b/package-lock.json\r\n", b"--- a/package-lock.json\r\n", b"+++ b/package-lock.json\r\n"]
        body += [f"+  \"dep_{i}\": \"1.0.{i}\",\r\n".encode() for i in range(400)]
        body += [b"diff --git a/src/app.py b/src/app.py\r\n", b"--- a/src/app.py\r\n", b"+++ b/src/app.py\r\n", b"@@ -1,2 +1,2 @@\r\n", b"-old()\r\n", b"+new()\r\n"]
        return b"".join(body), 0
    if cat == "http":
        rows = [f'{{"id":{i},"status":"ok","msg":"event_payload_{i}_padding_0123456789abcdef"}}' for i in range(250)]
        return ("[" + ",".join(rows) + "]\n").encode(), 0
    if cat == "search":
        return b"".join(f"/srv/app/deep/nested/routes/handler_{i%5}.py:{i}:  match_token_xyz_{i} padding_0123456789\n".encode() for i in range(450)), 0
    if cat == "find":
        return b"".join(f"./packages/service-{i%10}/src/components/widget_{i}.tsx\n".encode() for i in range(500)), 0
    if cat == "test":
        return b"".join(f"tests/test_mod.py::test_case_{i} PASSED\n".encode() for i in range(400)) + b"FAILED tests/test_mod.py::test_boom - AssertionError: 1 != 2\n", 1
    if cat == "build":
        return b"".join(f"src/app.ts:{i}:5 - error TS2322: Type 'string' is not assignable to type 'number'.\n".encode() for i in range(250)) + b"Found 250 errors.\n", 1
    if cat == "lint":
        return b"".join(f"src/file_{i%5}.py:{i}:1: E501 line too long (120 > 88 characters) padding_012345\n".encode() for i in range(300)), 1
    if cat == "pkg":
        return b"".join(f"Downloading package-{i}-1.2.3.whl (120 kB)\r\nInstalling collected package-{i}\r\n".encode() for i in range(250)), 0
    if cat == "git_log":
        return b"".join(f"commit {i:040x}\nAuthor: Dev <dev@example.test>\nDate: Tue Nov 24 2026\n\n    commit message {i} with extra padding text\n\n".encode() for i in range(150)), 0
    if cat == "git_net":
        return b"".join(f"remote: Compressing objects: {i%100}% ({i}/400)\r\n".encode() for i in range(400)) + b"From https://example.test/repo\n * branch main -> FETCH_HEAD\n", 0
    if cat == "archive":
        return b"".join(f"x dist/assets/chunk_{i:04d}_verylong_archive_entry_name.js\n".encode() for i in range(500)), 0
    if cat == "iac":
        return b"".join(f"  # aws_s3_bucket.b_{i} will be updated in-place\n  ~ resource \"aws_s3_bucket\" \"b_{i}\" {{\n      id = \"bucket-{i}\"\n    }}\n".encode() for i in range(200)), 0
    if cat == "k8s_yaml":
        return b"apiVersion: v1\nkind: Pod\nmetadata:\n  name: app\n  managedFields:\n" + b"".join(f"    - field_{i}: value_{i}_padding_0123456789\n".encode() for i in range(400)), 0
    if cat == "ps":
        return b"PID   %CPU COMMAND\n" + b"".join(f"{1000+i}  0.1 /usr/bin/worker --id={i} --config=/etc/app/worker_{i}.conf\n".encode() for i in range(400)), 0
    if cat in ("table", "cloud", "pwsh", "script"):
        hdr = b"NAME                 STATUS    AGE    NODE                          PADDING\r\n"
        return hdr + b"".join(f"pod-{i:04d}             Running   12d    worker-node-{i%8:02d}.cluster.local   extra_{i}\r\n".encode() for i in range(400)), 0
    # logs, ssh, cat_large, chain, generic
    return b"".join(f"\x1b[36mINFO\x1b[0m line {i} payload_0123456789abcdef_padding_text  \r\n".encode() for i in range(500)) + b"ERROR marker_end\r\n", 0

CATEGORIES = [
    "test", "pkg", "build", "lint", "git_diff", "git_log", "git_net",
    "search", "find", "cat_large", "http", "archive", "iac", "k8s_yaml",
    "logs", "ssh", "ps", "table", "cloud", "chain", "pwsh", "script",
]

for cat in CATEGORIES:
    raw_in, rc = make_input(cat)
    out_c = run_wrap(cat, raw_in, rc=rc)
    mc = re.search(rb"full: ([^\]]+)\]", out_c)
    if not mc:
        # If lossless transform didn't drop anything, verify output preserved or had no lossy drop
        if out_c == raw_in or b"full:" not in out_c:
            # Verify it was genuinely lossless (no omitted marker without spill)
            if b"omitted" in out_c or b"more lines" in out_c or b"collapsed" in out_c:
                bad(f"lossless_cat:{cat}", "dropped content without spill path!")
            else:
                ok(f"lossless_cat:{cat} (lossless in-place, no drop)")
            continue
    sp_c = Path(mc.group(1).decode("utf-8"))
    if sp_c.is_file() and sp_c.read_bytes() == raw_in:
        ok(f"lossless_cat:{cat} (spill == byte-exact original)")
    else:
        bad(f"lossless_cat:{cat}", f"spill mismatch exists={sp_c.is_file()} orig={len(raw_in)}B")

print(f"PASS={passed} FAIL={failed}")
sys.exit(0 if failed == 0 else 1)
PYEOF
