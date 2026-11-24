# Changelog

All notable changes to **agy-tokensaver** (`menscho/agy-tokensaver`) are documented here.

## [Unreleased]

### Changed
- **Structured `JSON::PP` hook parsing (`scripts/lib/TsJson.pm`):** replaced regex JSON extraction across all Perl hooks with core `JSON::PP`, added a 2 MB fail-open payload cap, fixed uninitialized loop-counter variables under `use warnings`, and recorded latency measurements in `docs/perf.md` (before `8.9 ms` → after `17.1 ms` median on 8 hook hits; fast-path misses stay `~2.5 ms`).

## [1.1.0] — 2026-11-24

### Defaults & Modes
- **Lossless Mode 1 is the default:** ships with lossless hooks and `Archive Only` handoffs enabled out of the box. Heavier optimizations (`Mode 2a` Compact Reads, `Mode 2b` + Concise Output, `Mode 2c` + Auto-Handoff, `Mode 2 Max`) are opt-in via one dropdown in the **Token Saver** HUD.
- **Cache-safe deferred rule sync (`mcp_app/core.py`):** toggling Concise Output while an active `>=30k` chat has a warm prompt cache defers writing `rules/AGENTS.md` until the cache cools or rolls over, avoiding mid-session prefix invalidation.
- **Snapshot vs Auto-Handoff state separation:** tracks `snapshotted_convs` separately from `rolled_convs` so switching an already-snapshotted session to Auto-Handoff works immediately.

### Hooks & Compression
- **Output compressors (`scripts/cmd_wrap.pl`, `cmd_hook_core.pl`):**
  - Lossless JSON `[N]{cols}` schema+CSV compaction (1-level flattening, `>256 B` blob offload, error-preserving sampling past 9.9 KB).
  - Non-adjacent `>=3`-line / `>=160 B` exact chunk deduplication in logs/SSH output, repeated multi-line block folding, dense machine-generated line elision, and `±2` context trimming on large diffs.
  - Compiler error grouping on failed builds (`tsc`/`cargo`/`rustc`), inline `style=""` stripping on large HTML responses, `NO_COLOR=1`/`CLICOLOR=0`/`GIT_PAGER=cat` env injection, and `ids_preserved` spill footers for omitted UUIDs/`ERR_*`/URLs.
  - Windows PowerShell wrapping (`AGY_TS_SHELL=off|posix|pwsh`), multi-command SSH diagnostic wrapping, `ps -eo` / `ls && find` / setup-step chains up to 16 segments, and deterministic content-addressed FNV-1a spill filenames.
- **File read & edit guards (`scripts/read_hook.pl`, `post_edit_hook.pl`, `write_hook.pl`):**
  - Compaction-verified duplicate-read window with MD5 fingerprint and missing-range trim for partially overlapping re-reads; `PostToolUse` edit tracking immediately invalidates edited entries.
  - Tail-anchored unbounded `.log`/`.err`/`.out`/spill reads, post-edit dirty-hunk narrowing on modified files (`<=64 KB`), leading SPDX/copyright header skip, `@generated`/`DO NOT EDIT` gate for unbounded `>24 KB` files, duplicate image read blocking, and opt-in Mode 2 downscaling for `>1568px` screenshots.
  - Non-blocking alert when `write_file` overwrites `>120L` with `>=75%` unchanged lines.
- **Loop & search guards (`scripts/grep_hook.pl`, `find_hook.pl`, `board_hook.pl`, `cmd_hook_core.pl`, `swarm_hook.pl`):**
  - 3× identical failure/query/board-poll loop breaker with a 1-retry escape hatch (cleared on edits).
  - Single-file Windows `grep_search` fix via parent dir + `Includes`; `WaitMsBeforeAsync` floor bump (`5000 ms`) on fast read-only queries.

### Handoffs, CLI & Benchmarking
- **Richer handoff packets (`scripts/handoff_pack.py`):** includes edited-file `L<start>-L<end>` symbol outlines, recent failed commands, and cross-session verified repo commands (`>=2` runs).
- **Cold-cache + causal Auto-Handoff (`mcp_app/core.py`):** unified causal `rollover_policy()` shared by the live tick, HUD, and simulator (no oracle), settled-only firing, and cold-cache rollover for `>=75k` chats idle past the local TTL cliff (up to 30 min).
- **Inspection & auditing (`scripts/ts_inspect.py`, `scripts/audit.py`):** added `ts_inspect.py outline` and SQLite FTS5 `ts_inspect.py query` for spill files; added `audit.py --discover` and dead-MCP / dynamic-date rule checks.
- **Real-session replay benchmark (`scripts/transcript_replay.py`, `scripts/benchmark.py`, `scripts/make_benchmark_chart.py`):** replays recorded IDE + CLI sessions through the real hooks with exact per-turn tokens, charges retries and the plugin's `392 tok/turn` prompt overhead, clearly separates measured replay rows from estimated concise-output rows, and removes the redundant `1.7×` demo quota row.
- **HUD fixes (`mcp_app/core.py`, `mcp_app/server.py`, `mcp_app/dashboard.html`):** alphabetically sorted MCP `tools/list` + `sort_keys=True`, fixed the output comparison row, added idle-vs-midrun cache-drop breakdown, and added 10-minute timeouts to GitHub Actions CI jobs.
- **Reduced prompt overhead:** trimmed rules, skills, and scouts from `906` to `392 tok/turn`.

## [1.0.0] — Initial Open-Source Release

- **Fail-Open Perl Hooks (`PreToolUse` / `PostInvocation` / `Stop`):** `cmd_hook.pl`, `cmd_hook_core.pl`, `cmd_wrap.pl`, `read_hook.pl`, `write_hook.pl`, `find_hook.pl`, `swarm_hook.pl`, `post_invocation_alerts.pl`, `stop_telemetry.pl`.
- **Native Full-Page HUD (`mcp_app/`):** Global Antigravity MCP App (`agy-tokensaver`) with `Overview`, `Archive`, and `Updates` tabs.
- **Archive & Optional Rollover (`scripts/handoff_pack.py`, `mcp_app/core.py`):** Defaults to `Archive Only`, extracts workspace git state + touched files + recent context into a compact continuation prompt.
- **Tagged, SHA-256 Verified Installer & Updater (`install.py`, `Updates` tab):** Installs and updates only from tagged GitHub Releases verified against `SHA256SUMS.txt`.
- **Reproducible Benchmarks & 3-OS CI (`scripts/benchmark.py`, `.github/workflows/test.yml`):** Deterministic before/after hook benchmark, transcript counterfactual replay, and regression tests on Ubuntu, macOS, and Windows.
