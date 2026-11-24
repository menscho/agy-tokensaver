---
name: lean-workflow
description: Isolated workflows for >=5-file audits/refactors/tests.
---

# Phase-Isolated Workflows (`/lean-workflow`)

Raw reads, logs and fix iterations run in isolated `run_workflow` agents, so they never enter the main chat's cached prefix (-65-80% parent context on such jobs).

Choose the matching bundled workflow under `~/.gemini/config/plugins/token-saver/workflows/` and launch it directly via `run_workflow(ScriptPath=..., Args=..., Description=...)`:

1. **Multi-File Audit (`fanout_audit.py`)** — Parallel `flash`/`flash_lite` scan across many files; returns only a deduplicated <500-token ranked report:
   - `ScriptPath`: `~/.gemini/config/plugins/token-saver/workflows/fanout_audit.py`
   - `Args`: `{"goal": "<audit goal>", "scan_model": "flash", "batch_size": 5}` *(optional `"files": [...]`)*
2. **Isolated Multi-File Refactor (`isolated_refactor.py`)** — Plans on Flash, executes per-file surgical edits in isolated child agents, verifies once:
   - `ScriptPath`: `~/.gemini/config/plugins/token-saver/workflows/isolated_refactor.py`
   - `Args`: `{"goal": "<refactor spec>", "test_cmd": "<optional test cmd>", "edit_model": "inherit"}`
3. **Isolated Test-Fix Loop (`test_fix_loop.py`)** — Triages noisy test output on Flash, fixes surgically in isolated rounds, returns only pass/fail + diff stat:
   - `ScriptPath`: `~/.gemini/config/plugins/token-saver/workflows/test_fix_loop.py`
   - `Args`: `{"test_cmd": "<test command>", "max_rounds": 3, "fix_model": "inherit"}`

Always set `Cwd` to the target project directory when running outside the primary workspace. Skip this for 1-4 file jobs: do them directly.
