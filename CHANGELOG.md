# Changelog

All notable changes to **agy-tokensaver** (`menscho/agy-tokensaver`) are documented here.

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
