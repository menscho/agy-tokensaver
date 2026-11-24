# agy-tokensaver

<p align="left">
  <a href="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml"><img src="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml/badge.svg" alt="CI"/></a>
  <a href="https://github.com/menscho/agy-tokensaver/releases"><img src="https://img.shields.io/badge/release-v1.2.3-4ade80?style=flat-square&labelColor=0d0d11" alt="Release v1.2.3"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8e8e98?style=flat-square&labelColor=0d0d11" alt="MIT"/></a>
</p>

Shortens noisy test/build/log/git output and blocks duplicate file reads in **Google Antigravity**, without losing errors. Zero third-party dependencies (Perl 5 + Python 3 stdlib).

![Real-Session Measured Benchmark](docs/assets/benchmark-chart.svg)

## What to expect

- **Default mode (Lossless):** **`-0.7%`** avg (`+0.1%` median) cost saved via `python3 scripts/benchmark.py --modes --report`; full breakdown in [docs/perf.md](docs/perf.md#1-real-session-replay-python3-scriptsbenchmarkpy---modes---report).
- **Compact Reads (Opt-in):** **`-0.8%`** avg (`+0.1%` median) cost saved via `python3 scripts/benchmark.py --modes --report` by tightening unbounded file-read limits.
- **Auto-Handoff (Opt-in):** **`-19.7%`** avg (`+0.1%` median) cost saved via `python3 scripts/benchmark.py --modes --report` by starting fresh chats when settled sessions hit context limits.
- **Signal retention & live pass rate:** `100.0%` signal retention across 30 tasks (`python3 eval/runner.py --driver hook-replay --runs 3`). **Live pass-rate: not yet measured** — run `python3 eval/runner.py --driver manual` ([`eval/README.md`](eval/README.md)).

## Install & Uninstall

Download `agy-tokensaver-v1.2.3.zip` from **[Releases](https://github.com/menscho/agy-tokensaver/releases)**, extract it, and run:

- **Windows:** `Install-TokenSaver.bat`
- **macOS:** `Install-TokenSaver.command`
- **Linux / CLI:** `python3 install.py` (or `python3 install.py --remote`)

Restart Antigravity and open **agy-tokensaver** in the sidebar. Verify setup anytime with `python3 install.py --doctor` (auto-detects Git-for-Windows Perl on Windows), and uninstall with `python3 install.py --uninstall` (`--purge-stats` deletes `~/.gemini/antigravity/plugin_data/token-saver/`).

## Is it safe?

- **Fail-open hooks:** Every Perl hook parses payloads via `JSON::PP` with a `2 MB` cap and emits `{"decision":"allow"}` or `{}` on malformed/oversized input (`bash tests/test_failopen.sh`).
- **Byte-exact raw logs:** Whenever `cmd_wrap.pl` shortens output, it first writes the full raw stream to a `0600` spill file (`ts_inspect.py purge` deletes them).
- **Instant kill switch:** Set `AGY_TS_SHELL=off` to bypass all command rewriting immediately.
- **Network calls:** Hooks and telemetry never use the network. User-triggered updates fetch GitHub Releases (plus optional `gh attestation verify`) and POST once to localhost `RefreshMcpServers` to reload Antigravity's MCP server; see [SECURITY.md](SECURITY.md).

## Platform Support

- **Linux / macOS:** Full CI (`bash tests/run_all.sh` + linters + Python unit tests); wraps POSIX `bash` commands via system/Homebrew Perl.
- **Windows (Git-Bash Perl `msys`):** Full CI (`bash tests/run_all.sh` + native PowerShell tests); wraps Windows PowerShell `C:\...` commands via `powershell.exe` and POSIX `/...` commands via Git Bash.
- **Windows (Strawberry Perl `MSWin32`):** Wraps Windows PowerShell `C:\...` commands via `powershell.exe`; returns `allow` unmodified if forced into `AGY_TS_SHELL=posix` (no POSIX `bash` in native Win32 Perl).

## Modes

| Mode | Savings (Avg / Median) | What changes for you | Tradeoffs |
| :--- | :--- | :--- | :--- |
| **Mode 1: Lossless (Default)** | **`-0.7%` / `+0.1%`** *(measured)* | Compresses test/build/log/git output, blocks duplicate/lockfile reads, archives handoffs on request. | Keeps full raw bytes on disk; short plain-editing chats see `+0.1%` overhead. |
| **Mode 2a: Compact Reads** *(Opt-in)* | **`-0.8%` / `+0.1%`** *(measured)* | Lowers unbounded file read caps and optionally downscales huge images. | May require an extra targeted slice read on large source files. |
| **Mode 2b: + Concise Output** *(Opt-in)* | Measured in live eval only *(not yet measured)* | Adds a terse-reply instruction to `rules/AGENTS.md` once warm caches cool. | Shorter agent prose explanations; live impact not yet measured. |
| **Mode 2c: + Auto-Handoff** *(Opt-in)* | **`-19.7%` / `+0.1%`** *(measured)* | Mode 2a plus automatic fresh-chat rollovers with a git/symbol handoff summary. | **Starts a new chat**, discarding verbatim conversation history beyond the summary. |
| **Mode 2 (Max): All opt-in** | Measured in live eval only *(not yet measured)* | Combines Compact Reads, Concise Output, and Auto-Handoff. | Combines shorter replies and automatic chat resets. |

## Documentation

- [Performance, Modeled Estimates & Latency (`docs/perf.md`)](docs/perf.md) · [Architecture (`docs/architecture.md`)](docs/architecture.md) · [Research References (`docs/research.md`)](docs/research.md)
- [Evaluation Harness (`eval/README.md`)](eval/README.md) · [Security Policy (`SECURITY.md`)](SECURITY.md) · [Contributing (`CONTRIBUTING.md`)](CONTRIBUTING.md) · [Changelog (`CHANGELOG.md`)](CHANGELOG.md)

MIT License — see [LICENSE](LICENSE).
