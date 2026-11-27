# Changelog

All notable changes to **agy-tokensaver** (`menscho/agy-tokensaver`) are documented here.

## [1.5.3] — 2026-11-27

Fresh installs start in Max mode, the best measured setting, and the docs now separate measured results from estimates.

### Changed
- **Fresh installs start in Max mode (`install.py`).** Max had the best measured result (18/18 passed, −9.0% cost, −24% tokens on the hard suite) and is the only mode that reduces output tokens. The installer now writes the Max profile and its `rules/AGENTS.md` on a first install. Updates never change the mode you already use, and the runtime fallback without a settings file is still the lossless Auto-Handoff + Read Guards profile.
- **Dashboard lists Max first as Recommended** and labels Auto-Handoff + Read Guards as the lossless option.
- **README rewritten around measured results.** The modelled quota and speed table moved to [docs/perf.md section 7](docs/perf.md#7-modelled-estimate-for-the-batched-tools-not-measured), marked as a model. The README now says where savings come from, what short vs long chats can expect, that the batched tools have not had a live A/B yet, and how much latency each hook adds.
- **`scripts/benchmark.py --demo` adds a `run_checks` step** (two noisy 240-line passing test runs) and marks the two rows whose baseline is assumed rather than measured.
- **Removed "sub-50 ms" startup claims** from `plugin.json`, the dashboard and the docs. The snapshot has a 250 ms scan limit; the full hook measured 68–84 ms in CI.

### Added
- **`THIRD_PARTY_NOTICES.md`** with the MIT license notice for the code ported from INK-Q/agy-fast, as that license requires.
- A unit test that a fresh install starts in Max and that an existing saved mode is never overwritten.

## [1.5.2] — 2026-11-27

Integrates the full batched local MCP accelerator suite, sub-50 ms `SessionStart` workspace snapshot, `≤ 8.8 KB` zero-spill output folding, and direct `--cli` Swarm mode—adapted from engineering pioneered by **[INK-Q/agy-fast](https://github.com/INK-Q/agy-fast)** (MIT License) and natively implemented in pure zero-dependency Python 3.9+ stdlib.

### Added
- **5 Eager Batched Local MCP Accelerator Tools (`mcp_app/server.py`, `mcp_config.json`, `mcp_app/fast_*.py`):** registered with `"eager": true` in `mcp_config.json` (0 lazy-load schema-read turns) and strictly capped at `≤ 8,800` UTF-8 bytes inline to eliminate `output.txt` disk-spill extra turns:
  - `batch_search` (`mcp_app/fast_search.py`): up to 20 concurrent mixed queries (`text`, `regex`, `glob`, `fuzzy`, hardened read-only `git`) with `rg` + pure-Python fallback, `fileType`/`glob` filters, `multiline`, `outputMode`, `sortBy`, glob auto-recovery (`|` alternation and bare keywords), and file-coverage-first water-filling.
  - `apply_edits` (`mcp_app/fast_edits.py`): up to 50 files per call with in-memory overlap validation, atomic writes + rollback, `bestEffort: true` partial per-file commits, closest-region & line-drift mismatch hints, hunk aliases (`oldText`/`old_string`, `newText`/`new_string`), UTF-8 BOM / CRLF / mixed-EOL / permission / symlink preservation, `.git` internal write protection, and optional inline `verify`.
  - `quick_outline` & `read_slices` (`mcp_app/fast_outline.py`): multi-language symbol outlines across 15+ languages (TS/JS incl. routes & tests, Python, GDScript, Go, Rust, Java/Kotlin/C#/Swift/C/C++, HTML/Vue/Svelte with embedded `<script>`/`<style>`, CSS/SCSS/Less, Shell, Markdown, JSON/YAML) with breadth-first water-filling + targeted `slices` / `lineRanges` shorthand.
  - `run_checks` (`mcp_app/fast_checks.py`): up to 12 concurrent shell checks (`concurrency: 1..8`, shared `150 s` call budget), ANSI/spinner cleanup, Windows `powershell -Command` trivial wrapper unwrapping, green-phase pass collapsing, Pino/Bunyan/Maven noise stripping, and Head-Tail failure compaction in `compact` and `full` modes.
- **Sub-50 ms `SessionStart` Workspace Snapshot (`scripts/session_start_hook.py`, `mcp_app/fast_snapshot.py`, `hooks.json`):** fail-open `python3 -S` startup hook injecting a `≤ 1.5 KB` snapshot (git branch/status/commits, multi-module manifests, priority-sorted 2-level tree) with a `250 ms` scan circuit-breaker, nested-repo detection, and OS/`$HOME` skip lists. Toggleable in the dashboard or via `AGY_TS_STARTUP_SNAPSHOT=off`.
- **Direct CLI Mode & Host Compatibility (`mcp_app/fast_cli.py`, `mcp_app/fast_utils.py`, `mcp_app/server.py`):** `python3 mcp_app/server.py --cli <search|outline|slice|edit|check>` for Swarm workers and scripts; MCP `roots/list` workspace root resolution with non-workspace/plugin-dir CWD guards; `--tools` / `AGY_TS_TOOLS` subsetting; `--schema-compat=minimal` / `AGY_TS_SCHEMA_COMPAT`; and automatic `apply_edits.edits` format adaptation for pre-eager CLI 1.5.0–1.5.7.

### Changed
- **Opt-in Max-Mode Rules (`mcp_app/rules_sync.py`, `scripts/pre_invocation_handoff.py`):** updated byte-identical `rules/AGENTS.md` (still absent by default = 0 passive prompt bytes) with concise hybrid batching, targeted `apply_edits`, and >150k multimodal image-isolation / cache-hygiene rules. User-owned `rules/AGENTS.local.md` is now preserved across `install.py` and `updater.py` updates.
- **Handoff Packet Tracking & Doctor (`scripts/handoff_pack.py`, `install.py`, `scripts/benchmark.py`):** `handoff_pack.py` now tracks files edited and commands verified via `apply_edits`; `install.py --doctor` reports `rg` availability and probes `SessionStart` latency; `scripts/benchmark.py --demo` benchmarks the `SessionStart` snapshot and `quick_outline`.

## [1.5.1] — 2026-11-27

Lossless token cuts: the changes remove text the model already has, or shorten text that stays in context. The only behaviour changes are the lower dedup floor and one extra rule in the opt-in Max mode.

### Changed
- **Duplicate-read dedup floor lowered from 1 KB to 512 B (`scripts/read_hook.py`):** unchanged re-reads of 0.5–1 KB files (small configs, manifests, `__init__.py`) that are still in context are now blocked. The deny message is ~20 tokens against ~150–300 for the re-read. The same mtime, size, MD5 and compaction checks apply as for larger files.
- **Shorter messages that stay in context (`scripts/read_hook.py`, `scripts/pre_invocation_handoff.py`, `scripts/post_invocation_alerts.py`):** every deny reason and advisory is replayed on each later turn, so each was cut to the facts the model acts on. Duplicate-read reason 134–147 → 74–94 chars (it no longer repeats the file name), image duplicate reason drops the savings estimate, cache-shield advisories −14%, huge-write nudge −14%, delegated handoff directive −20% (~35 tokens).
- **Smaller MCP tool definitions (`mcp_app/server.py`):** shorter descriptions for the model-visible `open_token_saver` tool (308 → 241 chars of schema) and the eight app-only tools (whole list 2,568 → 2,264 chars). Tool definitions are sent with every prompt by hosts that list them.
- **Handoff packet lists each edited file once (`scripts/handoff_pack.py`):** code files now carry their symbol outline inline in "Edited in that conversation" instead of being repeated under a separate "Edited File Symbols" section. Same information, one fewer header and one fewer line per edited code file.
- **Max mode adds a targeted-edit rule (`mcp_app/rules_sync.py`, `scripts/pre_invocation_handoff.py`):** `rules/AGENTS.md` now also says "Change existing files with targeted edits, not full-file rewrites" (output tokens cost ~6× input; diff-style edits match full rewrites on accuracy, arXiv:2604.27296). An existing rules file from an older version is rewritten through the same warm-cache deferral, and both writers now share byte-identical text (unit-tested) so they can never flip-flop the file and invalidate the prompt cache.

### Fixed
- **Re-read tax labels:** `scripts/benchmark.py`, the benchmark chart and `docs/perf.md` said "+8k re-read tax per rollover" while the code charges 6k (`REDISCOVERY_TAX_TOK`). They now match the code.
- **README rewritten:** plain-language description, a results table that defines the cost proxy and explains direct vs delegated handoffs, all five dashboard modes (Guards + Compact Reads was missing), installer flags explained, accurate threshold range (75k–220k adaptive), and no more conflicting figures (packet size, re-read tax, `255k` vs `254,916`).
- **SECURITY.md and docs/architecture.md:** now list the localhost `ConversationService` calls that open continuation chats (previously documented as "hooks never open network connections").
- **docs/research.md:** paper titles and descriptions corrected (IAL-Scan, AdaEdit and LogDx-CI were described as something the papers are not), plus a section on why the guards stay lossless (arXiv:2607.12161).

## [1.5.0] — 2026-11-27

### Added & Cachemaxxing
- **PostInvocation Turn-2 Termination (`scripts/post_invocation_alerts.py`):** terminates the loop immediately after delegated `handoff_exec.py` completes inside `run_command` (running right after tool completion with `terminationBehavior: "terminate"` + clickable `user_markdown`), eliminating the old chat's second full-context echo turn that previously slipped past `PreInvocation`.
- **Proactive High-Context KV-Cache Shield (`scripts/pre_invocation_handoff.py`):** appends a single 1-line tail advisory (~35 tokens, 0 KV prefix invalidation, at most once per 10–15m) when a long autonomous run (`invocationNum >= 6`) crosses the model's high-risk TTL zone (`110k` on Hydrogen `90s` TTL / `180k` on Neon `240s` TTL) so calls stay batched and avoid mid-run TTL evictions.
- **Extended KV-Cache Prefix Protection (`mcp_app/rules_sync.py`, `mcp_app/transcript_io.py`, `scripts/post_invocation_alerts.py`):** aligned empirical model TTL cliffs from 4,688 real turns (`90s` Hydrogen / `240s` Neon) and extended `rules_sync.py` deferral to `300s` on Neon (`150s` Hydrogen) for `>=20k` active chats so toggling modes never breaks a hot Neon cache (which empirically retains 90.2% hit rate across 150–240s).
- **In-Context 6×-Output Overwrite Nudge (`scripts/write_hook.py`, `scripts/post_invocation_alerts.py`):** lowered non-blocking `huge_overwrite` detection to `>80L` / `>=3KB` (`>=75%` unchanged lines) and paired the UI alert with a 1-line tail `systemMessage` nudge so the model switches to `edit_file` for subsequent edits instead of repeating full-file rewrites.
- **Fast-Growing Session Cooldown & High-Context Fast-Track (`mcp_app/config.py`, `scripts/pre_invocation_handoff.py`, `mcp_app/rollover.py`):** lowered default `cooldown_min` from `20m` to `8m`, bypassed cooldown on emergency (`>=180k`) or post-drop rotations (60s anti-burst guard), and fast-tracked `min_turns` to `6` when context already exceeds `120k`.

### Fixed & Hardened
- **Zero-Loss Read Guard Upgrades (+29% more tool tokens cut, `scripts/read_hook.py`, `scripts/find_hook.py`):**
  - Fixed dead-code `<= 4096` early-return that previously bypassed `DEDUPE_MIN_BYTES` on `1–4 KB` files, and lowered `DEDUPE_MIN_BYTES` to `1024` bytes.
  - Added `/.system_generated/tasks/*.log` to `SPILLBACK_RE` and added byte-aware capping (`<=14 KB`) on wide bounded spillback reads (`150–220+` long lines) and blanket bounded (`>=300L`, `>16 KB`) data/log reads.
  - Fixed cross-conversation `dirty_hunk` safety by scoping snapshots to `(conversationId, epoch, path)` so a fresh chat's first read is never falsely narrowed, while expanding `dirty_hunk` coverage to `128 KB` / `>=120L` and full-file `1..N` re-reads.
  - Expanded binary, generated-artifact, vendor, and `find_hook.py` exclude lists (`.mypy_cache`, `.ruff_cache`, `.pytest_cache`, `.turbo`, `.terraform`, `Pods`, `site-packages`, `.cargo/registry`, etc.).
- **Cleaner Handoff Packets (`scripts/handoff_pack.py`):** increased `last_verify` capture limit to 180 chars and filtered bare `Traceback (most recent call last):` headers so real exception/failure lines are surfaced.
- **Ground-Truth Replay Accounting (`mcp_app/core.py`, `mcp_app/transcript_io.py`, `mcp_app/rollover.py`, `scripts/transcript_replay.py`, `scripts/replay_io.py`, `scripts/replay_modes.py`):** exported `has_direct_handoff_cap` in `core.py`, gated PreInvocation replay strictly on exact `followed_by_user` (`USER_INPUT`) boundaries (never background-task waits), checked real observed next-turn cache drops in Path 2 replay, and decoupled Default mode overhead (`0 tok/turn`) from local Max-mode `rules/AGENTS.md`.

## [1.4.0] — 2026-11-27

### Added
- **PreInvocation Auto-Handoff & Turn-2 Termination (`hooks.json`, `scripts/pre_invocation_handoff.py`, `scripts/handoff_exec.py`):** runs at `invocationNum == 0` when the user sends a message — spawning the fresh continuation chat directly via `ConversationService` (`terminationBehavior: "terminate"`, 0 tokens spent) when `ANTIGRAVITY_LS_ADDRESS` is available (Path 1), or via a 1-step delegated `run_command` handoff (Path 2) that now **terminates Turn 2 immediately** via `user_markdown` right after `handoff_exec.py` completes (saving 100% of the old chat's second full-context reply turn).
- **Mid-run cache-drop advisory & pre-swarm shield (`scripts/pre_invocation_handoff.py`):** warns once per 10m after a large (`>=75k`) mid-run KV drop to batch remaining calls and wrap up before the next TTL cliff, and rotates before heavy swarm/workflow/benchmark prompts (`>=55k` direct / `>=75k` delegated).
- **Richer zero-re-read handoff packets (`scripts/handoff_pack.py`):** auto-detects the active git root from touched files, recovers full text from `transcript_full.jsonl` when truncated, preserves **both head and tail** (setup + final conclusions/metrics) on long assistant replies, and includes active plan steps, artifact checklists, key diff hunks, edited file symbol outlines, read line ranges, and cross-session verified repo commands.
- **Side-pane (`aux_pane`) entrypoint (`mcp_app/server.py`, `mcp_app/dashboard.html`):** enables opening `agy-tokensaver` in a chat side pane alongside the global sidebar, with `app.sendMessage(..., {target: 'new'})` fallback when LS env vars are absent.

### Changed & Fixed
- **Honest Path 1 vs. Path 2 Auto-Handoff economics (`scripts/pre_invocation_handoff.py`, `scripts/handoff_exec.py`, `mcp_app/rollover.py`, `mcp_app/transcript_io.py`, `mcp_app/dashboard.html`, `scripts/post_invocation_alerts.py`):**
  - Disables low-threshold `cold_cache` (`55k`) and `model_switch` (`45k`) triggers in Path 2 delegated mode (since Turn 0 still runs in the old chat and cannot avoid the cold rebuild), skips trivial short confirmations (`<=45` chars below `180k`), accounts for delegated overhead in `_compute_policy`, and aligns Neon's empirical cold-cache TTL cliff to `240s` (Hydrogen `60–90s`).
  - Dashboard counterfactual replay and rollover telemetry automatically model Path 2 delegated cost whenever `hook_caps.json` shows `has_ls_addr: false`, and cold-cache alerts/banners advise using **Archive & New Chat** before resuming cold chats.
- **Lowered unchanged-file dedup floor (`scripts/read_hook.py`):** lowered `DEDUPE_MIN_BYTES` from `4096` to `1536` bytes so repeated unchanged reads of `1.5–4 KB` files (`SKILL.md`, `hooks.json`, `pyproject.toml`, small hooks) are caught with zero quality loss.
- **Deferred rule sync (`mcp_app/rules_sync.py`, `.gitignore`):** ignores runtime-generated `rules/AGENTS.md` in git and avoids unnecessary disk rewrites when state matches.

## [1.3.1] — 2026-11-25

### Fixed
- **Windows Git Bash CI test suites:** Converted `mktemp -d` temporary directories via `cygpath -m` in `tests/run_tests.sh`, `tests/test_read.sh`, `tests/test_headroom_plexer.sh`, and `tests/test_json_parsing.sh` when running under native Windows Python (`os.name == "nt"`), so hook JSON inputs receive Windows-readable paths instead of MSYS `/tmp/...` paths.

## [1.3.0] — 2026-11-25

### Changed
- **Single zero-dependency Python 3.9+ runtime (`scripts/*.py`, `hooks.json`, `install.py`):** replaced all 7 Perl hook scripts and `scripts/lib/*.{pm,pl}` with `python3 -S` Python 3.9+ stdlib hooks (`scripts/hook_common.py`, `read_hook.py`, `write_hook.py`, `find_hook.py`, `grep_hook.py`, `post_edit_hook.py`, `post_invocation_alerts.py`, `stop_telemetry.py`). Eliminates Perl installation and path issues on Windows, macOS, and Linux, removes `.perlcriticrc` and Perl CI checks, and drops ~1,850 lines of Perl code.
- **Auto-Handoff product focus & honest replay labeling (`README.md`, `plugin.json`, `mcp_app/dashboard.html`, `scripts/replay_modes.py`, `scripts/make_benchmark_chart.py`, `docs/`):** leads clearly with Auto-Handoff for long or cold-cache chats as the primary feature (read/write/search guards as a lightweight extra), labels counterfactual transcript numbers explicitly as **replay estimates**, and removes `docs/review-response.md`.

### Added
- **Hard multi-module tasks & 6-turn A/B benchmarks (`eval/hard_tasks.json`, `eval/fixtures/{order_engine,log_router}/`, `eval/runner.py`):** added `--driver agentapi` (using `agentapi new-conversation` + `send-message`), `--tasks-file`, `--resume`, zero-token rate-limit retry backoff, 6 hard cross-module tasks (`X01`–`X06` tested across 54 live runs on `gemini-4-hydrogen-xhigh`), and 3 six-turn A/B sequences (`L01`–`L03` tested across 18 live sessions / 108 user turns on `gemini-4-hydrogen-high`).

### Fixed
- **Live rollover conversation ID parsing (`mcp_app/snapshots.py`):** fixed `perform_rollover` to extract the nested `response.newConversation.conversationId` field returned by `agentapi new-conversation`.
- **Installer & updater 7-hook wiring (`install.py`, `mcp_app/updater.py`):** updated `_wire_hooks_python`, `run_doctor`, and `apply_update` to iterate direct `PostInvocation` and `Stop` hook entries alongside `PreToolUse`/`PostToolUse` arrays so all 7 hooks are verified and wired to `sys.executable` on Windows.

## [1.2.5] — 2026-11-25

### Added
- **Unit test coverage (`tests/test_python_unit.py`):** added tests for `mcp_app/config.py` (`off` and fallback paths), `mcp_app/rules_sync.py` (warm-cache deferral), `mcp_app/snapshots.py` (`spawn_chat=True` rollover), and `mcp_app/server.py` (`ts_save_settings`, `ts_build_handoff`, `ts_rollover_now`, `ts_clear_history`) to bring `mcp_app/` coverage cleanly above 80%.

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
