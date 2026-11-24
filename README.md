# agy-tokensaver

<p align="left">
  <a href="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml"><img src="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml/badge.svg" alt="CI"/></a>
  <a href="https://github.com/menscho/agy-tokensaver/releases"><img src="https://img.shields.io/badge/release-v1.1.0-4ade80?style=flat-square&labelColor=0d0d11" alt="Release v1.1.0"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8e8e98?style=flat-square&labelColor=0d0d11" alt="MIT"/></a>
</p>

A zero-dependency Perl + Python plugin for **Google Antigravity** that trims noisy tool output, blocks duplicate or lockfile reads, and optionally rolls long chats over into fresh sessions.

It defaults to **Mode 1 (Lossless)**: raw output is spilled to disk before any compression, hooks fail open on bad input, and nothing resets your chat unless you opt in from the HUD.

## Install

Download `agy-tokensaver-v1.1.0.zip` from **[Releases](https://github.com/menscho/agy-tokensaver/releases)** (verified against `SHA256SUMS.txt`), extract it, and run:

- **Windows:** `Install-TokenSaver.bat`
- **macOS:** `Install-TokenSaver.command`
- **Linux / CLI:** `python3 install.py`

Or install from source:

```bash
git clone https://github.com/menscho/agy-tokensaver.git && cd agy-tokensaver && python3 install.py
```

Restart Antigravity and open **Token Saver** in the sidebar to switch modes or inspect session telemetry.

## Benchmark

![Real-Session Benchmark](docs/assets/benchmark-chart.svg)

Replayed against **79 local Antigravity sessions** (`3,952` turns, `111.3M` cost-proxy units) through the real hooks, charging retries, follow-up reads, and the plugin's own `392 tok/turn` prompt overhead:

| HUD Mode | Default / Opt-in | Measured vs Estimated | Tool Tokens Cut | Avg Cost Change |
| :--- | :--- | :--- | ---: | ---: |
| **Mode 1: Lossless** | **Default** | Measured replay | 224,649 | **-0.7%** |
| **Mode 2a: Compact Reads** | Opt-in | Measured replay | 327,704 | **-0.9%** |
| **Mode 2b: + Concise Output** | Opt-in | **Estimated** (modeled output) | 327,704 | **-7.0%..-11.5% (estimated)** |
| **Mode 2c: + Auto-Handoff** | Opt-in | Measured replay | 327,704 | **-20.2%** |
| **Mode 2 (Max): All opt-in** | Opt-in | **Estimated** (modeled output) | 327,704 | **-26.3%..-30.8% (estimated)** |
| *Upper Bound (manual phase resets)* | *Reference* | **Estimated** | 327,704 | *-40.9%..-45.4% (estimated)* |

- **Why Mode 1 looks small on average (-0.7%) but big on noisy steps:** Across normal mixed chats, tool outputs are a small share of total cost once prompt caching is active. On an 8-step noisy workflow (`python3 scripts/benchmark.py --demo` — lockfile read, large source file, duplicate read, 300-test pytest run, lockfile git diff, 900-line server log, 120-row JSON response, SSH grep), the same lossless hooks cut direct tool tokens by **-76.4%** (`36,383 → 8,594` tokens, `9.1 ms` median hook latency).
- **Methodology & caveats:**
  - Cost weights (`1×` uncached input, `0.05×` cache read, `6×` output) are a standard LLM API pricing proxy, not published Antigravity billing weights.
  - Rows marked **(estimated)** add a modeled `-20%..-35%` reduction on the `6×` output bucket (30.3% of baseline proxy cost) because concise-reply rules cannot be replayed retroactively on recorded transcripts.
  - The ~`90–150s` idle cache-drop cliff used by cold-cache handoffs is measured empirically from local Antigravity transcripts, not a documented service SLA.

## How the modes work

Switch anytime in the **Token Saver** HUD (toggling concise rules while a large warm cache is active defers the file write until the cache cools so your prefix isn't invalidated mid-session):

- **Mode 1 — Lossless (Default):** Compresses verbose test/build/git/log/JSON/SSH output (saving full raw output to temp files with deterministic content-addressed names), blocks lockfiles/binaries/auto-generated files and unchanged duplicate reads, anchors unbounded `.log` reads to the tail, breaks 3× identical retry loops, and archives snapshots without opening new chats.
- **Mode 2a — Compact Reads:** Tighter default slices on unbounded reads (`120L`/`80L` instead of `250L`/`120L`), `80L` cap on `CHANGELOG.md`, and optional downscaling of `>1568px` screenshots.
- **Mode 2b — + Concise Output:** Adds a short reply-style rule targeting verbose prose while leaving code and edits untouched. Never resets your chat.
- **Mode 2c — + Auto-Handoff:** When a settled long chat crosses its adaptive context cap or sits idle past the local cache-drop cliff (`>=75k` tokens), automatically opens a fresh chat seeded with a zero-LLM handoff packet (git branch/status/diff, edited files with `L<start>-L<end>` symbol outlines, recent failed commands, verified repo test commands, and a link to the full archived transcript). Best at task/phase boundaries; mid-debug rollovers can occasionally require re-reading a file.
- **Mode 2 (Max):** Combines Auto-Handoff, Compact Reads, and Concise Output.

## What's included

- **Hooks (`scripts/`):** `cmd_hook.pl`, `cmd_wrap.pl` (~40 output compressors across Unix & PowerShell), `read_hook.pl`, `write_hook.pl`, `find_hook.pl`, `grep_hook.pl`, `board_hook.pl`, `swarm_hook.pl`, `post_edit_hook.pl`, `post_invocation_alerts.pl`, `stop_telemetry.pl`.
- **MCP App HUD (`mcp_app/`):** Local full-page panel with session telemetry, mode selector, snapshot archive, and SHA-256 verified release updater.
- **CLI utilities (`scripts/`):** `handoff_pack.py`, `ts_inspect.py` (`outline` & SQLite FTS5 `query`), `audit.py --discover`, `benchmark.py`, `transcript_replay.py`.
- **Requirements:** Python 3.9+ and Perl 5 (macOS/Linux built-in; Git for Windows / MSYS2 / Strawberry Perl on Windows). Zero third-party packages.

<details>
<summary>Research & open-source references</summary>

- Prompt caching & prefix stability: *Don't Break the Cache* ([arXiv:2601.06007](https://arxiv.org/abs/2601.06007)), *CAPC* ([arXiv:2607.15516](https://arxiv.org/abs/2607.15516)), *Keeping the Cache Warm* ([arXiv:2607.19214](https://arxiv.org/abs/2607.19214)), *Persistent Context Graphs for Efficient Memory Compaction in LLM Agents* ([arXiv:2609.40118](https://arxiv.org/abs/2609.40118)), *Models Take Notes at Prefill* ([arXiv:2606.17107](https://arxiv.org/abs/2606.17107)).
- Context resets & handoffs: *The Complexity Trap* ([arXiv:2508.21433](https://arxiv.org/abs/2508.21433)), *Active Context Compression* ([arXiv:2601.07190](https://arxiv.org/abs/2601.07190)), Factory.ai anchored summarization.
- Output & loop control: *Sketch-of-Thought* ([arXiv:2503.05179](https://arxiv.org/abs/2503.05179)), *Merlin* ([arXiv:2605.09990](https://arxiv.org/abs/2605.09990)), *TokenEconomizer* ([arXiv:2610.22732](https://arxiv.org/abs/2610.22732)), *AdaEdit* ([arXiv:2604.27296](https://arxiv.org/abs/2604.27296)), *SkillReducer* ([arXiv:2603.29919](https://arxiv.org/abs/2603.29919)), *IAL-Scan* ([arXiv:2607.01641](https://arxiv.org/abs/2607.01641)), *LogDx-CI* ([arXiv:2605.28876](https://arxiv.org/abs/2605.28876)).
- Open-source inspiration: RTK, Headroom, `@plexer/cli`, Aider, Cline, OpenCode, `context-mode`, `squeez`.

</details>

## Verify locally

```bash
# Run all 7 test suites (Ubuntu, macOS, Windows in CI)
for t in tests/*.sh; do bash "$t" || break; done

# Reproducible 8-step noisy-task benchmark
python3 scripts/benchmark.py --demo

# Replay hooks against your own local Antigravity transcripts
python3 scripts/benchmark.py --modes
```

MIT License — see [LICENSE](LICENSE) and [CHANGELOG.md](CHANGELOG.md).
