# Phase-isolated multi-file audit workflow for Antigravity `run_workflow`.
# Keeps ALL raw file reads inside isolated Flash/Flash-Lite agents so only the
# final compact (<600 tok) report enters the parent conversation.
#
# Args:
#   goal (str, required): What to audit (e.g. "find error-handling bugs and leaks")
#   files (list[str], optional): Explicit file list; if omitted, discovered via repo_digest.py
#   batch_size (int, optional): Files per parallel scout (default 5)
#   scan_model (str, optional): "flash" (default) or "flash_lite" (mechanical checks)

import os
import subprocess
from pathlib import Path

goal = args.get("goal", "").strip()
if not goal:
    raise ValueError("Args must include a non-empty 'goal' string.")

ws = str(WORKSPACES[0]) if WORKSPACES else os.getcwd()
batch_size = max(1, int(args.get("batch_size", 5)))
scan_model = args.get("scan_model", "flash")
digest_script = str(Path.home() / ".gemini/config/plugins/token-saver/scripts/repo_digest.py")

phase("1. Discover target files (0 parent tokens)")
target_files = list(args.get("files") or [])
if not target_files:
    digest_out = subprocess.run(
        ["python3", digest_script, ws, "120"],
        text=True, capture_output=True, check=False,
    ).stdout[:12000]
    target_files = await agent(
        f"Select ONLY the files in `{ws}` relevant to this audit goal:\n{goal}\n\n"
        f"Repo digest:\n{digest_out}\n\n"
        f"Return a JSON array of relative file paths (max 30). Do not read files yet.",
        schema={"type": "array", "items": {"type": "string"}},
        role="Select audit targets",
        model="flash_lite",
    )

target_files = [f for f in target_files if f and not f.startswith("..")]
log(f"Selected {len(target_files)} files for isolated audit ({scan_model})")

if not target_files:
    print("No matching files found for audit.")
else:
    batches = [target_files[i : i + batch_size] for i in range(0, len(target_files), batch_size)]

    phase(f"2. Parallel isolated scan ({len(batches)} batches on {scan_model})")
    item_schema = {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "severity": {"type": "string"},
                        "issue": {"type": "string"},
                        "fix": {"type": "string"},
                    },
                    "required": ["file", "severity", "issue"],
                },
            }
        },
        "required": ["findings"],
    }

    batch_results = await parallel(
        batches,
        lambda grp: agent(
            f"Audit ONLY these files in `{ws}` for: {goal}\n"
            f"Files: {grp}\n\n"
            f"Rules: Read only these files (use StartLine/EndLine on >300L files). "
            f"Report ONLY verified, concrete findings with exact file:line. Return empty findings if clean.",
            schema=item_schema,
            role=f"Audit {grp[0]}" + (f" +{len(grp)-1}" if len(grp) > 1 else ""),
            model=scan_model,
        ),
    )

    all_findings = []
    for br in batch_results:
        all_findings.extend(br.get("findings") or [])

    phase("3. Aggregate compact summary")
    if not all_findings:
        print(f"Audit clean across {len(target_files)} files for: {goal}")
    else:
        summary = await agent(
            f"Deduplicate and rank these audit findings by severity (high -> low) for goal: {goal}\n"
            f"Raw findings: {all_findings}\n\n"
            f"Return a concise markdown list (<= 500 tokens) with exact `file:line`, severity, issue, and 1-line fix.",
            schema={"type": "string"},
            role="Synthesize audit report",
            model="flash",
        )
        print(summary)
