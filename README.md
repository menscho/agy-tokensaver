# agy-tokensaver

<p align="left">
  <a href="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml"><img src="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml/badge.svg" alt="CI"/></a>
  <a href="https://github.com/menscho/agy-tokensaver/releases"><img src="https://img.shields.io/badge/release-v1.2.0-4ade80?style=flat-square&labelColor=0d0d11" alt="Release v1.2.0"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8e8e98?style=flat-square&labelColor=0d0d11" alt="MIT"/></a>
</p>

A zero-dependency Perl 5 + Python 3 plugin for **Google Antigravity** built for coding-agent sessions that run heavy test suites, logs, builds, diffs, or search queries.

**Headline results:** Cuts **-76.4%** of direct tool tokens on noisy workloads (`36,383 → 8,594` tokens in `benchmark.py --demo`, `15.7 ms` median hook hit latency; `~2.5 ms` fast-path misses). Across **79 real local sessions** (`4,051` turns, prompt caching active, `190 tok/turn` overhead charged), the default **Lossless** mode averages **-0.7%** (**p90 -1.7%**, `224,631` tool tokens removed); opt-in modes reach **-20.1% measured** (**p90 -25.7%**) with Auto-Handoff and **-26.2%..-30.7% (estimated)** with Concise Output. **Quality impact:** **100.0%** (`95.9..100.0%` CI) across `30` scripted coding tasks × `3` runs under `--driver hook-replay` (`70.0%` baseline without hooks because lockfile/log dumps truncate required lines); live end-to-end LLM pass-rate evaluation awaits manual/headless runs (`eval/README.md`).

![Token Saver HUD](docs/assets/hud-preview.svg)

## Install & Uninstall

