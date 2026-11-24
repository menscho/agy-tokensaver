#!/usr/bin/env bash
# Auto-Rollover regression suite: shared causal policy, faithful counterfactual, db token fallback,
# IDE+CLI discovery, settled-run gating, handoff packet. Usage: bash tests/test_rollover.sh (exit 0 = pass)
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export PLUGIN_DATA="$T/pdata"; mkdir -p "$PLUGIN_DATA"      # never touch real plugin data
export AGY_TS_GEMINI_HOME="$T/gemini"                       # fake ~/.gemini (real one is never read)
PY=python3; command -v python3 >/dev/null 2>&1 && python3 -c 'import sys' 2>/dev/null || PY=python
PASS=0; FAIL=0

check() {  # check <name> <python body>  (body runs with `core` imported; assert to fail)
  local name="$1"; shift
  local out
  out=$("$PY" - "$ROOT" "$T" 2>&1 <<PYEOF
import sys, os, json, sqlite3, subprocess
from pathlib import Path
sys.path.insert(0, sys.argv[1] + "/mcp_app"); sys.path.insert(0, sys.argv[1] + "/scripts")
import core
ROOT, T = Path(sys.argv[1]), Path(sys.argv[2])
def q(turns, r): return r[0] + 0.05 * r[1] + 6 * sum(t[2] for t in turns)
def q0(turns): return sum(a + 0.05 * b + 6 * c for a, b, c in turns)
def grow(n, start=15000, d=6000, out=300):
    t, ctx = [(start, 0, out)], start
    for _ in range(n - 1):
        t.append((d, ctx, out)); ctx += d
    return t
$*
PYEOF
)
  if [[ $? -eq 0 ]]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); printf 'FAIL: %s\n%s\n' "$name" "$out"; fi
}

# ---------- counterfactual simulation ----------
check "below cap: sim == actual exactly (auto + manual)" '
t = [(15000, 0, 200), (2000, 15000, 200), (1800, 17000, 200), (2200, 18800, 200)]
for cap in (None, 110_000):
    si, sc, ro = core._simulate_rollover_session(t, cap)
    assert ro == 0 and si == sum(x[0] for x in t) and sc == sum(x[1] for x in t), (cap, si, sc, ro)
