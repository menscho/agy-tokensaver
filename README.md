# agy-tokensaver

<p align="left">
  <a href="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml"><img src="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml/badge.svg" alt="CI"/></a>
  <a href="https://github.com/menscho/agy-tokensaver/releases"><img src="https://img.shields.io/badge/release-v1.4.0-4ade80?style=flat-square&labelColor=0d0d11" alt="Release v1.4.0"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8e8e98?style=flat-square&labelColor=0d0d11" alt="MIT"/></a>
</p>

Starts fresh continuation chats when a long Google Antigravity session grows overgrown or its prompt cache goes cold, carrying over a compact (~450–535 token) handoff summary so work continues without lost context. Also includes lightweight, fail-open Python read, write, and search guards (`0.4%` replay estimate) with zero prompt overhead and no shell-command rewriting. Single zero-dependency Python 3.9+ runtime (no Perl needed on Windows, macOS, or Linux).

## How well it works

Every number below comes from runnable commands in this repository (`eval/runner.py` and `scripts/benchmark.py`):

- **6-turn long-session A/B (`eval/results/ab_long_eval.json`, `gemini-4-hydrogen-high`):**
  Across 18 six-turn sessions (`L01`–`L03`, 108 user turns), handing off to a fresh chat via `handoff_pack.py` after Turn 3 (~533 tokens) passed **9/9 (100%)** vs **9/9 (100%)** for unbroken chats and cut total tokens **31.9%** (18.25M -> 12.43M; peak context 136k -> 75k). On back-to-back hot-cache turns (`92%` cache hits), weighted cost (`1x` in + `0.05x` cache + `6x` out) was `+2.8%` overall (`-9.0%` on `L01`, `+1.0%` `L02`, `+21.8%` `L03`), showing why Auto-Handoff waits for `>150k` or cold-cache chats instead of splitting warm `~110k` caches early.
- **Hard multi-module tasks (`eval/results/hard_eval.json`, `gemini-4-hydrogen-xhigh`):**
  Across 54 runs (`X01`–`X06` × 3 runs × 3 modes spanning multiple modules, 85 KB logs, 21 KB JSON, and YAML): `off` scored **18/18 (100.0%)**, Default scored **17/18 (94.4%, -15.7% tokens, -1.6% cost)**, and Max scored **18/18 (100.0%, -24.2% tokens, -9.0% cost)**.
- **Counterfactual replay estimate (`scripts/benchmark.py --modes --report`, 81 sessions):**
  Assuming unchanged post-handoff agent trajectories (+8k re-read tax per rollover), Default estimates **20.7% lower cost** (24.5% p90, 0.0% median on short chats; 12.1% on 200k–400k chats; 29.7% above 400k). Guards alone estimate **0.4% lower cost** (182k tool tokens cut).

## Install and remove

Download `agy-tokensaver-v1.4.0.zip` from [Releases](https://github.com/menscho/agy-tokensaver/releases), extract it, and run:

- **Windows:** `Install-TokenSaver.bat`
- **macOS:** `Install-TokenSaver.command`
- **Linux or CLI:** `python3 install.py` (or `python3 install.py --remote`)

Restart Antigravity and open **agy-tokensaver** in the sidebar. Run `python3 install.py --doctor` anytime to verify your install, or `python3 install.py --uninstall` to remove the plugin (`--purge-stats` also deletes saved telemetry).

## Safety and privacy

- **No shell command rewriting:** shell calls pass through unmodified.
- **Fail-open Python hooks:** every hook runs on Python 3.9+ stdlib (`python3 -S`) with a 2 MB payload limit and returns `allow` or `{}` on malformed, missing, or oversized inputs (`bash tests/test_failopen.sh`).
- **Kill switch:** switch to **Off** in the sidebar dashboard or set `AGY_TS_HOOKS=off`.
- **Network calls:** hooks and local telemetry never use the network; updates run only on demand with `SHA256SUMS.txt` + optional `gh attestation verify` ([SECURITY.md](SECURITY.md)).

## Optimization modes

| Mode | Best for | What it does |
| :--- | :--- | :--- |
| **Auto-Handoff + Read Guards** *(Default)* | Long or cold chats (0 prompt tax) | Starts a fresh continuation chat above adaptive thresholds or after cache expiry; blocks lockfiles, binaries, duplicates, and 3x search loops. |
| **Auto-Handoff + Compact Reads** | Large files and screenshots | Tightens unbounded read caps (120L code / 80L data) and downscales oversized screenshots. |
| **Max (+ Concise Replies)** | Lowest live cost (-24.2% tok / -9.0% cost on hard suite) | Pairs Compact Reads with a concise-reply rule in `rules/AGENTS.md` once warm caches cool. |
| **Guards Only** | Keeping one continuous chat | Saves handoff snapshots locally without auto-switching chats; keeps read, write, and search guards active. |

## Docs

- [Performance and sensitivity (`docs/perf.md`)](docs/perf.md) · [Architecture (`docs/architecture.md`)](docs/architecture.md) · [Research links (`docs/research.md`)](docs/research.md)
- [Live evaluation (`eval/README.md`)](eval/README.md) · [Security (`SECURITY.md`)](SECURITY.md) · [Changelog (`CHANGELOG.md`)](CHANGELOG.md) · [License](LICENSE)
