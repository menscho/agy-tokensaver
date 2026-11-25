# agy-tokensaver

<p align="left">
  <a href="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml"><img src="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml/badge.svg" alt="CI"/></a>
  <a href="https://github.com/menscho/agy-tokensaver/releases"><img src="https://img.shields.io/badge/release-v1.2.3-4ade80?style=flat-square&labelColor=0d0d11" alt="Release v1.2.3"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8e8e98?style=flat-square&labelColor=0d0d11" alt="MIT"/></a>
</p>

Saves ~19% on long Google Antigravity sessions by rolling overgrown or cold-cache chats into fresh sessions with a compact handoff summary; expect ~0% savings on short chats. Also blocks lockfiles, binaries, and duplicate file reads without touching your shell commands.

## How well it works

Every number below comes from runnable commands in this repository (`scripts/benchmark.py --modes --report` and `eval/runner.py`).

- **Real sessions (79 local chats, 4,294 turns):**
  - **Default (Auto-Handoff + Read Guards):** **19.4% lower cost** overall (`25.3%` at the 90th percentile; `0.0%` median because short chats below threshold are left alone). Prompt overhead is **0 tokens per turn**.
  - **Guards Only (stay in same chat):** **0.5% lower cost** overall (`177,153` tool tokens blocked or capped, mostly large source/data files and lockfiles).
  - **By session length:** `<75k` chats save `0.2%`, `200k–400k` chats save `12.1%`, and `>400k` chats save `28.9%`.
- **Live task completion (90 real runs on `flash` across 10 tasks × 3 runs × 3 modes):**
  - Baseline (`off`), **Default**, and **Max** all scored **30/30 (100%, 95% CI 88.6–100%)** with 0 retries.
  - On 4 multi-step continuity tasks where Phase 1 ran in Chat A, `handoff_pack.py` summarized it into ~296 tokens, Chat A's history was dropped, and a new Chat B finished Phase 2 from the packet alone, **12/12 passed in both Default and Max** (vs 12/12 single-chat `off`). Splitting tiny short chats cost `+21.9%` more tokens because of cold start, which is why auto-handoff waits for large or expired-cache chats.
  - On standard coding tasks in **Max** (Compact Reads + Concise Replies), total tokens dropped **9.8%** (`-16.8%` output tokens) with **18/18 passing**.

## Install and remove

Download `agy-tokensaver-v1.2.3.zip` from [Releases](https://github.com/menscho/agy-tokensaver/releases), extract it, and run:

- **Windows:** `Install-TokenSaver.bat`
- **macOS:** `Install-TokenSaver.command`
- **Linux or CLI:** `python3 install.py` (or `python3 install.py --remote`)

Restart Antigravity and open **agy-tokensaver** in the sidebar. Run `python3 install.py --doctor` anytime to check your setup, or `python3 install.py --uninstall` to remove the plugin (`--purge-stats` also removes saved telemetry).

## Safety and privacy

- **No shell command rewriting:** `run_command` calls pass straight through unmodified.
- **Fail-open hooks:** Every Perl hook parses JSON via `JSON::PP` with a 2 MB limit and returns `allow` on malformed, missing, or oversized input (`bash tests/test_failopen.sh`).
- **Kill switch:** Switch to **Off** in the sidebar dashboard or set `AGY_TS_HOOKS=off` in your environment.
- **Network calls:** Hooks and local stats never use the network. Clicking Check/Install Update queries GitHub Releases (plus optional `gh attestation verify`) and posts once to localhost `RefreshMcpServers`. See [SECURITY.md](SECURITY.md).

## Optimization modes

| Mode | Avg cost saved (79 chats) | Live task pass rate | What it does |
| :--- | :--- | :--- | :--- |
| **Auto-Handoff + Read Guards** *(Default)* | **19.4% lower** (`0.0%` med) | **30/30 (100%)** | Starts a fresh chat when a settled chat grows large or its cache expires; blocks lockfiles, binaries, duplicate reads, and search loops. |
| **Auto-Handoff + Compact Reads** | **19.3% lower** (`0.0%` med) | **100%** *(subset of Max)* | Also tightens unbounded read caps (120L code / 80L data) and downscales oversized screenshots. |
| **Max (+ Concise Replies)** | Compact Reads + `-16.8%` output live | **30/30 (100%)** | Adds a short concise-reply rule to `rules/AGENTS.md` once active warm caches cool. |
| **Guards Only** | **0.5% lower** (`0.0%` med) | Same hooks (`0` prompt tax) | Never starts new chats automatically; saves snapshots locally and keeps read/write/find guards active. |

## Docs

- [Performance and sensitivity (`docs/perf.md`)](docs/perf.md) · [Architecture (`docs/architecture.md`)](docs/architecture.md) · [Research links (`docs/research.md`)](docs/research.md)
- [Live evaluation (`eval/README.md`)](eval/README.md) · [Security (`SECURITY.md`)](SECURITY.md) · [Changelog (`CHANGELOG.md`)](CHANGELOG.md) · [License](LICENSE)
