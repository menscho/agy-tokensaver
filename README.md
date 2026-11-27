# agy-tokensaver

<p align="left">
  <a href="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml"><img src="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml/badge.svg" alt="CI"/></a>
  <a href="https://github.com/menscho/agy-tokensaver/releases"><img src="https://img.shields.io/badge/release-v1.5.2-4ade80?style=flat-square&labelColor=0d0d11" alt="Release v1.5.2"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8e8e98?style=flat-square&labelColor=0d0d11" alt="MIT"/></a>
</p>

A zero-dependency Google Antigravity plugin that cuts token quota, reduces wall-clock latency, and prevents context bloat across both short and long agent sessions. It combines **Auto-Handoff** for long or cold-cache chats, **5 eager-loaded batched local MCP accelerator tools** (`batch_search`, `apply_edits`, `quick_outline`, `read_slices`, `run_checks`), a **sub-50 ms `SessionStart` workspace snapshot**, and **lightweight read/write/search guards**.

Requires Google Antigravity and Python 3.9 or newer. Uses only the Python standard library; there is nothing else to install. Optional: [`ripgrep` (`rg`)](https://github.com/BurntSushi/ripgrep) speeds up `batch_search` (automatically falls back to pure Python when absent).

> **Engineering Attribution:** The 5 batched local MCP accelerator tools, sub-50 ms `SessionStart` workspace snapshot, `≤ 8.8 KB` zero-spill water-filling & head-tail failure folding, direct `--cli` Swarm mode, and hybrid tool-routing architecture are adapted from engineering pioneered by [**INK-Q/agy-fast**](https://github.com/INK-Q/agy-fast) (MIT License), natively re-engineered in pure zero-dependency Python 3.9+ stdlib alongside `agy-tokensaver`'s Auto-Handoff and PreToolUse guards.

## What it does

- **Auto-Handoff.** Before each of your messages, a hook checks the chat's size and cache state. When the chat passes an adaptive size threshold (75k–220k tokens, tuned from your past sessions) or you return after its prompt cache has expired, it opens a continuation chat, sends your message there along with a compact handoff packet (goal, last state, git changes, edited files and their symbols, recent errors, verified test commands), and archives the old chat.
- **5 Eager Batched Local MCP Accelerator Tools.** Registered as `"eager": true` in `mcp_config.json` (0 lazy-load schema-read turns) with a strict **`≤ 8,800` UTF-8 byte inline cap** so responses never spill to `output.txt` disk files:
  - **`batch_search`** — Run up to **20 concurrent mixed queries** (`text`, `regex`, `glob`, `fuzzy`, and hardened read-only `git`) in one call, using `rg` with automatic pure-Python fallback, `fileType`/`glob` filters, `multiline`, `outputMode` (`content` | `files_with_matches` | `count`), `sortBy` (`path` | `mtime`), and glob auto-recovery (`|` alternation & bare keywords).
  - **`apply_edits`** — Atomic multi-hunk & multi-file edits across up to **50 files** in one call, with in-memory overlap validation, `bestEffort: true` atomic-per-file partial commits, closest-line / line-drift mismatch snippets, `.git` internal write protection, UTF-8 BOM / CRLF / mixed-EOL / permission / symlink preservation, and optional inline **`verify`** with noisy-log stripping & head-tail error compaction. Accepts both native arrays and JSON array strings (auto-adapts to pre-eager CLI 1.5.0–1.5.7).
  - **`quick_outline`** — Flat `paths`-only multi-language symbol/section line-range outlines across up to **40 files** (TS/JS incl. Express/Fastify routes & test suites, Python, Godot GDScript, Go, Rust, Java/Kotlin/C#/Swift/C/C++, HTML/Vue/Svelte with embedded `<script>`/`<style>` parsing, CSS/SCSS/Less, Shell, Markdown, JSON/YAML) with breadth-first water-filling before parallel native `read_file`.
  - **`read_slices`** — Batch symbol outlines plus optional tiny targeted line ranges (`slices` or flat `lineRanges` `"path:start-end,s2-e2"` shorthand) while steering bulk reads (`>150` lines) to parallel native `read_file`.
  - **`run_checks`** — Execute up to **12 independent non-interactive build/lint/test commands concurrently** (`concurrency: 1..8`, shared `150 s` call budget under Antigravity's `180 s` limit), with ANSI/spinner normalization, Windows `powershell -Command` trivial wrapper unwrapping, green-phase pass collapsing, Pino/Bunyan/Maven noise stripping, and Head-Tail folding in `compact` (default) and `full` modes.
- **Sub-50 ms `SessionStart` Workspace Snapshot.** Runs once at session startup (`250 ms` scan circuit-breaker, `800 ms` git timeout, OS/`$HOME` skip list, nested repo support) to inject a `≤ 1.5 KB` snapshot of git branch/status/recent commits, multi-module project manifests (`package.json`, `Makefile`, `pom.xml`, `build.gradle`, `pyproject.toml`, `Cargo.toml`, `go.mod`), and a 2-level priority-sorted directory layout—eliminating cold-start exploration turns.
- **Direct CLI Mode for Swarm Workers.** Subagents/Swarm workers without MCP mounted can invoke every accelerator directly via `python3 <plugin>/mcp_app/server.py --cli <search|outline|slice|edit|check>` (supports `--json`, `--stdin`, `--cwd`, `--max-chars`, and shorthand flags).
- **Read, Write & Search Guards.** Blocks reads of lockfiles, binaries, minified/generated files, and SQLite databases; blocks re-reads of unchanged files still in context or narrows them to unseen lines; caps unbounded reads of huge files and logs; blocks lazy full-file placeholder rewrites; excludes build/dependency folders from `find_by_name` and stops redundant identical search loops.
- **Dashboard and Skills.** An **agy-tokensaver** panel in the Antigravity sidebar shows savings, cache hit rate, Startup Snapshot toggle, and handoff archive. Three skills: `/token-stats`, `/handoff`, and `/context-audit`.

The default mode adds no `rules/AGENTS.md` file to the prompt, and native `run_command` calls and their output are never rewritten.

## The 5 Batched MCP Tools

| Scenario | Preferred Tool | Why |
| :--- | :--- | :--- |
| Run ≥2 text, regex, glob, fuzzy, or read-only `git` queries | **`batch_search`** | 1 call for up to 20 concurrent queries, water-filled `≤ 8.8 KB` inline |
| Map unknown large files (`>300` lines) | **`quick_outline`** *(or **`read_slices`**)* | Flat `paths`-only symbol outlines across up to 40 files without full-file dumps |
| Read small files (`<300` lines) or known line ranges | Built-in `read_file` | Locally allowlisted (`~7 ms`, `46 KB/call`, 0 remote check) |
| Edit ≥2 hunks/files in one turn, or edit + verify | **`apply_edits`** | Atomic or `bestEffort` multi-file edits + optional inline `verify` |
| Run ≥2 independent build, lint, test, or diagnostic commands | **`run_checks`** | Concurrent execution with compact pass summaries & head-tail folded failures |

## Results

### Realistic Quota & Speed Simulation (v1.5.2)

To estimate end-to-end quota and wall-clock impact when combining the v1.5.2 batched MCP accelerators, `SessionStart` snapshot, and Auto-Handoff, we measured real tool payloads and local execution timings directly on this repository (`SessionStart`: 342 tok in 12 ms vs 1,039 tok baseline; `batch_search`: 5 queries in 35 ms; `quick_outline` + targeted slices on 3 large files: 2,069 tok in 55 ms vs 30,023 tok full reads, **−93.1%**; `apply_edits` + `verify` across 3 files: 76 tok in 28 ms; `run_checks` running `ruff` + `mypy` + `unittest` in parallel: 91 tok vs 1,193 tok raw output, **−92.4%**) and fed them into an exact cache-accounting turn simulator (`1×` uncached input, `0.05×` cached input, `6×` output; `16k` base system prompt + `850` tok eager tool schemas; every newly appended turn/tool payload billed 100% uncached on entry; `92%` warm prefix cache hit; `4.5 s` average LLM round-trip latency per turn):

| Simulated Workflow | LLM Turns | Peak Context | Total Tokens | Est. Quota Saved | Est. Wall-Clock Speedup |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **Quick Bugfix** *(warm cache)* | 8 → 4 *(−50%)* | 32.2k → 20.6k | −62.7% | **−46.5%** | **1.87× faster** *(39.0 s → 20.8 s)* |
| **Cross-Module Feature** *(conservative parallel baseline)* | 9 → 6 *(−33%)* | 53.2k → 22.9k | −65.8% | **−54.7%** | **1.46× faster** *(43.5 s → 29.9 s)* |
| **Cross-Module Feature** *(standard sequential baseline)* | 14 → 6 *(−57%)* | 54.5k → 22.9k | −76.6% | **−64.4%** | **2.21× faster** *(66.0 s → 29.9 s)* |
| **Long 3-Task Session** *(1 idle cache drop + Auto-Handoff)* | 42 → 18 *(−57%)* | 132.2k → 29.4k | −86.1% | **−77.8%** | **2.21× faster** *(198.2 s → 89.6 s)* |

**Why both quota and speed improve together:**
- **Fewer LLM round-trips (33–57% fewer turns):** Collapsing cold-start discovery (`SessionStart`), multi-query searches (`batch_search`), multi-file edits + verify (`apply_edits`), and concurrent checks (`run_checks`) eliminates intermediate model round-trips—saving both **3–5 s of LLM latency per avoided turn** and **6×-weighted reasoning/tool-call output tokens**.
- **Much smaller uncached tool payloads:** Outlining large files with `quick_outline` before targeted slices cuts initial read payload by **~93%**, and keeping every MCP response `≤ 8.8 KB` eliminates Antigravity's `>10 KB` `output.txt` disk-spill follow-up turn.
- **Compounding gains on long sessions:** Keeping context ~2–4× smaller delays cache cliffs, and Auto-Handoff resets context cleanly when you return from an idle cache drop or exceed the adaptive threshold.

*Simulation note:* Real-world quota savings depend on how aggressively your model batches calls, your codebase file sizes, and prompt cache hit rates (even with a fast 3.0 s/turn model and a baseline that already parallelizes native reads/greps, the conservative feature simulation still cuts **−54.7% quota** and runs **1.44× faster**).

### Live Benchmarks & Session Replay (Auto-Handoff + Guards)

All Auto-Handoff and Guard numbers below were measured on v1.5.0 with `eval/runner.py` and `scripts/benchmark.py`. Full tables are in [docs/perf.md](docs/perf.md). "Cost" is the same weighted quota proxy (`1×` input, `0.05×` cached input, `6×` output).

| Test | Setup | Result |
| :--- | :--- | :--- |
| Long-session A/B | 18 six-turn sessions on `gemini-4-hydrogen-high`, handing off after turn 3 | Same pass rate (9/9 vs 9/9). Total tokens −31.9% (18.25M → 12.43M), peak context 136k → 75k. Cost +2.8%, because these turns ran back to back with 92% cache hits. |
| Hard multi-module tasks | 54 runs on `gemini-4-hydrogen-xhigh` | Off 18/18. Default 17/18 (−15.7% tokens, −1.6% cost). Max 18/18 (−24.2% tokens, −9.0% cost). |
| Replay of real sessions | 86 local sessions, 4,688 turns | Default: an estimated 16.3% lower cost with delegated handoffs and 20.9% with direct handoffs (24.1% and 29.6% for sessions above 400k). Guards alone: 0.6% (254,916 tool tokens cut). |
| Deterministic guard benchmark | `python3 scripts/benchmark.py --demo` (9 real hook/tool steps) | Direct tool tokens **−78.7%** (`57,933 → 12,315` tokens, `45,618` tokens avoided). |

The A/B result is why Auto-Handoff weighs cache state and not just size: splitting a chat whose cache is still warm can cost more than it saves, while a fresh start pays off for very long chats or after the cache has expired. The batched MCP tools and `SessionStart` snapshot complement Auto-Handoff by cutting turn count, cold-start latency, and tool-return upload bytes *inside* every chat before a handoff is ever needed.

The replay numbers are estimates. They assume the work after a handoff would have gone the same way, and charge 6k tokens per handoff for re-reading files.

**Direct and delegated handoffs.** When the hook can reach Antigravity's local language server (`ANTIGRAVITY_LS_ADDRESS` is set), it opens the new chat itself and the old chat spends no tokens. This is a *direct* handoff. Otherwise the agent runs one `run_command` in the old chat to open it, which costs one extra turn there. This is a *delegated* handoff.

## Install and remove

Download `agy-tokensaver-v1.5.2.zip` from [Releases](https://github.com/menscho/agy-tokensaver/releases), extract it, and run the installer for your system:

- **Windows:** double-click `Install-TokenSaver.bat`
- **macOS:** double-click `Install-TokenSaver.command`
- **Linux or any terminal:** `python3 install.py` (use `python` instead of `python3` on Windows)

Restart Antigravity and open **agy-tokensaver** in the sidebar.

| Command | What it does |
| :--- | :--- |
| `python3 install.py --remote` | Downloads and installs the latest release from GitHub instead of using the extracted files. |
| `python3 install.py --doctor` | Checks Python, plugin paths, data folder, hook registration, `rg` availability, and `SessionStart` hook latency. |
| `python3 install.py --uninstall` | Removes the plugin. Add `--purge-stats` to also delete saved statistics. |
| `python3 mcp_app/server.py --cli --help` | Direct CLI runner for Swarm workers and scripts (`search`, `outline`, `slice`, `edit`, `check`). |

### Optional Environment Overrides

| Environment Variable | Values / Default | Purpose |
| :--- | :--- | :--- |
| `AGY_TS_HOOKS` | `on` *(default)* \| `off` | Global kill switch for all hooks (including `SessionStart`). |
| `AGY_TS_STARTUP_SNAPSHOT` | `on` *(default)* \| `off` | Toggle `SessionStart` workspace snapshot independently. |
| `AGY_TS_TOOLS` | Comma-separated subset *(default: all 5)* | Expose only specific accelerator tools (`batch_search,apply_edits,quick_outline,read_slices,run_checks`; also `--tools`). |
| `AGY_TS_SCHEMA_COMPAT` | `standard` *(default)* \| `minimal` | Strip JSON Schema range keywords for strict hosts (also `--schema-compat=minimal`). |
| `AGY_TS_EDITS_FORMAT` | `auto` *(default)* \| `array` \| `string` | Override `apply_edits.edits` schema (`auto` uses native arrays on eager hosts and JSON strings on pre-eager CLI 1.5.0–1.5.7). |

## Modes

Pick a mode in the dashboard.

| Mode | Fresh chats | What it adds |
| :--- | :--- | :--- |
| **Auto-Handoff + Read Guards** (default) | Yes | All read, write and search guards, `SessionStart` snapshot, and 5 eager batched MCP tools. Adds no `rules/AGENTS.md` to the system prompt. |
| **Auto-Handoff + Compact Reads** | Yes | Tighter limits on large-file reads without a line range (120 lines of code, 80 lines of data). Downscales screenshots larger than 1568 px if ffmpeg, ImageMagick or macOS `sips` is available. |
| **Max** | Yes | Compact Reads plus concise-reply, hybrid batching, targeted-edit, and multimodal/cache hygiene rules in `rules/AGENTS.md` (written only once the current prompt cache has cooled; user-owned `rules/AGENTS.local.md` is preserved across updates). |
| **Guards Only** | No | Keeps one chat. Saves handoff snapshots to the archive without switching chats. |
| **Guards + Compact Reads** | No | Guards Only with the Compact Reads limits. |
| **Off** | No | Every hook (including `SessionStart`) passes through untouched. |

## Safety and privacy

- **Native shell commands are never rewritten.** `run_command` calls and their output pass through untouched.
- **Fail-open hooks.** Every hook runs on the Python standard library (`python3 -S`), caps its input, and allows the call (or returns `{}`) if its input is malformed, missing or too large (`bash tests/test_failopen.sh`).
- **Hardened local tools.** `batch_search` strictly blocks mutating `git` subcommands/flags and shell operators; `apply_edits` refuses writes inside `.git/` internal metadata and rolls back atomically on failure; `SessionStart` enforces a `250 ms` scan circuit-breaker and skips OS/home directories.
- **Kill switch.** Choose **Off** in the dashboard or set `AGY_TS_HOOKS=off`.
- **No internet access from hooks or MCP tools.** To open a new chat, Auto-Handoff talks only to Antigravity's language server on localhost. Updates are checked and downloaded only when you ask, verified against `SHA256SUMS.txt`, and also checked with `gh attestation verify` if the GitHub CLI is installed. Every network call is listed in [SECURITY.md](SECURITY.md).
- **Local data.** Statistics, settings and archived handoffs stay in `~/.gemini/antigravity/plugin_data/token-saver/`.

## Credits & Engineering Attribution

`agy-tokensaver` incorporates and adapts engineering architecture and algorithms from [**INK-Q/agy-fast**](https://github.com/INK-Q/agy-fast) (licensed under the MIT License), including:
- The 5-tool batched local MCP accelerator design (`batch_search`, `apply_edits`, `quick_outline`, `read_slices`, `run_checks`) with `"eager": true` registration and host/schema compatibility modes
- The `≤ 8,800` UTF-8 byte zero-spill inline cap, water-filling budget allocator, green build/test-pass phase folding, noisy structured-log stripping, and Head-Tail failure compaction
- Multi-language symbol outline extraction across 15+ languages with breadth-first water-filling
- Atomic & `bestEffort` multi-file edit planning with closest-region fuzzy mismatch hints, line-drift detection, mixed-EOL preservation, and `.git` write protection
- The sub-50 ms `≤ 1.5 KB` `SessionStart` workspace snapshot with `250 ms` circuit-breaker, nested-repo detection, and OS/home-directory skip lists
- Direct `--cli` execution mode for Swarm workers and hybrid tool-routing / multimodal cache-hygiene rules

All components are natively implemented in pure Python 3.9+ stdlib inside `agy-tokensaver` to preserve its zero-dependency runtime.

## Docs

- [Performance and benchmarks](docs/perf.md)
- [Architecture](docs/architecture.md)
- [Research references](docs/research.md)
- [Live evaluation harness](eval/README.md)
- [Security](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [License (MIT)](LICENSE)
