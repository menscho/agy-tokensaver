# agy-tokensaver

<p align="center">
  <a href="https://github.com/menscho/agy-tokensaver/releases"><img src="https://img.shields.io/badge/release-v1.0.0-4ade80?style=flat-square&labelColor=0d0d11" alt="Release v1.0.0"/></a>
  <a href="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml"><img src="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml/badge.svg" alt="Tests CI"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8e8e98?style=flat-square&labelColor=0d0d11" alt="MIT License"/></a>
</p>

A local token, cache, and quota optimizer plugin for **Google Antigravity**.

Coding agents burn quota by pulling large noisy command logs, lockfiles, and repeated file slices into context — where every byte gets replayed on later turns — and by letting long sessions grow past 200k–500k tokens where idle pauses trigger full uncached cache rebuilds. `agy-tokensaver` filters noisy tool I/O locally with fail-open Perl hooks, keeps raw outputs on disk, and provides an optional full-page dashboard (`agy-tokensaver`) for tracking usage and archiving compact handoffs.

---

## Install & Update

**Installer:** download `agy-tokensaver-v1.0.0.zip` from **[Releases](https://github.com/menscho/agy-tokensaver/releases)**, extract, and run **`Install-TokenSaver.bat`** (Windows), **`Install-TokenSaver.command`** (macOS), or `python3 install.py`.

**Or from a terminal:**
```bash
# Linux / macOS / WSL
curl -fsSL https://raw.githubusercontent.com/menscho/agy-tokensaver/main/install.sh | bash
```
```powershell
# Windows PowerShell
irm https://raw.githubusercontent.com/menscho/agy-tokensaver/main/install.ps1 | iex
```

**Updates:** open **`agy-tokensaver`** in the Antigravity sidebar → **Updates** tab → **Install Update** (or re-run the installer). Updates pull **only from tagged GitHub Releases** after checking **`SHA256SUMS.txt`** from the same release (catches corrupted downloads; not a separate cryptographic signature). Local stats, settings, and archived handoffs live in `~/.gemini/antigravity/plugin_data/token-saver/` and are never overwritten.

*(Requires Python 3.9+ stdlib only; on Windows, `install.py` pins the active Python path in `mcp_config.json` to avoid the Microsoft Store `python3` stub. Perl 5 is required for the hooks.)*

---

## Reproducible Benchmarks

Every number below is produced directly by [`scripts/benchmark.py`](scripts/benchmark.py) — no hand-estimated multipliers.

### 1. Measured Before / After Task (Auto-Handoff OFF — Hooks Only)

Reproduce locally in ~1 second (also verified in CI):
```bash
python3 scripts/benchmark.py --demo
```

Runs a deterministic 8-step noisy-task fixture through `read_hook.pl`, `cmd_hook.pl`, and `cmd_wrap.pl`. Each command step inspects `cmd_hook.pl`'s actual rewrite output (failing if a command is not wrapped on POSIX) and verifies the target signal survives compression. Baseline counts are capped at Antigravity's native limits (`46,080 B` for `read_file`, `10,136 B` for `run_command`), and post-hook counts include the `94 B` PreToolUse overwrite notice.

| Task Step | Without Plugin | With Plugin | Saved | Result |
|---|---:|---:|---:|---|
| 1. `read_file` `package-lock.json` (unbounded) | 11,520 tok | 41 tok | -99.6% | blocked (hint emitted) |
| 2. `read_file` `service.py` (900L unbounded) | 11,520 tok | 4,844 tok | -58.0% | capped to lines 1–250 |
| 3. `read_file` `service.py:50-120` (dup slice) | 1,359 tok | 36 tok | -97.4% | blocked (already in context) |
| 4. `run_command` `pytest` (300 pass, 1 fail) | 2,534 tok | 108 tok | -95.7% | `--cat test`: assertion + traceback kept |
| 5. `run_command` `git diff` (code + lockfile) | 2,534 tok | 107 tok | -95.8% | `--cat git_diff`: code hunk verbatim |
| 6. `run_command` server log (900L + 1 ERROR) | 2,534 tok | 1,550 tok | -38.8% | `--cat logs`: `ERROR` pinned + raw saved |
| 7. `run_command` `curl` JSON API (120 rows) | 2,534 tok | 940 tok | -62.9% | `--cat http`: CSV+schema & error row kept |
| 8. `run_command` `ssh` remote grep (74 hits) | 1,847 tok | 959 tok | -48.1% | `--cat ssh`: lossless path hoisting |
| **Total direct tool tokens added to context** | **36,383 tok** | **8,588 tok** | **-76.4%** | **27,795 tokens avoided** |
| **15-turn weighted quota (`1×` in + `14×0.05×` cache)** | **61,851 qu** | **14,599 qu** | **-76.4%** | **47,252 quota units avoided** |

Measured median `PreToolUse` hook wall-clock time across the 8 calls: **~8 ms** (including process spawn). This demo is a noisy-task fixture (lockfile, 900-line file, 300-test run, lockfile diff, 900-line log, 120-row JSON API, 74-hit remote grep); typical sessions with small clean reads and short commands will save much less.

### 2. Real-Session Replay (all local Antigravity sessions — IDE + CLI)

Reproduce on your own data:
```bash
python3 scripts/benchmark.py --replay --json after.json      # = scripts/transcript_replay.py
python3 scripts/make_benchmark_chart.py --after after.json [--before before.json]
```

<p align="center">
  <img src="docs/assets/benchmark-chart.svg" alt="Real-session replay across 65 local Antigravity sessions" width="100%"/>
</p>

`transcript_replay.py` loads every local session (`~/.gemini/antigravity` and `~/.gemini/antigravity-cli`), takes **exact per-turn tokens** from the transcript or, when the transcript has none, from `conversations/<id>.db` (`gen_metadata`), and **replays every recorded tool call and its recorded result through the real hooks** (read guard on a reconstructed virtual file, `cmd_hook.pl` → `cmd_wrap.pl --test-stdin` on the recorded output, find/grep guards). Removed bytes leave the context of every later turn until the next compaction; denials that force a retry, doc caps that force a follow-up read, and the plugin's own always-loaded prompt (`433 tok/turn`, cut down from `906 tok/turn`) are **charged**. Auto-Handoff is simulated causally (no future knowledge) and separated into **live-reachable** (only between agent runs where the live tick can fire) vs. **upper bound** (any generation).

Measured across **65 real sessions / 3,602 turns** (`4,944,854` recorded tool-result tokens; `213,975` removed by hooks = `4.3%`):

| Session Length | n | Avg Peak | Hooks Only | Handoff (Live) | Hooks + Handoff (Live) | Handoff (UB) | Hooks + Handoff (UB) |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Short (`<75k`)** | 19 | 41k | +0.1% | 0.0% | +0.1% | 0.0% | +0.1% |
| **Medium (`75k–110k`)** | 7 | 91k | +0.1% | +0.7% | +0.8% | -1.8% | -1.8% |
| **Long (`110k–200k`)** | 9 | 157k | 0.0% | -2.2% | -2.2% | -14.0% | -14.0% |
| **Very long (`200k–400k`)** | 22 | 297k | -1.1% | -10.5% | -11.4% | -34.9% | -35.2% |
| **Extreme (`>400k`)** | 8 | 479k | -0.6% | -27.3% | -27.7% | -38.7% | -38.9% |
| **Per-session median** | **65** | — | **+0.1%** | **0.0%** | **+0.1%** | **-7.5%** | **-8.5%** |
| **Per-session mean** | **65** | — | **-0.3%** | **-5.1%** | **-5.3%** | **-15.7%** | **-15.7%** |
| **Total quota-weighted** | **65** | — | **-0.7%** | **-16.5%** | **-17.1%** | **-32.8%** | **-33.0%** |

*(Negative = less quota used; positive = more quota used. Improved sessions hooks-only: 27/65; regressed: 37/65 — mostly short chats paying the 433-token prompt overhead without hitting a guard. Regressed sessions on live Auto-Handoff alone: 4/65.)*

**Honest reading:** on typical multi-turn sessions, model output (`6×` weight) and first-time reads of code files dominate quota, so hooks alone save `-0.7%` aggregate (`-1.1%` on 200k–400k sessions, while removing `213.9k` noisy tool tokens). Where real quota drops steeply is long multi-run sessions (`200k–400k+`) with causal Auto-Handoff enabled (`-17.1%` live-reachable aggregate; `-27.7%` on `>400k` sessions). Blocked reads can also cost an extra turn if the model tries to work around a denial, which is why the replay charges retry turns and duplicate searches include a 1-retry escape hatch.

---

## Native HUD

Opens from **`agy-tokensaver`** under **Plugins** in Antigravity's left sidebar. Internal dashboard tools use `visibility: ["app"]` so they are hidden from the model prompt.

<p align="center">
  <img src="docs/assets/hud-preview.svg" alt="agy-tokensaver HUD Preview" width="100%"/>
</p>

- **Overview:** Input/Cache/Output/Quota breakdown, live context meter, rollover mode selector (`Archive Only` default / `Smart Auto` / `Off`), and manual actions.
- **Archive:** readable history of saved compact handoffs (workspace, branch, commits, diff stat, touched files, last goal, and last assistant summary).
- **Updates:** tagged GitHub Release check, local requirement checks, changelog, and SHA-256 verified 1-click updates.

---

## Architecture & Safety Invariants

- **Shell hooks (`scripts/cmd_hook.pl`, `cmd_hook_core.pl`, `cmd_wrap.pl`):** Stdlib Perl (zero external modules). Skips redirects, heredocs, command substitutions, `tee`, `-f`/`--follow`, background jobs, interactive TTYs, and unknown chain segments. Includes lossless JSON CSV+schema compaction (`[N]{cols}` + blob offload + error-preserving SmartCrusher sampling past 9.9 KB), dense minified-line elision, repeated multi-line block folding, large-diff `±2` context trimming, Windows-safe & SSH remote grep path hoisting, duplicate search / unchanged `git diff` dedup (with a 1-retry escape hatch), and a `WaitMsBeforeAsync` floor bump (`5000 ms`) on fast read-only queries. Every lossy cut spills the unmodified output to the OS temp directory (`agy-tokensaver-<uid>`).
- **Read, Write, Edit, Find & Grep guards (`read_hook.pl`, `write_hook.pl`, `post_edit_hook.pl`, `find_hook.pl`, `grep_hook.pl`, `ts_inspect.py`):** Blocks binaries, SQLite DBs, lockfiles, sourcemaps, minified bundles, and unchanged re-reads (verified against transcript compaction checkpoints, `initialNumSteps` drops, `mtime`, `size`, MD5, and `PostToolUse` edit invalidation; partial overlaps trim to missing lines only). Auto-caps unbounded data (`>8 KB`) and code (`>16 KB`) files while leaving prose docs up to 46 KB uncapped. `ts_inspect.py outline` provides stdlib AST/regex `L<start>-L<end>` symbol outlines. `grep_hook.pl` fixes Windows single-file `grep_search`. `write_hook.pl` only blocks full overwrites that shrink a `>150`-line file by `≥40%` with lazy omission placeholders.
- **Rules & Scouts (`rules/AGENTS.md`, `agents/*.md`, `workflows/*.py`):** Compact ~0.7 KB ruleset (`433 tok/turn` total prompt overhead including skills/agents), 3 Flash/Flash-Lite scouts (`scout`, `lite-sifter`, `vision-scout`), and 3 isolated `/lean-workflow` scripts.

### Compatibility & Limitations
- **Antigravity IDE vs. CLI hooks:** Hooks fire reliably in Antigravity 2.0 and the `agy` CLI (verify in 5 seconds via `/hooks` or by checking whether hook events increment in the `agy-tokensaver` HUD). Some older desktop IDE builds may not invoke plugin lifecycle hooks yet — on those builds `rules/AGENTS.md`, skills, scouts, `ts_inspect.py`, `handoff_pack.py`, and the HUD still work, while automatic hook filtering stays inactive.
- **Antigravity internals:** Hooks and live MCP refresh depend on Antigravity's current hook events (`PreToolUse`, `PostToolUse`, `PostInvocation`, `Stop`), payload fields, and local `RefreshMcpServers` RPC. If payload shapes change, hooks fail open to pass-through without breaking tool calls.
- **Cross-platform (Windows PowerShell + POSIX):** On Windows (`MSWin32`, or Git/MSYS Perl with a drive-letter `Cwd`), `cmd_hook.pl` wraps noisy PowerShell commands through a child `powershell.exe` with exact exit codes, lossless table/header compaction, and raw spill to `%TEMP%` (`AGY_TS_SHELL=off|posix|pwsh` overrides). POSIX `bash -c` wrapping runs on Linux, macOS, and WSL.

---

## Tests

```bash
for t in tests/run_tests.sh tests/test_*.sh; do bash "$t"; done
python3 scripts/benchmark.py --demo
```

Verified on Ubuntu and macOS (`291` assertions across 7 test suites + MCP/installer/demo checks) and native Windows (Python smoke + `C:\` Perl hook + PowerShell + demo checks) in [`.github/workflows/test.yml`](.github/workflows/test.yml).

---

## License

[MIT License](LICENSE).
