# Quality & Token Evaluation (`eval/`)

This directory contains **30 fixed, scripted coding tasks** across 3 miniature fixture repositories (`py_calc`, `node_api`, `log_ops`) covering 5 task families:

- `fix-failing-test` (6 tasks: `T01`–`T06`)
- `add-feature-and-test` (6 tasks: `T07`–`T12`)
- `refactor` (6 tasks: `T13`–`T18`)
- `debug-from-log` (6 tasks: `T19`–`T24`)
- `lockfile-dependency` (6 tasks: `T25`–`T30`)

Every task in [tasks.json](tasks.json) has an automatic `verify_cmd` exit-0 check and a reference patch proving the check fails before the fix and passes after.

## 1. Verify fixture & task integrity

```bash
python3 eval/runner.py --verify-fixtures
```

## 2. Deterministic hook signal-preservation eval (`--driver hook-replay`)

Replays every task's realistic tool calls (`read_file`, noisy `unittest` output, 450-line service logs, 400-package `package-lock.json` reads, and duplicate re-reads) through the real Perl hooks for all 6 modes (`off`, `mode1`, `mode2a`, `mode2b`, `mode2c`, `max`) across `N=3` runs, asserting that 100% of required diagnostic error codes, tracebacks, and source symbols survive compression:

```bash
python3 eval/runner.py --driver hook-replay --runs 3 --out eval/results/hook_eval.json
python3 eval/runner.py --summarize eval/results/hook_eval.json
```

**What this proves and what it does NOT prove:** `--driver hook-replay` deterministically proves that hooks never strip required error lines, tracebacks, or symbols across all 30 tasks, and measures exact tool-token reduction. It does **not** invoke a live LLM, so it cannot measure model-level behavioral differences from `Concise Output` rules or `Auto-Handoff` chat resets.

## 3. End-to-end live agent protocol (headless `agy` CLI or manual IDE)

Because Antigravity IDE does not expose a headless automation runner on all hosts, live LLM pass-rate evaluation follows this protocol:

1. For each mode (`off`, `mode1`, `mode2a`, `mode2b`, `mode2c`, `max`), switch the plugin HUD to that mode (or disable the plugin for `off`).
2. For each of the 30 tasks × `N >= 3` runs:
   - Copy the task's fixture into a fresh temp directory and open a fresh chat.
   - Paste `prompt` from `eval/tasks.json`.
   - When the agent finishes, run `verify_cmd` and record `passed` (`true`/`false`), transcript total tokens, cost proxy, retries, re-reads, and wall time.
3. Save the raw JSON array under `eval/results/live_eval.json` and summarize:

```bash
python3 eval/runner.py --summarize eval/results/live_eval.json
```

Any mode whose pass rate drops by more than **2.0 percentage points** vs `off` is automatically flagged `LOSS >2pp`.
