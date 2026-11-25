# Evaluation Harness (`eval/`)

This directory holds **43 coding tasks** across 5 fixture repositories (`py_calc`, `node_api`, `log_ops`, `order_engine`, `log_router`):

- **[`tasks.json`](tasks.json) (34 baseline tasks):** 30 standard tasks (`T01`–`T30`) plus 4 two-phase continuity tasks (`H01`–`H04`).
- **[`hard_tasks.json`](hard_tasks.json) (9 harder multi-module + 6-turn A/B tasks):**
  - `X01`–`X05`: Cross-module invariant bugs spanning 2–4 Python files + large logs (`71–85 KB`) + JSON/YAML configs (`9–21 KB`).
  - `X06`: Multi-module two-phase handoff task.
  - `L01`–`L03`: True **6-turn multi-turn A/B** sequences executed via `agentapi new-conversation` + `agentapi send-message` (`off` unbroken 6-turn chat vs `default` real `handoff_pack.py` reset after Turn 3).

Every task has a `verify_cmd` and `reference_edit`/`reference_edits` proving the fixture fails before fixes and passes after.

## Reproducible commands

```bash
# 1. Verify baseline (34/34) and hard (9/9) fixtures fail before fix and pass after reference edits
python3 eval/runner.py --verify-fixtures
python3 eval/runner.py --verify-fixtures --tasks-file eval/hard_tasks.json

# 2. Live multi-turn 6-turn A/B benchmark on gemini-4-hydrogen-high (18 sessions, 108 user turns)
python3 eval/runner.py --driver agentapi --tasks-file eval/hard_tasks.json \
  --tasks L01,L02,L03 --modes off,default --runs 3 --workers 2 \
  --model gemini-4-hydrogen-high --out eval/results/ab_long_eval.json

# 3. Live hard multi-module evaluation on gemini-4-hydrogen-xhigh (54 runs)
python3 eval/runner.py --driver agentapi --tasks-file eval/hard_tasks.json \
  --tasks X01,X02,X03,X04,X05,X06 --modes off,default,max --runs 3 --workers 2 \
  --model gemini-4-hydrogen-xhigh --out eval/results/hard_eval.json

# 4. Deterministic hook signal-retention replay or summarize committed results
python3 eval/runner.py --driver hook-replay --runs 3 --out eval/results/hook_eval.json
python3 eval/runner.py --summarize eval/results/ab_long_eval.json
python3 eval/runner.py --summarize eval/results/hard_eval.json
```

Committed live results:
- [ab_long_eval.json](results/ab_long_eval.json): `18` six-turn sessions on `gemini-4-hydrogen-high` (`9/9` vs `9/9`, `-31.9%` total tokens).
- [hard_eval.json](results/hard_eval.json): `54` runs on `gemini-4-hydrogen-xhigh` (`18/18 off`, `17/18 default`, `18/18 max` with `-24.2%` tokens and `-9.0%` cost proxy).
- [live_eval.json](results/live_eval.json): `90` baseline runs on `flash` (`30/30` across `off`, `default`, `max`).
