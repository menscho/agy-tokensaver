# Phase-isolated multi-file refactor workflow for Antigravity `run_workflow`.
# Plans on Flash, executes file edits in isolated child agents (keeping the
# parent session permanently <40K tokens), and verifies once at the end.
#
# Args:
#   goal (str, required): Refactor specification
#   test_cmd (str, optional): Verification command to run once at the end
#   edit_model (str, optional): "inherit" (default, main model) or "pro"

import os
import subprocess
from pathlib import Path

goal = args.get("goal", "").strip()
if not goal:
    raise ValueError("Args must include a non-empty 'goal' string.")

ws = str(WORKSPACES[0]) if WORKSPACES else os.getcwd()
test_cmd = args.get("test_cmd", "").strip()
edit_model = args.get("edit_model", "inherit")
digest_script = str(Path.home() / ".gemini/config/plugins/token-saver/scripts/repo_digest.py")

phase("1. Plan refactor slices (Flash)")
digest_out = subprocess.run(
    ["python3", digest_script, ws, "100"],
    text=True, capture_output=True, check=False,
).stdout[:12000]

plan_schema = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "file": {"type": "string"},
            "instructions": {"type": "string"},
        },
        "required": ["file", "instructions"],
    },
}

steps = await agent(
    f"Create a minimal, ordered file-by-file edit plan in `{ws}` for:\n{goal}\n\n"
    f"Repo digest:\n{digest_out}\n\n"
    f"Inspect only targeted line slices needed to verify exact symbol names/signatures. "
    f"Return an ordered array of {{file, instructions}} (max 12 files).",
    schema=plan_schema,
    role="Plan refactor",
    model="flash",
)
log(f"Planned {len(steps)} isolated edit step(s)")

phase(f"2. Execute surgical edits ({edit_model})")
results = await pipeline(
    steps,
    lambda s: agent(
        f"Apply this exact surgical edit in `{ws}`:\n"
        f"File: {s['file']}\n"
        f"Instructions: {s['instructions']}\n"
        f"Overall goal: {goal}\n\n"
        f"Rules: Read only the needed line slices of `{s['file']}` (and direct imports if required). "
        f"Use `edit_file` (no full rewrites unless <100L). Return a 1-line summary of what changed.",
        schema={"type": "string"},
        role=f"Edit {s['file']}",
        model=edit_model,
    ),
)

for s, r in zip(steps, results):
    log(f"{s['file']}: {r}")

phase("3. Final verification")
verify_note = "Skipped (no test_cmd provided)."
if test_cmd:
    verify_note = await agent(
        f"In `{ws}`, run `{test_cmd}` once. If it passes, return 'PASS' + 1-line summary. "
        f"If it fails, return ONLY the failing test names, exact errors, and file:line refs.",
        schema={"type": "string"},
        role="Verify refactor",
        model="flash",
    )

diff_stat = subprocess.run(["git", "-C", ws, "diff", "--stat"], text=True, capture_output=True).stdout.strip()
print(f"## Isolated Refactor Complete\n- Edits: {len(steps)} files\n- Verify: {verify_note}")
if diff_stat:
    print(f"\n```text\n{diff_stat}\n```")
