# Research and Open-Source References

Papers and projects that informed specific modules in this repository. Where a module only borrows an idea from a paper, the entry says so.

## Prompt Caching and Session Resets

- **Don't Break the Cache: An Evaluation of Prompt Caching for Long-Horizon Agentic Tasks** ([arXiv:2601.06007](https://arxiv.org/abs/2601.06007)) — Keep dynamic content at the end of the prompt: alphabetically sorted MCP tool definitions (`mcp_app/server.py`) and deferred concise-rule writes (`mcp_app/rules_sync.py`) so active KV caches are not invalidated mid-run.
- **Keeping the Cache Warm Pays** ([arXiv:2607.19214](https://arxiv.org/abs/2607.19214)) and **Persistent Context Graphs for Efficient Memory Compaction in LLM Agents** ([arXiv:2609.40118](https://arxiv.org/abs/2609.40118)) — Cache TTL economics behind the empirical idle-TTL tracking and cold-cache rollover payback triggers (`mcp_app/rollover.py`, `mcp_app/snapshots.py`).
- **The Complexity Trap** ([arXiv:2508.21433](https://arxiv.org/abs/2508.21433)) and **Active Context Compression** ([arXiv:2601.07190](https://arxiv.org/abs/2601.07190)) — Threshold-based fresh-chat handoffs carrying compact git status, edited symbols, and recent errors (`scripts/handoff_pack.py`).

## Read, Write and Loop Guards

- **Sketch-of-Thought** ([arXiv:2503.05179](https://arxiv.org/abs/2503.05179)) — Opt-in concise reply rule (`mcp_app/rules_sync.py`).
- **TokenEconomizer** ([arXiv:2610.22732](https://arxiv.org/abs/2610.22732)) — Micro-skill injection, adapted as cross-session verified command promotion (`scripts/handoff_pack.py`).
- **To Diff or Not to Diff?** ([arXiv:2604.27296](https://arxiv.org/abs/2604.27296), the AdaEdit method) — Diff-style edits match full rewrites on accuracy at lower cost on long files; basis for the non-blocking full-file overwrite nudge (`scripts/write_hook.py`, `scripts/post_invocation_alerts.py`) and Max mode's targeted-edit rule (`mcp_app/rules_sync.py`).
- **When Agents Do Not Stop** ([arXiv:2607.01641](https://arxiv.org/abs/2607.01641), the IAL-Scan tool) — Studies unbounded agent loops in agent code; motivated the runtime 3x identical `grep_search` / `find_by_name` loop guard with 1-retry override (`scripts/hook_common.py`).
- **LogDx-CI** ([arXiv:2605.28876](https://arxiv.org/abs/2605.28876)) — Benchmarks CI log reduction for root-cause diagnosis (tail and grep+tail routers score well); basis for tail-anchoring unbounded `>46KB` log and spillback reads (`scripts/read_hook.py`).

## Open-Source Inspirations

- **`@plexer/cli`** — Session-scoped span dedup and post-edit refresh via `PreToolUse` / `PostToolUse` hooks; inspired edit-sequence invalidation (`scripts/post_edit_hook.py`), compaction-aware read dedup (`scripts/read_hook.py`), and `ts_inspect.py outline`.
- **`context-mode`** — In-memory SQLite FTS5 chunk search (`scripts/ts_inspect.py query`).
- **`squeez`** (hook-based tool-output compressor) — Exact-hash dedup of repeated tool output; inspired post-edit dirty-hunk read narrowing (`scripts/read_hook.py`).

## Why the guards stay lossless

- **Token Reduction Is Not Cost Reduction** ([arXiv:2607.12161](https://arxiv.org/abs/2607.12161)) — In Claude Code, aggressive tool-output compression cut tool tokens 38.4% but raised billed cost 6.8% (prompt-cache reads and writes dominate the bill, and compression added turns) and hurt task completion when it altered edit anchors or dropped evidence. This is why the hooks never rewrite shell output and only block or narrow reads whose content is provably already in context.
- **The Complexity Trap** ([arXiv:2508.21433](https://arxiv.org/abs/2508.21433)) — Observation tokens are ~84% of an agent turn, so duplicate reads are the cheapest safe target.