'
check "legacy call signature still allows regressions (never clamped)" '
t = [(20000, 0, 100)] + [(1000, 115000, 100)] * 14
si, sc, ro = core._simulate_rollover_session(t, cap=110_000, rediscovery_tax=100_000)
assert ro >= 1 and q(t, (si, sc)) > q0(t)
'
check "causal: no oracle - a rollover after the final turn is still paid" '
t = grow(20, d=7000)                       # crosses 110k at turn ~14, ends right after
si, sc, ro, log = core._simulate_rollover_session(t, 110_000, detail=True)
assert ro == 1 and log[0]["remaining"] <= 6, log
t2 = grow(14, d=8000)                      # crosses only on the very last turn (min_turns 14)
si2, sc2, ro2, log2 = core._simulate_rollover_session(t2, 110_000, detail=True)
assert ro2 == 1 and log2[0]["remaining"] == 0 and q(t2, (si2, sc2)) > q0(t2), (log2, q(t2, (si2, sc2)), q0(t2))
'
check "long session: rollover saves quota" '
t = grow(80, d=3000)
si, sc, ro = core._simulate_rollover_session(t)
assert ro >= 1 and q(t, (si, sc)) < q0(t) * 0.9, (ro, q(t, (si, sc)) / q0(t))
'
check "min_turns counted per conversation (new chat restarts at 0)" '
t = grow(60, d=12000)
_, _, ro, log = core._simulate_rollover_session(t, 110_000, detail=True)
assert ro >= 2 and all(e["conv_turns"] >= 14 for e in log), log
assert all(b["after_turn"] - a["after_turn"] >= 14 for a, b in zip(log, log[1:])), log
'
check "real compaction is NOT charged as a cache drop of the simulated chat" '
t = grow(30, d=10000)                      # 15k .. 305k
pre = t[:20]; ctx = sum(pre[-1][:2])
post = [(19000, 0, 300)] + [(10000, 19000 + 10000 * i, 300) for i in range(10)]
s = pre + post                             # Antigravity compacts 205k -> 19k at turn 20
si, sc, ro, log = core._simulate_rollover_session(s, 110_000, detail=True)
r = core.RolloverTracker(); evs = [r.push(*x) for x in s]
assert evs[20] == "compaction", evs[18:22]
# old sim charged the whole simulated context uncached at the compaction turn; now it stays warm
i0 = sum(x[0] for x in s[:20])
assert ro >= 1
'
check "real TTL drop is replayed on the smaller simulated context" '
t = grow(40, d=4000)
d = list(t); d[30] = (sum(t[30][:2]), 0, 300)    # cache expired at turn 30
a = core._simulate_rollover_session(t, 110_000); b = core._simulate_rollover_session(d, 110_000)
extra_sim, extra_real = q(d, b) - q(t, a), q0(d) - q0(t)
assert 0 < extra_sim < extra_real, (extra_sim, extra_real)
'
check "measured warm-up cache misses make a rollover more expensive" '
hot = grow(60, d=3000)
cold = list(hot); cold[1] = (sum(hot[1][:2]), 0, 300); cold[2] = (sum(hot[2][:2]), 0, 300)
assert core.RolloverTracker().__class__
fh, fc = core.RolloverTracker(), core.RolloverTracker()
[fh.push(*x) for x in hot]; [fc.push(*x) for x in cold]
assert fc.features()["warm_miss"][:2] == [1.0, 1.0] and core.rollover_cost_tok(fc.features()) > core.rollover_cost_tok(fh.features())
assert core.rollover_policy(fc.features())["cap"] >= core.rollover_policy(fh.features())["cap"]
'
check "settled flags gate the simulation (no settled point -> no rollover)" '
t = grow(60, d=4000)
assert core._simulate_rollover_session(t, 110_000, settled=[False] * 60)[2] == 0
assert core._simulate_rollover_session(t, 110_000, settled=[True] * 60)[2] >= 1
'
check "p_continue pauses auto-spawn but never a manual threshold" '
f = {"ctx": 200000, "turns": 40, "drops": 0, "warm_turns": 36, "warm_miss": [0, 0, 0]}
assert core.rollover_policy(f)["roll"]
lo = core.rollover_policy({**f, "p_continue": 0.05})
assert lo["paused"] and not lo["roll"], lo
assert core.rollover_policy({**f, "p_continue": 0.05}, {"threshold_mode": "110", "min_turns": 14})["roll"]
assert core.continuation_probability([{"settled_hi": 9, "settled_hi_continued": 0}]) < 0.1
'
check "HUD threshold and simulation use the same policy function" '
f = {"ctx": 130000, "turns": 30, "drops": 2, "warm_turns": 26, "warm_miss": [0.0, 1.0, 0.0]}
cfg = {"threshold_mode": "auto", "min_turns": 14}
a = core.compute_adaptive_threshold({"rollover_features": f, "cur_tot": 130000, "turns": 30}, cfg)
b = core.rollover_policy(f, cfg)
assert a == b, (a, b)
assert core.compute_adaptive_threshold({"rollover_features": f}, {"threshold_mode": "140", "min_turns": 14})["cap_k"] == 140
assert core.AUTO_CAP_MIN <= b["cap"] <= core.AUTO_CAP_MAX and b["cost_tok"] > core.COLD_START_TOK
'

# ---------- db token fallback + IDE/CLI discovery + settled detection ----------
check "transcript without tokens falls back to conversations/<id>.db (CLI dir scanned)" '
def vi(n):
    b = bytearray()
    while True:
        x = n & 0x7F; n >>= 7
        b.append(x | (0x80 if n else 0))
        if not n: return bytes(b)
def ld(f, payload): return vi((f << 3) | 2) + vi(len(payload)) + payload
def usage(i, c, o): return ld(1, vi(8) + vi(1) + ld(4, vi(16) + vi(i) + vi(40) + vi(c) + vi(24) + vi(o)))
app = T / "gemini" / "antigravity-cli"; sid = "11111111-2222-3333-4444-555555555555"
logs = app / "brain" / sid / ".system_generated" / "logs"; logs.mkdir(parents=True)
(app / "conversations").mkdir(parents=True)
toks = [(15000, 0, 300)] + [(3000, 15000 + 3000 * k, 200) for k in range(29)]
db = sqlite3.connect(str(app / "conversations" / f"{sid}.db"))
db.execute("create table gen_metadata (idx integer primary key, data blob, size integer not null default 0)")
rows = [usage(*x) for x in toks]; rows.insert(5, usage(0, 0, 0))        # zero row: alignment must hold
for k, blob in enumerate(rows): db.execute("insert into gen_metadata (idx, data) values (?, ?)", (k, blob))
db.commit(); db.close()
recs = [{"type": "USER_INPUT", "content": "<USER_REQUEST>fix the parser</USER_REQUEST>", "created_at": "2026-01-01T10:00:00Z"}]
for k in range(len(rows)):
    last = k == len(rows) - 1
    recs.append({"type": "PLANNER_RESPONSE", "created_at": f"2026-01-01T10:{k // 6:02d}:{(k % 6) * 9:02d}Z",
                 "tool_calls": "None" if last else "[{\"name\": \"view_file\", \"args\": {}}]"})
