# Isolated test-fix loop workflow for Antigravity `run_workflow`.
# Absorbs noisy test outputs and iterative fix turns in isolated agents so NONE
# of the test-failure clutter enters the parent session's cached prefix.
#
# Args:
#   test_cmd (str, required): Command to run tests (e.g. "pytest -q" or "npm test")
#   max_rounds (int, optional): Max fix attempts (default 3)
#   fix_model (str, optional): "inherit" (default) or "pro"

import os
import subprocess

test_cmd = args.get("test_cmd", "").strip()
if not test_cmd:
    raise ValueError("Args must include a non-empty 'test_cmd' string.")

ws = str(WORKSPACES[0]) if WORKSPACES else os.getcwd()
max_rounds = max(1, min(6, int(args.get("max_rounds", 3))))
fix_model = args.get("fix_model", "inherit")

triage_schema = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "summary": {"type": "string"},
        "failures": {"type": "string"},
        "narrow_cmd": {"type": "string"},
    },
    "required": ["passed", "summary"],
}

final_status = None
for rnd in range(1, max_rounds + 1):
    phase(f"Round {rnd}: Triage tests (Flash)")
    tri = await agent(
        f"In `{ws}`, run `{test_cmd}` once. Return JSON: passed (bool), summary (1 line), "
        f"failures (exact failing test names, assertions, and file:line refs if failed; empty if passed), "
        f"and narrow_cmd (single-test repro command if failed).",
        schema=triage_schema,
        role=f"Triage round {rnd}",
        model="flash",
    )
    log(f"Round {rnd}: passed={tri['passed']} — {tri['summary']}")
    if tri["passed"]:
        final_status = f"PASS on round {rnd}: {tri['summary']}"
        break

    if rnd == max_rounds:
        final_status = f"STILL FAILING after {max_rounds} rounds:\n{tri.get('failures', tri['summary'])}"
        break

    phase(f"Round {rnd}: Apply surgical fix ({fix_model})")
    narrow = tri.get("narrow_cmd") or test_cmd
    fix_res = await agent(
        f"Fix the failing test(s) in `{ws}`.\n"
        f"Failures:\n{tri.get('failures', tri['summary'])}\n\n"
        f"Rules: Read only the failing code/test slices, apply surgical `edit_file` fix "
        f"(never write files via `run_command`; omit `ArtifactMetadata`), "
        f"verify once with `{narrow}`, and stop. Return a 1-2 sentence summary of the root cause and fix.",
        schema={"type": "string"},
        role=f"Fix round {rnd}",
        model=fix_model,
    )
    log(f"Fix {rnd}: {fix_res}")

diff_stat = subprocess.run(["git", "-C", ws, "diff", "--stat"], text=True, capture_output=True).stdout.strip()
print(f"## Isolated Test-Fix Result\n{final_status}")
if diff_stat:
    print(f"\n```text\n{diff_stat}\n```")
