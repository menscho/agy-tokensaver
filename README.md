# agy-tokensaver

<p align="center">
  <a href="https://github.com/menscho/agy-tokensaver/releases"><img src="https://img.shields.io/badge/release-v1.0.0-4ade80?style=flat-square&labelColor=0d0d11" alt="Release v1.0.0"/></a>
  <a href="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml"><img src="https://img.shields.io/badge/tests-304%20passing-4ade80?style=flat-square&labelColor=0d0d11" alt="304 Tests Passing"/></a>
  <a href="https://github.com/menscho/agy-tokensaver/actions/workflows/test.yml"><img src="https://img.shields.io/badge/CI-Ubuntu%20%7C%20macOS%20%7C%20Windows-8e8e98?style=flat-square&labelColor=0d0d11" alt="3-OS CI"/></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8e8e98?style=flat-square&labelColor=0d0d11" alt="MIT License"/></a>
</p>

A local token, cache, and quota optimizer for **Google Antigravity** (CLI & IDE), with a full-page native HUD (`agy-tokensaver`).

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

**Updates:** open **`agy-tokensaver`** in the Antigravity sidebar → **Updates** → **Install Update**. Updates install only from tagged GitHub Releases after checking `SHA256SUMS.txt` from the same release (catches corrupted downloads; not a separate cryptographic signature). Local stats, settings, and archived handoffs in `~/.gemini/antigravity/plugin_data/token-saver/` are never overwritten.

*(Requires Python 3.9+ stdlib and Perl 5 stdlib — zero external packages or API keys.)*

---

## Where Your Antigravity Quota Actually Goes

Across **68 real Antigravity sessions (3,705 turns, 102.7M weighted quota units)** measured with `scripts/benchmark.py --modes`, quota breaks down into three almost-equal buckets:

- **37.5% — Uncached Input (`1×` weight):** over **half (`19.1M` tokens = `19%` of total quota)** came from just **109 prompt-cache drops** (idle pauses `>2 min` on 150k–450k chats forcing a full uncached rebuild).
- **31.6% — Cache Reads (`0.05×` weight):** replaying a growing 150k–500k context window across dozens of turns ($O(T^2)$ growth).
- **30.9% — Model Output (`6×` weight):** every generated token costs 6× an uncached input token and 120× a cached token.

**Why this matters:** pure tool-output hooks only touch *new tool results* (~4–5% of total quota on clean chats, though **-76.4%** on noisy logs/tests/JSON). To cut **24–44% of real total quota**, you also have to attack **6× output tokens** and **long-session context / cache drops**. That is why `agy-tokensaver` ships with **two simple modes** in the HUD:

- **Mode 1 — `Zero Quality Loss` (`0.0%` loss, Default ON):** strictly lossless compression, duplicate/loop guards, prompt-overhead diet, and `Archive Only` handoffs. Never truncates code mid-logic, never spawns chats automatically, never alters prose.
- **Mode 2 — `Small Quality Loss` (`~0.1%–1.0%` risk, Opt-in):** one toggle in the HUD enables **Compact Reads** (`~0.1–0.5%` risk), **Concise Output** (`~0.1%` prose risk), and **Anchored Auto-Handoff** (`0%` at phase switches / `~0.5–1.0%` mid-debug risk).

---

## 2-Mode Real-Session Benchmark (68 Sessions / 3,705 Turns)

Reproduce on your own Antigravity history:
```bash
python3 scripts/benchmark.py --modes    # exact 2-mode comparison
python3 scripts/benchmark.py --replay   # full tier-by-tier breakdown
```

| Mode | Stated Quality Loss | Tool Tokens Cut | Avg Total Quota Saved |
|---|---|---:|---:|
| **Mode 1: Zero Quality Loss** *(Default ON)* | **0.0%** *(exact replay)* | 217,668 tok | **-0.7%** *(noisy tasks: **-76.4%**)* |
| **Mode 2a: Small Loss — Compact Reads only** | **~0.1–0.5%** *(exact replay)* | 321,903 tok | **-1.0%** |
| **Mode 2b: Small Loss — + Concise Output\*** | **~0.1%** prose *(modeled\*)* | 321,903 tok | **-7.2% to -11.8%** |
| **Mode 2c: Small Loss — + Auto-Handoff (Live)** | **~0.5–1.0%**\*\* *(exact causal replay)* | 321,903 tok | **-18.0%** |
| **Mode 2 (Full): Live + Compact + Concise\*** | **~0.1–1.0%**\*\* | 321,903 tok | **-24.2% to -28.8%** |
| **Mode 2 (Upper Bound): Phase Resets + Concise\*** | **~0.1–1.0%**\*\* | 321,903 tok | **-39.7% to -44.3%** |

