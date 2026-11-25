# Evaluation Harness (`eval/`)

This directory holds **34 coding tasks** across 3 small repositories (`py_calc`, `node_api`, `log_ops`), covering:

- 30 standard tasks (`T01`–`T30`) across 5 categories: `fix-failing-test`, `add-feature-and-test`, `refactor`, `debug-from-log`, and `lockfile-dependency`.
- 4 multi-step **Auto-Handoff continuity tasks** (`H01`–`H04`): in `off` a single conversation runs both steps; in active modes (`default`, `max`), Phase 1 runs in Conversation A, `scripts/handoff_pack.py` extracts a compact handoff packet, all Conversation A history is discarded, and a fresh Conversation B finishes Phase 2 from that packet alone.

Every task in [tasks.json](tasks.json) has an automated `verify_cmd` check and a reference patch proving the test fails on unpatched fixtures and passes after the reference edit.

## Reproducible commands

```bash
# 1. Verify all 34 tasks fail before fix and pass after reference edit
python3 eval/runner.py --verify-fixtures

# 2. Deterministic hook signal-retention replay (34 tasks x 3 runs, 0 LLM calls)
python3 eval/runner.py --driver hook-replay --runs 3 --out eval/results/hook_eval.json

# 3. Live Antigravity ConversationService evaluation (requires ANTIGRAVITY_LS_ADDRESS + CSRF token)
python3 eval/runner.py --driver antigravity \
  --tasks T01,T07,T14,T19,T25,T30,H01,H02,H03,H04 \
  --modes off,default,max --runs 3 --workers 4 --model flash \
  --out eval/results/live_eval.json

# Summarize any saved results file with Wilson 95% CIs
python3 eval/runner.py --summarize eval/results/live_eval.json
```

In `--driver hook-replay`, `off` delivers unmodified full raw files (zero baseline truncation), and any mode whose signal retention drops by more than `2.0` percentage points vs `off` is flagged `LOSS >2pp`.

In `--driver antigravity`, `--driver agy`, and `--driver manual`, each trial runs in an isolated temporary copy **without** applying the reference patch before running `verify_cmd`. Results from the 90-run live evaluation (`10 tasks × 3 runs × 3 modes` on `flash`) are committed in [live_eval.json](results/live_eval.json).