(logs / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\n")
src = core.discover_session_sources()
assert [s[1] for s in src] == [sid], src
r = core.parse_session_cached(sid, src[0][2], src[0][3])
assert r and r["source"] == "db" and r["turns"] == 30 and r["app"] == "antigravity-cli", r and {k: r[k] for k in ("source", "turns", "app")}
assert r["tot_in"] == sum(x[0] for x in toks) and r["cur_tot"] == sum(toks[-1][:2])
assert r["last_settled"] and r["user_inputs"] == 1 and r["goal_preview"] == "fix the parser"
assert r["rollover_features"]["turns"] == 30
assert core.parse_transcript_cached(logs / "transcript.jsonl")["turns"] == 30
st = core.build_state(); assert st["savings"]["sessions_count"] == 1 and st["active"]["conversation_id"] == sid
'
check "settled detection: tool calls / background task / subagent / no user => not settled" '
def P(ts, final=True, quiet=True): return {"ts": ts, "final": final, "quiet": quiet and final}
base = [P(0, False), P(5, False), P(10, False), P(15)]
assert core._settled_flags(base, True)[-1]
assert not core._settled_flags(base, False)[-1]                    # subagent chat (no USER_INPUT)
assert not core._settled_flags(base[:3] + [P(15, quiet=False)], True)[-1]
assert not core._settled_flags(base + [P(30, False)], True)[3]       # agent resumed 15 s later
assert core._settled_flags(base + [P(60, False)], True)[3]           # idle 45 s: tick could fire
assert not core._settled_flags([P(0), P(4000)], True)[0]            # no momentum
tx = T / "tx.jsonl"
tx.write_text("\n".join(json.dumps(r) for r in [
  {"type": "USER_INPUT", "content": "go"},
  {"type": "PLANNER_RESPONSE", "created_at": "2026-01-01T10:00:00Z", "tool_calls": "[{\"name\": \"run_command\"}]"},
  {"type": "GENERIC", "content": "Tool is running as a background task with task id: abc/task-7"},
  {"type": "PLANNER_RESPONSE", "created_at": "2026-01-01T10:00:05Z", "tool_calls": None},
  {"type": "SYSTEM_MESSAGE", "content": "sender=abc/task-7 finished"},
  {"type": "PLANNER_RESPONSE", "created_at": "2026-01-01T10:00:50Z", "tool_calls": "[]"},
]) + "\n")
pl = core._scan_transcript(tx)["planners"]
assert [p["final"] for p in pl] == [False, True, True] and [p["quiet"] for p in pl] == [False, False, True], pl
'

# ---------- handoff packet ----------
check "handoff_pack: parses string tool_calls, merges status+diffstat, stays small, read-only git" '
ws = T / "ws"; ws.mkdir()
g = lambda *a: subprocess.run(["git", "-C", str(ws), *a], capture_output=True, text=True)
g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
(ws / "a.py").write_text("x = 1\n" * 10); g("add", "."); g("commit", "-qm", "init")
(ws / "a.py").write_text("x = 2\n" * 12); (ws / "new.txt").write_text("n\n")
app = T / "gemini" / "antigravity"; sid = "99999999-2222-3333-4444-555555555555"
logs = app / "brain" / sid / ".system_generated" / "logs"; logs.mkdir(parents=True)
fp = str(ws / "a.py")
tc = repr([{"name": "replace_file_content", "args": {"TargetFile": json.dumps(fp), "Instruction": "\"x\""}},
           {"name": "view_file", "args": {"AbsolutePath": json.dumps(str(ws / "b.py"))}}])
(logs / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in [
  {"type": "USER_INPUT", "content": "<USER_REQUEST>make x two</USER_REQUEST>"},
  {"type": "PLANNER_RESPONSE", "tool_calls": tc, "content": ""},
  {"type": "PLANNER_RESPONSE", "tool_calls": None, "content": "Done, see [a.py](file:///" + fp + ")"},
]) + "\n")
idx_before = (ws / ".git" / "index").stat().st_mtime_ns
p = subprocess.run([sys.executable, str(ROOT / "scripts" / "handoff_pack.py"), str(ws), "-c", sid, "--no-save"],
                   capture_output=True, text=True, env={**os.environ})
out = p.stdout
assert p.returncode == 0, p.stderr
assert "Goal: make x two" in out and "Last state: Done, see a.py" in out, out
assert "- M a.py (+12/-10)" in out and "?? new.txt" in out, out
assert "## Edited in that conversation\n- a.py" in out and "b.py" in out, out
assert "[Saved" not in out and len(out) // 4 < 400, len(out)
assert (ws / ".git" / "index").stat().st_mtime_ns == idx_before, "git index was rewritten"
'

echo "PASS=$PASS FAIL=$FAIL"
[[ $FAIL -eq 0 ]]