*\* **Concise Output** (*Sketch-of-Thought* `arXiv:2503.05179` + Caveman-lite) cannot be replayed on past transcripts without re-calling the LLM; rows 2b, Full, and Upper Bound apply a conservative `-20%` to `-35%` reduction to the measured `30.9%` (`6×`-weighted) output bucket (papers report `-65%` to `-76%`). Mode 1, 2a, and 2c are exact deterministic replays charging retries, follow-up reads, and the plugin's `383 tok/turn` always-loaded prompt overhead.*  
*\*\* **Anchored Auto-Handoff** (*The Complexity Trap* `arXiv:2508.21433` + *Focus* `arXiv:2601.07190`): `0%` to `+1.4 pp` quality at phase switches; `~0.5–1.0%` risk if triggered mid-debug. **Live** only fires between finished agent runs where the background tick can actually run; **Upper Bound** shows the ceiling when resetting at task/phase boundaries.*

<p align="center">
  <img src="docs/assets/benchmark-chart.svg" alt="2-Mode Real-Session Benchmark across 68 Antigravity sessions" width="100%"/>
</p>

### What Each Mode Enables (Pros, Cons & Exact Quality Risk)

| Technique | Mode | Stated Quality Loss | What It Does & Trade-Offs |
|---|---|---|---|
| **Lossless Hooks & Deduplication** | Mode 1 (`zero_loss`) | **0.0%** | Blocks lockfiles/binaries/sourcemaps, crushes JSON arrays to lossless `[N]{cols}` CSV+schema, hoists repeated grep/SSH paths, runs **Merlin** (`arXiv:2605.09990`) non-adjacent exact chunk dedupe, wraps multiline diagnostic chains + `ffmpeg` + PowerShell tables, breaks 3× identical failing test/build/lint loops (**TokenEconomizer** `arXiv:2610.22732`, 1-retry escape), trims prompt overhead to `383 tok/turn` (**SkillReducer** `arXiv:2603.29919`), and saves raw output to temp dir on every trim. |
| **Compact Reads** (`120L` code / `140L` docs) | Mode 2 (`small_loss`) | **~0.1–0.5%** | Cuts **+48% more tool tokens** (`218k → 322k`) on unbounded reads while leaving bounded `StartLine..EndLine` slices untouched. *Con:* if the needed function sits below line 120 in an unbounded read, the agent spends 1 extra targeted read turn (already charged in Mode 2a). |
| **Concise Output** (*SoT* + Caveman-lite) | Mode 2 (`small_loss`) | **~0.1% prose** | Cuts **`-6.2%` to `-10.8%` total quota even without Auto-Handoff** by stripping pleasantries, preambles, and pre/post-tool chatter from `6×`-weighted output. Code, diffs, paths, errors, and root-cause debugging stay verbatim. *Con:* replies are terse; toggle only between sessions to protect prompt-cache prefixes. |
| **Anchored Auto-Handoff** (`smart_auto`) | Mode 2 (`small_loss`) | **0% phase / ~0.5–1% mid-debug** | Biggest single win on long sessions (**`-18.0%` live-reachable**, **`-27.7%` on `>400k` chats**), preventing both $O(T^2)$ cache replay and 200k-token idle cache-drop rebuilds. Includes recent ruled-out errors in the packet. *Con:* opens a fresh chat with a structured packet (details below). |

---

## What Auto-Handoff Actually Does (And Does the Model Forget Everything?)

This is the most common question, so here is the plain, honest answer:

