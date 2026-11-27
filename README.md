# agy-tokensaver

<p align="left">
  <a href="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml"><img src="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml/badge.svg" alt="CI"/></a>
  <a href="https://github.com/menscho/agy-tokensaver/releases"><img src="https://img.shields.io/badge/release-v1.5.1-4ade80?style=flat-square&labelColor=0d0d11" alt="Release v1.5.1"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8e8e98?style=flat-square&labelColor=0d0d11" alt="MIT"/></a>
</p>

A Google Antigravity plugin that cuts token use in long agent sessions. When a chat gets too long, or its prompt cache has expired, it moves the work into a fresh chat that starts from a short handoff summary (typically 300–550 tokens) instead of the full history. It also adds lightweight guards that stop the agent from reading lockfiles, binaries and files it already has in context.

Requires Google Antigravity and Python 3.9 or newer. Uses only the Python standard library; there is nothing else to install.

## What it does

- **Auto-Handoff.** Before each of your messages, a hook checks the chat's size and cache state. When the chat passes an adaptive size threshold (75k–220k tokens, tuned from your past sessions) or you return after its prompt cache has expired, it opens a continuation chat, sends your message there along with a handoff packet (goal, last state, git changes, edited files and their symbols, recent errors, verified test commands), and archives the old chat.
- **Read guards.** Blocks reads of lockfiles, binaries, minified or generated files and SQLite databases, and points the agent to a better tool. Blocks re-reads of unchanged files that are still in context, or narrows them to the lines not yet seen. Caps unbounded reads of very large files and logs.
- **Write guard.** Blocks full-file rewrites that would silently drop code (for example a `// ... existing code ...` placeholder in place of real code), and nudges the agent toward `edit_file` after a large rewrite.
- **Search guards.** Excludes build and dependency folders from `find_by_name`, and blocks a third identical `grep_search` or `find_by_name` within a few steps when nothing was edited in between. Retrying once overrides the block.
- **Dashboard and skills.** An **agy-tokensaver** panel in the Antigravity sidebar shows savings, cache hit rate and handoff history. Three skills: `/token-stats`, `/handoff` and `/context-audit`.

The default mode adds no rules to the prompt, and shell commands and their output are never changed.

## Results

All numbers were measured on v1.5.0 with `eval/runner.py` and `scripts/benchmark.py`. Full tables are in [docs/perf.md](docs/perf.md). "Cost" below is a weighted proxy: input tokens ×1, cached input ×0.05, output ×6.

| Test | Setup | Result |
| :--- | :--- | :--- |
| Long-session A/B | 18 six-turn sessions on `gemini-4-hydrogen-high`, handing off after turn 3 | Same pass rate (9/9 vs 9/9). Total tokens −31.9% (18.25M → 12.43M), peak context 136k → 75k. Cost +2.8%, because these turns ran back to back with 92% cache hits. |
| Hard multi-module tasks | 54 runs on `gemini-4-hydrogen-xhigh` | Off 18/18. Default 17/18 (−15.7% tokens, −1.6% cost). Max 18/18 (−24.2% tokens, −9.0% cost). |
| Replay of real sessions | 86 local sessions, 4,688 turns | Default: an estimated 16.3% lower cost with delegated handoffs and 20.9% with direct handoffs (24.1% and 29.6% for sessions above 400k). Guards alone: 0.6% (254,916 tool tokens cut). |

The A/B result is why Auto-Handoff weighs cache state and not just size: splitting a chat whose cache is still warm can cost more than it saves, while a fresh start pays off for very long chats or after the cache has expired.

The replay numbers are estimates. They assume the work after a handoff would have gone the same way, and charge 6k tokens per handoff for re-reading files.

**Direct and delegated handoffs.** When the hook can reach Antigravity's local language server (`ANTIGRAVITY_LS_ADDRESS` is set), it opens the new chat itself and the old chat spends no tokens. This is a *direct* handoff. Otherwise the agent runs one `run_command` in the old chat to open it, which costs one extra turn there. This is a *delegated* handoff.

## Install and remove

Download `agy-tokensaver-v1.5.1.zip` from [Releases](https://github.com/menscho/agy-tokensaver/releases), extract it, and run the installer for your system:

- **Windows:** double-click `Install-TokenSaver.bat`
- **macOS:** double-click `Install-TokenSaver.command`
- **Linux or any terminal:** `python3 install.py` (use `python` instead of `python3` on Windows)

Restart Antigravity and open **agy-tokensaver** in the sidebar.

| Command | What it does |
| :--- | :--- |
| `python3 install.py --remote` | Downloads and installs the latest release from GitHub instead of using the extracted files. |
| `python3 install.py --doctor` | Checks Python, plugin paths, the data folder and hook registration. |
| `python3 install.py --uninstall` | Removes the plugin. Add `--purge-stats` to also delete saved statistics. |

## Modes

Pick a mode in the dashboard.

| Mode | Fresh chats | What it adds |
| :--- | :--- | :--- |
| **Auto-Handoff + Read Guards** (default) | Yes | All read, write and search guards. Adds nothing to the system prompt. |
| **Auto-Handoff + Compact Reads** | Yes | Tighter limits on large-file reads without a line range (120 lines of code, 80 lines of data). Downscales screenshots larger than 1568 px if ffmpeg, ImageMagick or macOS `sips` is available. |
| **Max** | Yes | Compact Reads plus two short rules in `rules/AGENTS.md`: reply concisely, and change existing files with targeted edits instead of full rewrites. The rules file is only written once the current prompt cache has cooled, so switching modes never invalidates a warm cache. |
| **Guards Only** | No | Keeps one chat. Saves handoff snapshots to the archive without switching chats. |
| **Guards + Compact Reads** | No | Guards Only with the Compact Reads limits. |
| **Off** | No | Every hook allows every call. |

## Safety and privacy

- **Shell commands are never rewritten.** `run_command` calls and their output pass through untouched.
- **Fail-open hooks.** Every hook runs on the Python standard library (`python3 -S`), caps its input at 2 MB, and allows the call if its input is malformed, missing or too large (`bash tests/test_failopen.sh`).
- **Kill switch.** Choose **Off** in the dashboard or set `AGY_TS_HOOKS=off`.
- **No internet access from hooks.** To open a new chat, Auto-Handoff talks only to Antigravity's language server on localhost. Updates are checked and downloaded only when you ask, verified against `SHA256SUMS.txt`, and also checked with `gh attestation verify` if the GitHub CLI is installed. Every network call is listed in [SECURITY.md](SECURITY.md).
- **Local data.** Statistics, settings and archived handoffs stay in `~/.gemini/antigravity/plugin_data/token-saver/`.

## Docs

- [Performance and benchmarks](docs/perf.md)
- [Architecture](docs/architecture.md)
- [Research references](docs/research.md)
- [Live evaluation harness](eval/README.md)
- [Security](SECURITY.md)
- [Changelog](CHANGELOG.md)
- [License (MIT)](LICENSE)
