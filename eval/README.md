# Signal Retention & Task Evaluation (`eval/`)

This directory contains **30 coding tasks** across 3 miniature fixture repositories (`py_calc`, `node_api`, `log_ops`) covering 5 task families:

- `fix-failing-test` (6 tasks: `T01`–`T06`)
- `add-feature-and-test` (6 tasks: `T07`–`T12`)
- `refactor` (6 tasks: `T13`–`T18`)
- `debug-from-log` (6 tasks: `T19`–`T24`)
- `lockfile-dependency` (6 tasks: `T25`–`T30`)

Every task in [tasks.json](tasks.json) has an automatic `verify_cmd` exit-0 check and a reference patch proving the check fails before the fix and passes after.

## What this eval measures vs. what it does not measure

- **What this eval measures (`--driver hook-replay`):** whether required error strings, tracebacks, and source symbols survive hook compression (`signal_retention`), plus direct tool-token differences compared against the **unmodified full raw output** in `off` mode (no baseline truncation and no artificial leading passing lines).
- **What it does NOT measure (`--driver hook-replay`):** whether an LLM agent actually solves tasks. `--driver hook-replay` never invokes a language model, never evaluates `Concise Output` instruction following or `Auto-Handoff` chat resets, and does not run `verify_cmd` to score hook outputs. Task solve rates require `--driver agy` or `--driver manual` (`live_eval.json`).

## 1. Verify fixture & task integrity

```bash
python3 eval/runner.py --verify-fixtures
```

## 2. Deterministic hook signal-retention eval (`--driver hook-replay`)

Replays each task's tool calls (`read_file`, unmodified `unittest` output, 450-line service logs, 400-package `package-lock.json` reads, and duplicate re-reads) through the real Perl hooks for all 6 modes (`off`, `mode1`, `mode2a`, `mode2b`, `mode2c`, `max`) across `N=3` runs:

```bash
python3 eval/runner.py --driver hook-replay --runs 3 --out eval/results/hook_eval.json
python3 eval/runner.py --summarize eval/results/hook_eval.json
```

In `off`, every tool delivers the full raw output unmodified. Any mode whose signal retention drops by more than **2.0 percentage points** vs `off` is flagged `LOSS >2pp`.

## 3. End-to-end live agent eval (`--driver agy` or `--driver manual`)

Live agent evaluation stages a fresh temporary copy of the task fixture **without** applying the reference patch, runs the mode, executes `verify_cmd` after the agent finishes, and records `passed`, `total_tokens`, `cost_proxy`, `retries`, and `wall_ms` into `eval/results/live_eval.json`.

- **Headless CLI (when `agy` is installed on `PATH`):**

```bash
python3 eval/runner.py --driver agy --runs 3 --out eval/results/live_eval.json
```

- **Interactive IDE walkthrough (`--driver manual`):** stages a fresh temp workspace per task and mode, prompts you to run the task in Antigravity IDE, executes `verify_cmd` automatically **without** applying any reference patch, and records verified JSON into `eval/results/live_eval.json`.

```bash
python3 eval/runner.py --driver manual --modes off,mode1,max --runs 3 --out eval/results/live_eval.json
python3 eval/runner.py --summarize eval/results/live_eval.json
```

Any mode whose live pass rate drops by more than **2.0 percentage points** vs `off` is automatically flagged `LOSS >2pp`.