Download `agy-tokensaver-v1.2.0.zip` from **[Releases](https://github.com/menscho/agy-tokensaver/releases)** (built in CI with `SHA256SUMS.txt` and GitHub build provenance attestations), extract it, and run:

- **Windows:** `Install-TokenSaver.bat`
- **macOS:** `Install-TokenSaver.command`
- **Linux / CLI:** `python3 install.py` (or `git clone https://github.com/menscho/agy-tokensaver.git && cd agy-tokensaver && python3 install.py`)

Restart Antigravity and open **Token Saver** in the sidebar.

**Uninstall:**

```bash
python3 install.py --uninstall                # removes plugin; preserves plugin_data telemetry & handoff archive
python3 install.py --uninstall --purge-stats  # also deletes ~/.gemini/antigravity/plugin_data/token-saver/
```

## Modes & Benchmark Results

All numbers come from `python3 scripts/benchmark.py --modes --report` (79 local sessions, cost-proxy weights `1×` input + `0.05×` cache read + `6×` output — configurable with `--weights`) and `python3 eval/runner.py --driver hook-replay --runs 3`.

| HUD Mode | Savings (Avg / Median / p90) | Quality Impact (`eval/`) | What Changes for the User |
| :--- | :--- | :--- | :--- |
| **Mode 1: Lossless (Default)** | **-0.7%** / `+0.1%` / **-1.7%** *(measured)* | `100.0%` signal pass (`0` agent prompt/chat changes) | Compresses noisy test/build/git/log/JSON/SSH output (full raw bytes kept in `0600` spill files), blocks binaries/lockfiles & duplicate re-reads, breaks 3× retry loops, archives handoffs only on request. |
| **Mode 2a: Compact Reads** *(Opt-in)* | **-0.9%** / `+0.1%` / **-2.2%** *(measured)* | `100.0%` signal pass *(live LLM eval pending before proposing default)* | Mode 1 + tighter unbounded read caps (`120L`/`80L` vs `250L`/`120L`), `80L` `CHANGELOG.md` cap, and optional downscaling of `>1568px` screenshots. |
| **Mode 2b: + Concise Output** *(Opt-in)* | **-7.0%..-11.5% (estimated)** / `-8.0%` / `-14.0%` | `100.0%` signal pass; adds terse-reply rule | Mode 2a + adds one concise-prose line to `rules/AGENTS.md` (deferred until active warm cache cools). Never resets chats. |
| **Mode 2c: + Auto-Handoff** *(Opt-in)* | **-20.1%** / `+0.1%` / **-25.7%** *(measured)* | `100.0%` signal pass; best at phase switches | Mode 2a + automatically rolls settled `>=75k` cold/over-cap chats into a fresh chat with a deterministic handoff packet (`git` state, edited `L<start>-L<end>` symbols, recent errors, verified test commands). |
| **Mode 2 (Max): All opt-in** | **-26.2%..-30.7% (estimated)** / `-8.5%` / `-30.9%` | `100.0%` signal pass | Combines Compact Reads, Concise Output, and Auto-Handoff. *(Manual phase-switch upper bound: `-40.9%..-45.4% (estimated)`.)* |

![Real-Session Benchmark and Per-Session Distribution](docs/assets/benchmark-chart.svg)

```bash
python3 scripts/benchmark.py --demo                 # 8-step noisy workflow (-76.4%)
python3 scripts/benchmark.py --modes --report       # full distribution, session-type & --weights sensitivity
python3 eval/runner.py --verify-fixtures            # verify all 30 coding tasks fail before & pass after fix
bash tests/run_all.sh                               # all 12 test suites + vendor SHA-256 check
```

## Will This Break My Agent? (FAQ)

- **What happens if a hook crashes or gets weird JSON?** Every Perl hook decodes payloads via `JSON::PP` (`scripts/lib/TsJson.pm`) with a 2 MB cap and strict fail-open handling (`tests/test_failopen.sh` tests all 10 hooks against 190 malformed/binary/oversized inputs). Any error emits `{"decision":"allow"}` (or `{}` for Post/Stop hooks).
- **Can command compression drop an error or hide output I need?** No: before any lossy compression runs, `cmd_wrap.pl` writes the byte-exact raw output (including ANSI/CRLF) to a `0600` spill file in `$TMPDIR/agy-tokensaver-<uid>` (pruned after 7 days; run `python3 scripts/ts_inspect.py purge` anytime).
- **Is telemetry sent anywhere?** Never. All hook logs, stats, and snapshots stay 100% local in `~/.gemini/antigravity/plugin_data/token-saver/`. The only network call is an explicit update check you trigger.
- **How do I turn command wrapping off immediately?** Set `AGY_TS_SHELL=off` in your environment.
- **How do I report a bad compression?** Open an issue with the **Bad Compression Report** template and attach the `[full: ...]` spill file name plus a minimal redacted input.

## Troubleshooting

- **Missing Perl on Windows:** Antigravity hooks invoke `perl`. Install [Strawberry Perl](https://strawberryperl.com/) or add Git for Windows' Perl (`C:\Program Files\Git\usr\bin`) to `PATH`, then verify with `perl -MJSON::PP -e "print 'OK'"`. Without Perl, the HUD and Archive tools still work.
- **Hooks not firing:** Confirm `plugin.json` and `hooks.json` exist in `~/.gemini/config/plugins/token-saver/`, then reload Antigravity (`install.py --cli --yes` refreshes MCP config and permissions).
- **Disable a single hook:** Remove or comment out that entry in `~/.gemini/config/plugins/token-saver/hooks.json` (e.g. `token-saver-cmd` or `token-saver-board`) and reload Antigravity.

## Compatibility Matrix

| Platform | Runtime Tested | Status |
| :--- | :--- | :--- |
| **Linux (`ubuntu-latest`)** | Perl 5.14+ (`JSON::PP` core), Python 3.9+, Antigravity IDE & CLI | Full CI (`bash tests/run_all.sh` + linters + 80% MCP coverage) |
| **macOS (`macos-latest`)** | System Perl 5 + Python 3.9+, Antigravity IDE & CLI | Full CI (`bash tests/run_all.sh`) |
| **Windows (`windows-latest`)** | Git Bash / Strawberry Perl 5 + Python 3.12 + PowerShell 5.1 | Full CI (`bash tests/run_all.sh` + native PowerShell hook & installer tests) |

## Documentation

- [Architecture & State Layout](docs/architecture.md) · [Performance & Hook Latency Budget](docs/perf.md) · [Research & OSS Citations](docs/research.md)
- [Quality Evaluation (`eval/`)](eval/README.md) · [Security Policy & Spill Handling](SECURITY.md) · [Contributing Guide](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

MIT License — see [LICENSE](LICENSE).
