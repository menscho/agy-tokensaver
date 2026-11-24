# Research & Open-Source References

Every citation below maps directly to a shipped feature and file in this repository.

## Prompt Caching & Prefix Stability

- **Don't Break the Cache** ([arXiv:2601.06007](https://arxiv.org/abs/2601.06007)) — Informed deterministic 12-hex FNV-1a spill filenames (`scripts/lib/TsSpill.pl`), alphabetically sorted MCP `tools/list` (`mcp_app/server.py`), and cache-safe deferred `rules/AGENTS.md` writes (`mcp_app/rules_sync.py`).
- **Keeping the Cache Warm** ([arXiv:2607.19214](https://arxiv.org/abs/2607.19214)) — Informed empirical idle TTL-cliff tracking (`90–150s`) and cold-cache rollover payback triggers (`mcp_app/rollover.py`, `mcp_app/snapshots.py`).
- **Persistent Context Graphs for Efficient Memory Compaction in LLM Agents** ([arXiv:2609.40118](https://arxiv.org/abs/2609.40118)) — Informed the 30-minute cold-cache auto-handoff window (`<= 1800s`) for settled `>= 75k` chats (`mcp_app/rollover.py`, `mcp_app/snapshots.py`).
- **Models Take Notes at Prefill** ([arXiv:2606.17107](https://arxiv.org/abs/2606.17107)) — Informed swarm coordinator `< 90s` pacing (`scripts/swarm_hook.pl`) and the 3× identical `read_board` poll-guard (`scripts/board_hook.pl`).

## Context Resets & Handoffs

- **The Complexity Trap** ([arXiv:2508.21433](https://arxiv.org/abs/2508.21433)) — Informed threshold-based chat handoffs (`mcp_app/rollover.py`) instead of unbounded context growth.
- **Active Context Compression** ([arXiv:2601.07190](https://arxiv.org/abs/2601.07190)) — Informed compact anchored handoff packets carrying git state, edited files, symbol outlines, and ruled-out errors (`scripts/handoff_pack.py`).

## Output Compression, Reads & Loop Control

- **Sketch-of-Thought** ([arXiv:2503.05179](https://arxiv.org/abs/2503.05179)) — Informed opt-in `Concise Output` reply rules in Mode 2b / Max (`mcp_app/rules_sync.py`).
- **Merlin** ([arXiv:2605.09990](https://arxiv.org/abs/2605.09990)) — Informed byte-exact non-adjacent `>= 3`-line / `>= 160 B` chunk deduplication (`merlin_dedupe_chunks` in `scripts/compressors/common.pl`).
- **TokenEconomizer** ([arXiv:2610.22732](https://arxiv.org/abs/2610.22732)) — Informed the 3× identical `test`/`build`/`lint` failure-loop breaker (`scripts/cmd_hook_core.pl`) and cross-session verified repo command promotion (`scripts/handoff_pack.py`).
- **AdaEdit** ([arXiv:2604.27296](https://arxiv.org/abs/2604.27296)) — Informed the non-blocking `> 120L` full-file overwrite UI nudge (`scripts/write_hook.pl`, `scripts/post_invocation_alerts.pl`).
- **SkillReducer** ([arXiv:2603.29919](https://arxiv.org/abs/2603.29919)) — Informed cutting always-loaded prompt overhead from `906` to `190 tok/turn` (`rules/AGENTS.md`, `skills/*/SKILL.md`, `agents/*.md`).
- **IAL-Scan** ([arXiv:2607.01641](https://arxiv.org/abs/2607.01641)) — Informed universal 3× identical `grep_search` and `find_by_name` loop guards with 1-retry escape hatches (`scripts/grep_hook.pl`, `scripts/find_hook.pl`).
- **LogDx-CI** ([arXiv:2605.28876](https://arxiv.org/abs/2605.28876)) — Informed tail-anchoring unbounded `.log`/`.err`/`.out` and spillback reads (`scripts/lib/read_dedupe.pl`).

## Open-Source Projects

- **RTK** — Inspired `NO_COLOR=1`/`GIT_PAGER=cat` injection, compiler diagnostic grouping (`scripts/compressors/build.pl`), and `scripts/audit.py --discover`.
- **Headroom** — Inspired JSON `[N]{cols}` schema+CSV compaction and outlier-preserving sampling (`scripts/compressors/json.pl`).
- **`@plexer/cli`** — Inspired `PostToolUse` edit-sequence invalidation (`scripts/post_edit_hook.pl`), `initialNumSteps` compaction resets (`scripts/lib/read_dedupe.pl`), and `ts_inspect.py outline`.
- **`context-mode` & `squeez`** — Inspired SQLite FTS5 BM25 spill search (`scripts/ts_inspect.py query`), `ids_preserved` spill footers (`scripts/cmd_wrap.pl`), and post-edit dirty-hunk read narrowing (`scripts/lib/read_dedupe.pl`).