1. **What it does:** When a long conversation crosses the adaptive threshold (~110k–180k tokens), `handoff_pack.py` generates a compact (~500-token) **Anchored Handoff** packet from your workspace and transcript:
   - Your **current goal** and the **assistant's last conclusion**
   - **Git branch, recent commits, diff stat, and exact list of edited files**
   - **Recent errors / ruled-out paths** (extracted from recent tool failures so the new chat doesn't repeat them)
   It saves this snapshot to the **Archive** tab. In **Mode 2 (`Small Quality Loss`)**, it also opens a fresh chat seeded with that packet (~17k starting tokens instead of 250k–500k).
2. **Does the model forget stuff?** **Yes — selectively.**
   - **What is kept:** your goal, edited file list, git state, recent ruled-out errors, last conclusion, and every file on disk (which the agent can still read at any time).
   - **What is dropped:** old terminal logs, dead-end debugging attempts from 60 turns ago, and earlier file slices that are no longer needed (the full original chat stays untouched in your Antigravity history and Archive tab).
3. **What is the real quality loss?**
   - **At a phase switch** (finished feature A → starting feature B, finished planning → implementing, or before taking a `>2 min` break): **0% quality loss** — and often *better* focus, because JetBrains' NeurIPS 2025 study (*The Complexity Trap*, `arXiv:2508.21433`) and *Focus* (`arXiv:2601.07190`) showed resetting stale observation bloat matches or beats (`+1.4 pp`) full-history solve rates while cutting cost 23–50%.
   - **Mid-debugging a subtle multi-file bug** where subtle context wasn't summarized yet: **~0.5–1.0% risk** — the fresh chat may spend 1–2 turns re-reading a file.
4. **Why Mode 1 (`Zero Quality Loss`) is the default:** Out of the box, Auto-Handoff runs in `Archive Only` mode — it never spawns a new chat without you. It archives snapshots and warns before huge idle cache drops so *you* can click **Archive & New Chat** (or type `/handoff`) right at natural boundaries. Switch the HUD to **`Small Quality Loss`** when you want hands-off resets + Compact Reads + Concise Output.

---

## 8-Step Noisy-Task Demo (`python3 scripts/benchmark.py --demo`)

Runs a deterministic 8-step noisy coding fixture directly through `read_hook.pl`, `cmd_hook.pl`, and `cmd_wrap.pl` (baseline capped at Antigravity's native limits: `46,080 B` read / `10,136 B` command; post-hook includes the `94 B` overwrite notice; median hook latency **~8 ms**):

| Task Step | Without | With | Saved | Result |
|---|---:|---:|---:|---|
| 1. `read_file` `package-lock.json` (unbounded) | 11,520 tok | 41 tok | -99.6% | blocked (hint emitted) |
| 2. `read_file` `service.py` (900L unbounded) | 11,520 tok | 4,844 tok | -58.0% | capped to lines 1–250 |
| 3. `read_file` `service.py:50-120` (dup slice) | 1,359 tok | 36 tok | -97.4% | blocked (already in context) |
| 4. `run_command` `pytest` (300 pass, 1 fail) | 2,534 tok | 108 tok | -95.7% | `--cat test`: assertion + traceback kept |
| 5. `run_command` `git diff` (code + lockfile) | 2,534 tok | 107 tok | -95.8% | `--cat git_diff`: code hunk verbatim |
| 6. `run_command` server log (900L + 1 ERROR) | 2,534 tok | 1,550 tok | -38.8% | `--cat logs`: `ERROR` pinned + raw saved |
| 7. `run_command` `curl` JSON API (120 rows) | 2,534 tok | 940 tok | -62.9% | `--cat http`: CSV+schema & error row kept |
| 8. `run_command` `ssh` remote grep (74 hits) | 1,847 tok | 959 tok | -48.1% | `--cat ssh`: lossless path hoisting |
| **Total direct tool tokens** | **36,383 tok** | **8,588 tok** | **-76.4%** | **27,795 tokens avoided** |
| **15-turn weighted quota (`1×` in + `14×0.05×` cr)** | **61,851 qu** | **14,599 qu** | **-76.4%** | **47,252 quota units avoided** |

*(This fixture represents a noisy test/log/JSON/diff workflow; clean small-file sessions save much less with hooks alone, as shown in the 68-session replay above.)*

---

## Native HUD

Opens from **`agy-tokensaver`** under **Plugins** in Antigravity's left sidebar. Internal dashboard tools use `visibility: ["app"]` so they add zero tokens to the model prompt.

<p align="center">
  <img src="docs/assets/hud-preview.svg" alt="agy-tokensaver HUD Preview" width="100%"/>
</p>

- **Overview:** Input/Cache/Output/Quota breakdown, live context meter, one-click **Optimization Mode** (`Zero Quality Loss` vs `Small Quality Loss`), Context Limit selector, and manual actions.
- **Archive:** Saved Anchored Handoff packets (goal, branch, commits, diff stat, edited files, ruled-out errors, last summary).
- **Updates:** Tagged GitHub Release check, local requirement checks, changelog, and SHA-256 verified 1-click updates.

---

## Research & Open-Source Foundations

Every technique in `agy-tokensaver` is grounded in 2024–2026 coding-agent efficiency papers and verified open-source tools (while deliberately excluding techniques shown to hurt cost or quality):

- **Zero-Loss Research (`0.0%` quality loss):**
  - **Merlin** (`arXiv:2605.09990`): byte-exact non-adjacent chunk deduplication (`cmd_wrap.pl` `merlin_dedupe_chunks`, 24–80% reduction on repeated log/SSH diagnostics with 0% info loss).
  - **TokenEconomizer** (`arXiv:2610.22732`): failure-loop breaker stopping 3× identical failing `test`/`build`/`lint` runs with no intervening edits (18–31% cost cut + Pass@1 gain).
  - **SkillReducer** (`arXiv:2603.29919`) & **Same Task, Different Work** (`arXiv:2608.01347`): tightened always-loaded prompt/skill descriptions from `906 → 383 tok/turn` (-58%) plus bounded-scope stop rules.
  - **Don't Break the Cache** (`arXiv:2601.06007`): strictly append-only prefix hygiene + pre-drop idle cache-rebuild cost warnings.
- **Small-Loss Research (`~0.1%–1.0%` risk, opt-in Mode 2):**
  - **The Complexity Trap** (JetBrains, NeurIPS 2025, `arXiv:2508.21433`) & **Focus** (`arXiv:2601.07190`) & **Factory.ai Anchored Summarization**: threshold-based Anchored Handoff packets with ruled-out errors (23–50% cost cut, `0%` to `+1.4 pp` at phase boundaries).
  - **Sketch-of-Thought** (`arXiv:2503.05179`), **TALE-EP** (`arXiv:2412.18547`), & **[Caveman](https://github.com/JuliusBrussee/caveman):** Concise Output rule cutting `6×`-weighted prose chatter while keeping code, diffs, paths, and root-cause debugging verbatim.
- **Open-Source Ports:**
  - **[headroomlabs-ai/headroom](https://github.com/headroomlabs-ai/headroom):** lossless JSON `[N]{cols}` CSV+schema compaction, nested flattening, `>256 B` blob offload, 12-keyword error-preserving SmartCrusher sampling, dense minified-line elision, multi-line block folding, large-diff `±2` context trimming, and Windows-safe / SSH grep path hoisting.
  - **[@plexer/cli](https://www.npmjs.com/package/@plexer/cli):** `PostToolUse` edit invalidation (`post_edit_hook.pl`), duplicate search / unchanged `git diff` dedup, `WaitMsBeforeAsync` floor bump (`5000 ms`), `initialNumSteps` compaction reset, and stdlib AST/regex symbol outlining (`ts_inspect.py outline`).
- **Deliberately Excluded Anti-Patterns (Proven Harmful):** lexical code minification (`arXiv:2606.01326`: **-12 pp** SWE-bench drop), per-turn sliding history mutation (`arXiv:2611.22238`: **+38–116%** cost by breaking KV caches), unsolicited Turn-0 snippet pre-injection (`arXiv:2611.22238`: **+33–42%** net cost), and global Chain-of-Draft on complex debugging (`arXiv:2506.10987`: up to **-10%** drop).

---

## Compatibility, Limitations & Tests

- **Antigravity IDE vs. CLI hooks:** Hooks fire reliably in Antigravity 2.0 and the `agy` CLI (verify via `/hooks` or by watching hook events increment in the HUD). Older desktop IDE builds that don't fire plugin hooks yet still benefit from `rules/AGENTS.md`, skills, scouts, `ts_inspect.py`, `handoff_pack.py`, and the HUD.
- **Cross-platform:** Full CLI wrapping runs on Linux, macOS, WSL, **and native Windows PowerShell** (`cmd_wrap.pl --pwsh-stdin` via child `powershell.exe` with exact exit codes, table compaction, and `%TEMP%` spillback; `AGY_TS_SHELL=off|posix|pwsh` overrides).
- **Fail-open invariant:** Every hook fails open (`{"decision":"allow"}` / `{}`) on any malformed input or Antigravity payload change, and every lossy compression spills the unmodified raw output to disk first.

```bash
for t in tests/run_tests.sh tests/test_*.sh; do bash "$t"; done
python3 scripts/benchmark.py --demo --modes
```

Verified in [`.github/workflows/test.yml`](.github/workflows/test.yml) across **Ubuntu, macOS, and Native Windows** (`304` assertions across 7 test suites + MCP/installer/PowerShell/demo checks).

## License

[MIT License](LICENSE).
