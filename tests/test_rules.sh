#!/usr/bin/env bash
# token-saver always-loaded prompt checks: 0-tok default overhead + skill frontmatter validity.
# Usage: bash tests/test_rules.sh   (exit 0 = all pass)
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(command -v python3 || command -v python)"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); }
bad() { FAIL=$((FAIL+1)); printf 'FAIL: %s\n  got: %s\n' "$1" "$2"; }

# ---------- default prompt overhead must stay 0 (no always-loaded AGENTS.md or agents/*.md) ----------
if [[ ! -f "$ROOT/rules/AGENTS.md" ]]; then ok; else bad "default rules/AGENTS.md absent" "present"; fi
if [[ -z "$(ls -A "$ROOT/agents" 2>/dev/null || true)" ]]; then ok; else bad "no bundled plugin subagents" "$(ls -A "$ROOT/agents")"; fi

# ---------- frontmatter validity + disable-model-invocation on all skills ----------
out=$("$PY" - "$ROOT" <<'EOF'
import re, sys
from pathlib import Path
root = Path(sys.argv[1]); errs = []
def fm(p):
    m = re.match(r"---\n(.*?)\n---\n", p.read_text(encoding="utf-8"), re.S)
    return m.group(1) if m else None
def get(y, k):
    m = re.search(rf"^{k}:[ \t]*(.*)$", y, re.M)
    return m.group(1).strip() if m else None
files = sorted(root.glob("skills/*/SKILL.md"))
if not files:
    errs.append("no skills found")
names = set()
for p in files:
    rel = p.relative_to(root).as_posix(); y = fm(p)
    if y is None: errs.append(f"{rel}: no frontmatter"); continue
    name, desc = get(y, "name"), get(y, "description")
    if not name or name != p.parent.name: errs.append(f"{rel}: invalid name")
    if name in names: errs.append(f"{rel}: duplicate name {name}")
    names.add(name)
    if not desc or ": " in desc or " #" in desc: errs.append(f"{rel}: invalid description")
    if (get(y, "disable-model-invocation") or "").lower() != "true":
        errs.append(f"{rel}: must set disable-model-invocation: true so prompt tax stays 0")
print("\n".join(errs))
EOF
)
[[ -z "$out" ]] && ok || bad "frontmatter valid" "$out"

# ---------- per-turn overhead via replay_io.plugin_overhead_bytes() must be 0 ----------
tok=$("$PY" - "$ROOT" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1] + "/scripts")
sys.path.insert(0, sys.argv[1] + "/mcp_app")
import replay_io
print(replay_io.plugin_overhead_bytes())
EOF
)
[[ "$tok" == "0" ]] && ok || bad "always-loaded prompt bytes == 0 by default" "$tok bytes"

# ---------- references stay resolvable & no dead links ----------
for s in audit stats handoff_pack repo_digest ts_inspect; do
  if grep -rqs "scripts/$s.py" "$ROOT/skills"; then
    [[ -f "$ROOT/scripts/$s.py" ]] && ok || bad "referenced scripts/$s.py exists" "missing"
  fi
done
grep -rqsE 'cmd_hook|cmd_wrap|lean-workflow|swarm-prep|vision-scout|contact_sheet' "$ROOT/skills" "$ROOT/plugin.json" "$ROOT/hooks.json" "$ROOT/scripts"/*.pl "$ROOT/scripts/lib"/*.pl \
  && bad "no references to removed components" "found stale reference" || ok

printf 'test_rules: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
