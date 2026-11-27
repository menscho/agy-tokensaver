# agy-tokensaver

<p align="left">
  <a href="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml"><img src="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml/badge.svg" alt="CI"/></a>
  <a href="https://github.com/menscho/agy-tokensaver/releases"><img src="https://img.shields.io/badge/release-v1.6.0-4ade80?style=flat-square&labelColor=0d0d11" alt="Release v1.6.0"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8e8e98?style=flat-square&labelColor=0d0d11" alt="MIT"/></a>
</p>

A Google Antigravity plugin that makes agent sessions cheaper and faster. It cuts the number of model turns, keeps junk out of the context window, and moves very long chats to a fresh one before they get expensive.

It needs Python 3.9 or newer and nothing else: no packages, no Node, no Perl. [`ripgrep`](https://github.com/BurntSushi/ripgrep) makes search faster if you have it.

## How it saves

The plugin works on five parts of the bill at once.

| Lever | What it does | Mainly helps |
| :--- | :--- | :--- |
| **Fewer turns** | Five batched tools let the agent do in one call what normally takes several: search 20 patterns, outline 40 files, edit 50 files, or run 12 checks. A startup snapshot of the repo (git state, project files, folder layout, under 1.5 KB) saves the usual `ls` / `git status` turns. | Speed and cost in every chat |
| **Smaller tool output** | Guards block reads of lockfiles, binaries and generated files, skip re-reads of files the agent already has, cap huge reads and logs, and fold long passing test output into a one-line summary. | Context size |
| **Shorter replies** | In Max mode, four short rules ask the agent to batch calls, edit files instead of rewriting them, and skip filler. Output tokens are 30–45% of the bill on recorded sessions. | Cost |
| **Fresh chats** | Auto-Handoff watches each chat's size and cache state. When a chat gets too long or its prompt cache has expired, it opens a new chat with a short handoff note (goal, current state, changed files, errors, test commands) and archives the old one. On Gemini 4 it never lets a chat grow past 180k, where cache misses climb sharply. | Cost on long chats |
| **Cache protection** | Knows how each Gemini 4 model keeps its prompt cache (Neon well, Carbon, Argon and Orchestrator poorly, per user reports) and tells you once when a long chat runs on a model that keeps losing it. `run_checks` returns within 90 s so long check batches don't let the cache go cold. | Cost and speed on Gemini 4 |

Native `run_command` calls and their output are never changed.

## Results

All numbers are measured unless marked as an estimate. Full tables and methods are in [docs/perf.md](docs/perf.md). "Cost" means weighted quota: uncached input ×1, cached input ×0.05, output ×6.

| Test | What was run | Result |
| :--- | :--- | :--- |
| **Hard multi-file tasks** | 6 tasks × 3 runs × 3 modes on `gemini-4-hydrogen-xhigh` | **Max: 18/18 passed, −24% tokens, −9.0% cost.** Default: 17/18, −16% tokens, −1.6% cost. Off: 18/18. |
| **Long six-turn sessions** | 18 sessions on `gemini-4-hydrogen-high`, handoff after turn 3 | Both 9/9. Tokens −32%, peak context 136k → 75k. Cost **+2.8%**, because back-to-back turns hit a warm cache. |
| **Replay of real sessions** (estimate) | 86 sessions, 4,688 turns, replayed through the real hooks | −16% to −21% cost overall; −24% to −30% for chats over 400k. Chats under 75k: about −0.2%. |
| **Replay, second machine** (estimate) | 30 shorter sessions, only one over 400k | About −1% cost. Guards alone: −0.7%. |
| **Tool output benchmark** | `python3 scripts/benchmark.py --demo`: 10 hook and tool calls on synthetic files | −81% tool tokens (66k → 12k). A worst case, not a typical session. |

**What this means for you:**

- **Long chats save the most.** Auto-Handoff does little below about 110k tokens and a lot above 200k.
- **Max mode is the best measured setting**, so new installs start in it. It is the only mode that cuts output tokens, which are 30–45% of the bill.
- **The guards are a small win.** They save under 1% of cost on real sessions. Their job is stopping waste such as lockfile dumps and repeat reads.
- **Handoffs can cost more when the cache is warm.** That is why Auto-Handoff weighs cache state and not just size.
- **The batched tools have not had a live A/B yet.** The benchmark above measures their output size, not how many turns they save. A model of the turn savings is in [docs/perf.md](docs/perf.md#7-modelled-estimate-for-the-batched-tools-not-measured), clearly marked as a model.

Each guarded tool call adds one hook run: 50–90 ms in CI (Linux, macOS, Windows) and up to about 150 ms on a slower Windows PC. Most of that is Python starting up.

## Install and remove

Download `agy-tokensaver-v1.6.0.zip` from [Releases](https://github.com/menscho/agy-tokensaver/releases), extract it, and run the installer:

- **Windows:** double-click `Install-TokenSaver.bat`
- **macOS:** double-click `Install-TokenSaver.command`
- **Linux or any terminal:** `python3 install.py` (`python` on Windows)

Restart Antigravity and open **agy-tokensaver** in the sidebar.

| Command | What it does |
| :--- | :--- |
| `python3 install.py --remote` | Downloads and installs the latest release instead of the extracted files. |
| `python3 install.py --doctor` | Checks Python, plugin paths, the data folder, hook registration, `rg`, and startup hook speed. |
| `python3 install.py --uninstall` | Removes the plugin. Add `--purge-stats` to delete saved statistics too. |
| `python3 mcp_app/server.py --cli --help` | Runs the batched tools from a terminal or a subagent without MCP. |

## Modes

Pick a mode in the sidebar. New installs start in **Max**. Updates keep the mode you already use.

| Mode | New chats | What it does |
| :--- | :--- | :--- |
| **Max** (recommended) | Yes | Everything below, plus compact reads and four rules in `rules/AGENTS.md`: batch tool calls, edit instead of rewrite, reply concisely, and keep images out of very long chats. The rules file is only written when it won't break a warm prompt cache. Your own `rules/AGENTS.local.md` is kept across updates. |
| **Auto-Handoff + Read Guards** | Yes | Auto-Handoff, all guards, the startup snapshot and the batched tools. Adds nothing to the system prompt. |
| **Auto-Handoff + Compact Reads** | Yes | As above, with tighter caps on large reads without a line range (120 lines of code, 80 lines of data). Shrinks screenshots over 1568 px if ffmpeg, ImageMagick or macOS `sips` is installed. |
| **Guards Only** | No | Keeps one chat. Saves handoff notes to the archive without switching chats. |
| **Guards + Compact Reads** | No | Guards Only with the compact read caps. |
| **Off** | No | Every hook passes calls through untouched. |

## The batched tools

The tools load with the plugin, so the agent doesn't spend a turn discovering them. Every response stays under 8.8 KB, below the size at which Antigravity moves tool output to a file the agent has to read in another turn.

| Tool | Use it to | Instead of |
| :--- | :--- | :--- |
| `batch_search` | Run up to 20 text, regex, glob, fuzzy or read-only `git` searches at once | Several `grep_search` / `find_by_name` turns |
| `quick_outline` | Get the functions, classes and sections of up to 40 files, in 15+ languages | Reading whole large files to find one function |
| `read_slices` | Read outlines plus small line ranges from many files | Many small `read_file` calls |
| `apply_edits` | Edit up to 50 files in one call, all or nothing, then run a check | One edit call per file, then a separate test run |
| `run_checks` | Run up to 12 build, lint or test commands in parallel, with passing output folded | Running checks one by one and reading their full logs |

Small files and known line ranges are still read with the built-in `read_file`, which is faster for that.

### Settings

| Environment variable | Values | Purpose |
| :--- | :--- | :--- |
| `AGY_TS_HOOKS` | `on` (default) \| `off` | Turns all hooks off at once. |
| `AGY_TS_STARTUP_SNAPSHOT` | `on` (default) \| `off` | Turns the startup snapshot off on its own. |
| `AGY_TS_TOOLS` | comma-separated list (default: all 5) | Exposes only some batched tools. |
| `AGY_TS_SCHEMA_COMPAT` | `standard` (default) \| `minimal` | Strips range keywords from tool schemas for strict hosts. |
| `AGY_TS_EDITS_FORMAT` | `auto` (default) \| `array` \| `string` | Forces the `apply_edits` input format for older Antigravity CLI versions (1.5.0–1.5.7). |

## Safety and privacy

- **Hooks fail open.** If a hook gets input it can't read, or input that is too large, it lets the call through unchanged (`bash tests/test_failopen.sh`).
- **Shell commands are never rewritten.**
- **Batched tools are guarded.** `batch_search` refuses `git` commands that change anything. `apply_edits` refuses to write inside `.git/` and rolls every file back if one edit fails.
- **No internet access from hooks or tools.** Auto-Handoff talks only to Antigravity on localhost. Updates are downloaded only when you ask, and checked against `SHA256SUMS.txt` and, if the GitHub CLI is installed, GitHub's build attestations. Every network call is listed in [SECURITY.md](SECURITY.md).
- **Your data stays local** in `~/.gemini/antigravity/plugin_data/token-saver/`.
- **Off switch:** choose **Off** in the sidebar or set `AGY_TS_HOOKS=off`.

## Credits

The five batched tools, the startup snapshot and the output folding are a Python port of designs from [INK-Q/agy-fast](https://github.com/INK-Q/agy-fast) (MIT). Its license is in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Docs

- [Performance and benchmarks](docs/perf.md)
- [Architecture](docs/architecture.md)
- [Research references](docs/research.md)
- [Live evaluation harness](eval/README.md)
- [Security](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [License (MIT)](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md)
