#!/usr/bin/env bash
# Fail-open fuzz suite for every hook in hooks.json (Phase 0.2).
# Feeds random bytes, truncated JSON, wrong types, missing fields, very long strings,
# deep nesting, and binary data to every hook and asserts rc==0, valid JSON, allow/empty,
# no stderr warnings, and completion within hooks.json timeout.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

PY="python3"
command -v "$PY" >/dev/null 2>&1 || PY="python"

"$PY" - "$ROOT" "$T" <<'PYEOF'
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
tmp = Path(sys.argv[2])
scripts = root / "scripts"
hooks_cfg = json.loads((root / "hooks.json").read_text(encoding="utf-8"))

# Discover every hook script and its timeout directly from hooks.json
hook_specs = []
for name, events in hooks_cfg.items():
    for ev_name, matchers in events.items():
        for m in matchers:
            hlist = m.get("hooks") if isinstance(m.get("hooks"), list) else [m]
            for h in hlist:
                cmd = h.get("command", "")
                parts = cmd.split()
                if len(parts) >= 2 and parts[0] == "perl":
                    script = (root / parts[1]).resolve()
                    timeout = float(h.get("timeout", 5))
                    hook_specs.append((ev_name, script, timeout))

assert len(hook_specs) >= 10, f"expected >=10 hooks in hooks.json, got {len(hook_specs)}"

rng = random.Random(20261124)
cases: list[tuple[str, bytes]] = [
    ("empty", b""),
    ("whitespace", b"   \n\t\r\n "),
    ("truncated_open", b'{"toolInput":{"Path":"/tmp/a'),
    ("truncated_escape", b'{"toolInput":{"CommandLine":"pytest \\'),
    ("truncated_array", b'{"toolInput":{"Excludes":["a","b"'),
    ("non_object_array", b"[1, 2, 3]"),
    ("non_object_string", b'"pytest"'),
    ("non_object_null", b"null"),
    ("missing_fields_empty_obj", b"{}"),
    ("missing_fields_empty_ti", b'{"toolInput":{}}'),
    ("null_tool_input", b'{"toolInput":null,"conversationId":null,"stepIdx":null}'),
    ("wrong_types_scalars", json.dumps({
        "toolInput": {
            "Path": 123,
            "AbsolutePath": True,
            "TargetFile": ["a"],
            "CommandLine": 456,
            "command": {"nested": "pytest"},
            "StartLine": "not-an-int",
            "EndLine": [],
            "ByteOffset": "99",
            "Overwrite": "true",
            "Append": 1,
            "IsDaemon": "true",
            "SearchDirectory": 999,
            "SearchPath": False,
            "Query": {"q": 1},
            "Excludes": "node_modules",
            "action": 42,
            "brief": ["x"],
            "since": {"n": 1},
        },
        "conversationId": 99999,
        "stepIdx": "abc",
        "invocationNum": "ten",
        "transcriptPath": 12345,
        "cwd": ["/" ],
    }).encode("utf-8")),
    ("deep_nesting_120", (b'{"a":' * 120) + b"1" + (b"}" * 120)),
    ("binary_nul_and_invalid_utf8", b'{"toolInput":{"Path":"\x00\xff\xfe\x80\x81","CommandLine":"pytest \x00\xff"}}'),
    ("raw_binary_256b", bytes(range(256))),
    ("random_bytes_1k", bytes(rng.getrandbits(8) for _ in range(1024))),
    ("random_bytes_8k", bytes(rng.getrandbits(8) for _ in range(8192))),
    ("very_long_string_512k", b'{"toolInput":{"Path":"/' + (b"x" * (512 * 1024)) + b'","CommandLine":"' + (b"y" * (128 * 1024)) + b'"}}'),
    ("oversized_3mb", b'{"toolInput":{"Path":"/tmp/a","Pad":"' + (b"z" * (3 * 1024 * 1024)) + b'"}}'),
]

env = dict(os.environ)
env["PLUGIN_DATA"] = str(tmp / "data")
env["PLUGIN_ROOT"] = str(root)

passed = 0
failed = 0

for ev_name, script, timeout in hook_specs:
    for cname, payload in cases:
        t0 = time.perf_counter()
        try:
            p = subprocess.run(
                ["perl", str(script)],
                input=payload,
                capture_output=True,
                timeout=timeout,
                env=env,
            )
            elapsed = time.perf_counter() - t0
        except subprocess.TimeoutExpired:
            failed += 1
            print(f"FAIL: {script.name} [{cname}] :: timed out after {timeout}s")
            continue

        out = p.stdout.decode("utf-8", errors="replace").strip()
        err = p.stderr.decode("utf-8", errors="replace").strip()
        try:
            parsed = json.loads(out)
        except Exception as e:
            failed += 1
            print(f"FAIL: {script.name} [{cname}] :: invalid JSON ({e}) rc={p.returncode} out={out[:120]!r} err={err[:120]!r}")
            continue

        ok = (
            p.returncode == 0
            and err == ""
            and isinstance(parsed, dict)
            and elapsed < timeout
        )
        if ev_name == "PreToolUse":
            ok = ok and parsed.get("decision") == "allow"
        else:
            ok = ok and parsed == {}

        if ok:
            passed += 1
        else:
            failed += 1
            print(f"FAIL: {script.name} [{cname}] :: rc={p.returncode} elapsed={elapsed:.3f}s err={err[:120]!r} out={out[:120]!r}")

print(f"PASS={passed} FAIL={failed} ({len(hook_specs)} hooks x {len(cases)} fuzz cases)")
sys.exit(0 if failed == 0 else 1)
PYEOF
