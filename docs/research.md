# Research and Open-Source References

Every paper and project listed below corresponds directly to an active module in this repository.

## Prompt Caching and Session Resets

- **Don't Break the Cache** ([arXiv:2601.06007](https://arxiv.org/abs/2601.06007)) — Alphabetically sorted MCP tool definitions (`mcp_app/server.py`) and deferred concise-rule writes (`mcp_app/rules_sync.py`) so active KV caches are not invalidated mid-run.
- **Keeping the Cache Warm** ([arXiv:2607.19214](https://arxiv.org/abs/2607.19214)) and **Persistent Context Graphs** ([arXiv:2609.40118](https://arxiv.org/abs/2609.40118)) — Empirical idle TTL tracking and cold-cache rollover payback triggers (`mcp_app/rollover.py`, `mcp_app/snapshots.py`).
- **The Complexity Trap** ([arXiv:2508.21433](https://arxiv.org/abs/2508.21433)) and **Active Context Compression** ([arXiv:2601.07190](https://arxiv.org/abs/2601.07190)) — Threshold-based fresh-chat handoffs carrying compact git status, edited symbols, and recent errors (`scripts/handoff_pack.py`).

## Read, Write and Loop Guards

- **Sketch-of-Thought** ([arXiv:2503.05179](https://arxiv.org/abs/2503.05179)) — Opt-in concise reply rule (`mcp_app/rules_sync.py`).
- **TokenEconomizer** ([arXiv:2610.22732](https://arxiv.org/abs/2610.22732)) — Cross-session verified command promotion (`scripts/handoff_pack.py`).
- **AdaEdit** ([arXiv:2604.27296](https://arxiv.org/abs/2604.27296)) — Non-blocking full-file overwrite warning (`scripts/write_hook.py`, `scripts/post_invocation_alerts.py`).
- **IAL-Scan** ([arXiv:2607.01641](https://arxiv.org/abs/2607.01641)) — 3x identical `grep_search` and `find_by_name` loop guards with 1-retry override (`scripts/hook_common.py`).
- **LogDx-CI** ([arXiv:2605.28876](https://arxiv.org/abs/2605.28876)) — Tail-anchoring unbounded `>46KB` log and spillback reads (`scripts/read_hook.py`).

## Open-Source Inspirations

- **`@plexer/cli`** — `PostToolUse` edit-sequence invalidation (`scripts/post_edit_hook.py`), `initialNumSteps` compaction resets (`scripts/read_hook.py`), and `ts_inspect.py outline`.
- **`context-mode` and `squeez`** — In-memory SQLite FTS5 chunk search (`scripts/ts_inspect.py query`) and post-edit dirty-hunk read narrowing (`scripts/read_hook.py`).
