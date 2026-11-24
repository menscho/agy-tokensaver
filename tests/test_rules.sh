#!/usr/bin/env bash
# token-saver always-loaded prompt checks: rules/AGENTS.md + skill/agent frontmatter.
# Antigravity puts AGENTS.md in full and "- name (path): description" per skill /
# "- name: description" per subagent into EVERY turn, so these stay small and valid.
# Usage: bash tests/test_rules.sh   (exit 0 = all pass)
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(command -v python3 || command -v python)"
PASS=0; FAIL=0
ok()  { PASS=$((PASS+1)); }
bad() { FAIL=$((FAIL+1)); printf 'FAIL: %s\n  got: %s\n' "$1" "$2"; }

A="$ROOT/rules/AGENTS.md"
# ---------- AGENTS.md budget + the rules that address the measured top output waste ----------
sz=$(wc -c < "$A")
[[ $sz -le 900 ]] && ok || bad "AGENTS.md <= 900 bytes (~225 tok, charged every turn)" "$sz bytes"
grep -q 'ArtifactMetadata' "$A"  && ok || bad "AGENTS.md: ArtifactMetadata rule (failed artifact-path writes)" "missing"
grep -q 'run_command' "$A"       && ok || bad "AGENTS.md: no file writes via run_command rule" "missing"
grep -Eq 'anchor' "$A"           && ok || bad "AGENTS.md: targeted-edit rule" "missing"
grep -q 'lean-workflow' "$A"     && ok || bad "AGENTS.md: lean-workflow pointer" "missing"
grep -q '^---' "$A"              && bad "AGENTS.md must not have frontmatter" "found ---" || ok

# ---------- frontmatter validity + size ----------
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
files = [(p, "skill") for p in sorted(root.glob("skills/*/SKILL.md"))] + [(p, "agent") for p in sorted(root.glob("agents/*.md"))]
names = set()
for p, kind in files:
    rel = p.relative_to(root).as_posix(); y = fm(p)
    if y is None: errs.append(f"{rel}: no frontmatter"); continue
    name, desc = get(y, "name"), get(y, "description")
    if not name: errs.append(f"{rel}: no name")
    if name in names: errs.append(f"{rel}: duplicate name {name}")
    names.add(name)
    if kind == "skill" and name != p.parent.name: errs.append(f"{rel}: name != folder")
    if kind == "agent" and name != p.stem: errs.append(f"{rel}: name != file stem")
    if not desc: errs.append(f"{rel}: empty description"); continue
    if desc in (">-", ">", "|"): continue  # folded scalar: size checked below via bytes
    # plain YAML scalar: no ': ' / ' #', no leading indicator chars
    if ": " in desc or " #" in desc or desc[0] in "[]{}&*!|>'\"%@\x60#,?:-":
        errs.append(f"{rel}: description is not a safe plain YAML scalar")
    if p.parent.name != "handoff" and len(desc) > 100:
        errs.append(f"{rel}: description {len(desc)} chars > 100")
    if kind == "agent":
        for k, v in (("subagent", "true"), ("mainAgent", "false"), ("inheritCustomizations", "false")):
            if get(y, k) != v: errs.append(f"{rel}: {k} must be {v}")
        if not get(y, "model"): errs.append(f"{rel}: no model")
        if not re.search(r"^tools:", y, re.M): errs.append(f"{rel}: no tools")
        if re.search(r"edit_file|write_file|write_to_file", y): errs.append(f"{rel}: scouts must stay read-only")
print("\n".join(errs))
EOF
)
[[ -z "$out" ]] && ok || bad "frontmatter valid" "$out"

# ---------- per-turn overhead (real Antigravity listing format) ----------
tok=$("$PY" - "$ROOT" <<'EOF'
import re, sys
from pathlib import Path
root = Path(sys.argv[1]); inst = "C:\\Users\\user\\.gemini\\config\\plugins\\token-saver"
def field(y, k):
    L = y.splitlines()
    for i, l in enumerate(L):
        if l.startswith(k + ":"):
            v = l[len(k) + 1:].strip()
            if v in (">-", ">", "|", "|-"):
                c = []
                for l2 in L[i + 1:]:
                    if not l2.startswith(" "): break
                    c.append(l2.strip())
                return " ".join(c)
            return v
    return ""
fm = lambda p: re.match(r"---\n(.*?)\n---", p.read_text(encoding="utf-8"), re.S).group(1)
n = len((root / "rules/AGENTS.md").read_bytes())
for p in root.glob("skills/*/SKILL.md"):
    y = fm(p); n += len(f"- {field(y,'name')} ({inst}\\skills\\{p.parent.name}\\SKILL.md): {field(y,'description')}\n".encode())
for p in root.glob("agents/*.md"):
    y = fm(p); n += len(f"- {field(y,'name')}: {field(y,'description')}\n".encode())
print(n // 4)
EOF
)
[[ "$tok" =~ ^[0-9]+$ && $tok -le 500 ]] && ok || bad "always-loaded prompt <= 500 tok/turn (real format)" "$tok"

# ---------- references stay resolvable ----------
for w in fanout_audit isolated_refactor test_fix_loop; do
  [[ -f "$ROOT/workflows/$w.py" ]] && grep -q "$w.py" "$ROOT/skills/lean-workflow/SKILL.md" && ok || bad "lean-workflow references workflows/$w.py" "missing"
done
for s in audit stats repo_digest contact_sheet ts_inspect; do
  if grep -rqs "scripts/$s.py" "$ROOT/skills" "$ROOT/agents"; then
    [[ -f "$ROOT/scripts/$s.py" ]] && ok || bad "referenced scripts/$s.py exists" "missing"
  fi
done
grep -rqsE 'fast-explorer|triage-scout|web-scout' "$ROOT/rules" "$ROOT/skills" "$ROOT/agents" "$ROOT/workflows" "$ROOT/scripts"/*.pl \
  && bad "no references to removed agents" "$(grep -rlE 'fast-explorer|triage-scout|web-scout' "$ROOT/rules" "$ROOT/skills" "$ROOT/agents" "$ROOT/workflows" "$ROOT/scripts"/*.pl)" || ok
grep -q 'vision-scout' "$ROOT/scripts/read_hook.pl" && { [[ -f "$ROOT/agents/vision-scout.md" ]] && ok || bad "read_hook names vision-scout -> agent exists" "missing"; } || ok
# swarm-prep must run in PowerShell 5.1 too (no &&, head, /tmp)
grep -Eq '&&|\bhead -n|/tmp/' "$ROOT/skills/swarm-prep/SKILL.md" && bad "swarm-prep command PowerShell-safe" "uses &&/head//tmp" || ok
# workflows parse and child edit prompts carry the write rules
for w in "$ROOT"/workflows/*.py; do
  "$PY" -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" "$w" 2>/dev/null && ok || bad "workflow parses: $(basename "$w")" "syntax error"
done
for w in isolated_refactor test_fix_loop; do
  grep -q 'ArtifactMetadata' "$ROOT/workflows/$w.py" && ok || bad "$w child prompt has write rules" "missing"
done

printf 'test_rules: %d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
