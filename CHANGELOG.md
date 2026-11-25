# Changelog

All notable changes to **agy-tokensaver** (`menscho/agy-tokensaver`) are documented here.

## [1.2.4] — 2026-11-25

### Changed
- **Focused around Auto-Handoff and Read Guards (`mcp_app/config.py`, `hooks.json`):** set **Auto-Handoff + Read Guards** (`smart_auto`) as the default mode (`19.4% lower cost` average across `79` replayed sessions, `25.3%` at p90), removed the `run_command` shell wrapper layer (`cmd_hook.pl`, `cmd_wrap.pl`, `scripts/compressors/*`, `tests/golden/*`), and left shell commands untouched.
- **Zero default prompt overhead (`0 tokens/turn`):** removed always-loaded default `rules/AGENTS.md`, bundled plugin subagents (`agents/*.md`), and auto-invoked workflow skills (`skills/lean-workflow`, `skills/swarm-prep`), eliminating short-session overhead regressions (`1/79` regressed vs `43/79` before). Opt-in concise reply rules (`rules_sync.py`) are created only when enabled in **Max** mode.
- **Documentation & README clarity (`README.md`, `docs/*.md`, `SECURITY.md`):** removed unused community templates (`CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `.github/ISSUE_TEMPLATE/`), updated install/docs to plain English with clear cost savings framing, and kept `eval/results/live_eval.json` as primary evidence.

### Added & Fixed
- **Live Antigravity & Handoff Continuity Evaluation (`eval/runner.py`, `eval/tasks.json`, `eval/results/live_eval.json`):** added `--driver antigravity` (`ConversationService`) and 4 multi-step continuity tasks (`H01`–`H04`) testing Phase 1 -> `handoff_pack.py` -> fresh Phase 2 chat resumption. Across **90 live runs** (`10 tasks × 3 runs × 3 modes` on `flash`), `off`, `default`, and `max` all passed **30/30 (`100%`, 95% CI `88.6–100.0%`)** with `0` retries; `max` cut standard-task tokens by **`9.8%`** (`-16.8%` output tokens).
- **Safer `.log` reads & hook kill switch (`scripts/read_hook.pl`, `scripts/lib/TsJson.pm`):** kept `.log`/`.out`/`.err` files `<= 46 KB` uncapped so mid-file errors are never hidden by blind tail-cropping (`100.0%` signal retention across all 34 tasks in `hook-replay`), and wired `"mode": "off"` / `AGY_TS_HOOKS=off` kill-switch checks across all remaining Perl hooks.

## [1.2.3] — 2026-11-24

### Fixed
- **Type checks (`eval/runner.py`, `tests/test_python_unit.py`):** removed unused `# type: ignore` comments and typed `_fake_agy` kwargs for `mypy --warn-unused-ignores`.

## [1.2.2] — 2026-11-24

### Changed
- **Unbiased eval harness (`eval/runner.py`, `eval/README.md`, `eval/results/hook_eval.json`):** `off` mode now delivers full unmodified raw output without truncation or synthetic passing-line padding; renamed `--driver hook-replay` metric to `signal_retention`; implemented live `--driver agy` and `--driver manual` task verification without reference patches (live pass-rate remains `not yet measured` until a live IDE/CLI run is executed).
- **Honest benchmarks & concise README (`scripts/replay_modes.py`, `scripts/make_benchmark_chart.py`, `docs/assets/benchmark-chart.svg`, `docs/perf.md`, `README.md`):** separated hook compression savings (`Mode 1` `-0.7%` avg / `+0.1%` median) from Auto-Handoff chat resets (`-19.2%` alone, `-19.7%` combined), demoted synthetic `--demo` (`-76.4%`) to `docs/perf.md`, moved modeled output estimates out of `README.md`, documented proxy weight sensitivity (`1/0.05/6` plus 3 alternatives), and added CI version/tag sync checks.
- **Transparency & Windows compatibility (`SECURITY.md`, `README.md`, `.github/workflows/test.yml`, `install.py`):** documented every outbound and loopback (`RefreshMcpServers`) HTTP request, tested Git-Bash (`msys`) vs Strawberry Perl (`MSWin32`) behavior, added Git-for-Windows/Strawberry Perl auto-wiring for installed `hooks.json`, and added `install.py --doctor`.

### Fixed & Removed
- **Cleanup & buried-error protection (`scripts/lib/TsLoopGuard.pl`, `scripts/compressors/common.pl`, `scripts/compressors/test.pl`, `tests/test_buried_error_prop.sh`):** removed `swarm_hook.pl` and `board_hook.pl`, merged `ssh.pl` into `log.pl`, extracted shared `TsLoopGuard.pl`, fixed zero-exit `compress_test` failure dropping (`|| true`), added 22 `<cat>_buried_error` golden cases (`66` total), and added 66 randomized property fuzz checks across all 22 categories.

## [1.2.1] — 2026-11-24

### Fixed
- **Windows golden line-ending preservation (`.gitattributes`, `tests/test_golden.sh`):** enforced LF line endings for `*.txt` and `tests/golden/**` and read `expected.txt` in binary mode so all 44 golden fixtures match byte-for-byte on Windows.
- **Perlcritic severity-4 cleanups (`scripts/compressors/log.pl`, `.perlcriticrc`):** replaced mutating `map` in HTML compression with a plain loop and documented intentional CLI hook exclusions.

## [1.2.0] — 2026-11-24

### Added
- **Quality evaluation harness (`eval/`):** 30 scripted coding tasks across 3 fixture repositories (`py_calc`, `node_api`, `log_ops`) covering bugfixes, new functions, refactoring, log debugging, and lockfile/dependency lookup (`eval/runner.py`, `eval/tasks.json`, `eval/README.md`, `eval/results/hook_eval.json`).
- **Distribution & sensitivity benchmarks (`scripts/transcript_replay.py`, `scripts/replay_modes.py`, `scripts/make_benchmark_chart.py`):** added `--report` (per-session median, p90, max, and `>5%` share), per-session-type breakdown (`log-heavy` vs `plain-editing`), `--weights` sensitivity across 4 pricing regimes, and a strip + IQR box plot in `docs/assets/benchmark-chart.svg`.
- **Modular architecture & golden regression suite (`tests/golden/`, `tests/test_golden.sh`, `tests/update_golden.sh`):** added 44 golden fixtures (2 per compressor category: typical + edge) plus `tests/run_all.sh` to run all 12 test suites and the vendor SHA-256 check across Linux, macOS, and Windows.
- **Fail-open & semantic verification suites (`tests/test_failopen.sh`, `tests/test_cmd_semantics.sh`, `tests/test_json_parsing.sh`, `tests/test_spill_safety.sh`):** 190 fuzz inputs across all 10 `hooks.json` hooks, 63+ POSIX/PowerShell semantic preservation cases (exit codes, stdin, stderr ordering, side effects, non-ASCII paths, `AGY_TS_SHELL=off`), and 30 spill-permission/retention/lossless-byte tests.
- **Spill-file hardening & purge CLI (`scripts/lib/TsSpill.pl`, `scripts/ts_inspect.py`):** `0700` dir / `0600` spill files (plus Windows `icacls` user ACL), symlink refusal, byte-exact raw output preservation, configurable retention (`AGY_TS_SPILL_DAYS`, default 7), and `ts_inspect.py purge [--days N]`.
- **CI linting, typing, coverage & attested releases (`.github/workflows/test.yml`, `.github/workflows/release.yml`, `mcp_app/vendor/README.md`, `pyproject.toml`, `.perlcriticrc`):** CI runs `ruff`, `mypy`, `perlcritic`, `shellcheck`, 80% `mcp_app/` coverage, native Windows Perl suites, vendor SHA-256 verification (`@modelcontextprotocol/ext-apps@2.0.4`), and builds tagged releases with `SHA256SUMS.txt` and `actions/attest-build-provenance` (verified optionally via `gh attestation verify`).
- **Documentation & community files:** added `docs/architecture.md`, `docs/perf.md`, `docs/research.md`, `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `.github/ISSUE_TEMPLATE/`, and `.github/PULL_REQUEST_TEMPLATE.md`.

### Changed
- **Structured `JSON::PP` hook parsing (`scripts/lib/TsJson.pm`):** replaced regex JSON extraction across all Perl hooks with core `JSON::PP`, added a 2 MB fail-open payload cap, fixed uninitialized warnings under `use warnings`, and recorded latency tradeoffs in `docs/perf.md`.
- **Modularized large files (`mcp_app/`, `scripts/compressors/`, `scripts/lib/`):** split `mcp_app/core.py` into `config.py`, `rules_sync.py`, `rollover.py`, `transcript_io.py`, `telemetry.py`, `snapshots.py`, `updater.py`, and facade `core.py`; split `scripts/cmd_wrap.pl`, `scripts/cmd_hook_core.pl`, `scripts/read_hook.pl`, and `scripts/transcript_replay.py` so every source file stays ~500 lines or under.
- **Lower always-loaded prompt overhead (`rules/AGENTS.md`):** trimmed prompt overhead from `392` to `190 tok/turn`.

## [1.1.0] — 2026-11-24

### Defaults & Modes
- **Lossless Mode 1 is the default:** ships with lossless hooks and `Archive Only` handoffs enabled out of the box (`Mode 2a`–`Max` remain opt-in in the HUD).
- **Cache-safe deferred rule sync & Snapshot/Auto-Handoff separation** across the HUD backend and dashboard.
- **Unified causal Auto-Handoff & real-session replay** across 79 local Antigravity sessions with explicit `(estimated)` labels on modeled output reductions.

## [1.0.0] — Initial Open-Source Release

- Fail-open Perl hooks, native MCP App HUD, archive/rollover handoffs, SHA-256 verified installer/updater, and 3-OS CI.
