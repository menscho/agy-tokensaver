# Performance, Benchmarks & Hook Latency

Every figure below comes directly from running commands in this repository. Never treat cost-proxy weights as Antigravity's unpublished billing formula; pass `--weights` to test your own pricing model.

## 1. Real-Session Replay (`python3 scripts/benchmark.py --modes --report`)

Replayed across **79 local Antigravity sessions** (`4,176` turns, `118.2M` cost-proxy units; **190 tok/turn** always-loaded prompt overhead charged on every turn: `38.0%` input / `32.0%` cache read / `30.0%` output).

**Why Compression Savings vs. Auto-Handoff Savings Are Shown Separately:**
- **Hook Compression (`Mode 1` / `Mode 2a`)** runs inside the current conversation: it strips noisy test/build/log/git/JSON/SSH output (keeping byte-exact raw output on disk), caps oversized file reads, and blocks duplicate reads/loops. It never resets the chat.
- **Auto-Handoff (`Mode 2c` / `Auto-Handoff Only`)** achieves large average cost reductions only on long sessions by **starting a fresh conversation** once a settled `>=75k` chat hits cold-cache or cap thresholds. That drops verbatim older turns beyond the generated handoff packet.

| Mode / Mechanism | Measurement | Tool Tok Cut | Median Session | Avg Cost Saved | p90 Session | Max Session | >5% Share |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| **Mode 1: Lossless Compression (Default)** | Measured replay | `224,550 tk` | **`+0.1%`** | **`-0.7%`** | `-1.7%` | `-6.8%` | `1.3%` |
| **Mode 2a: + Compact Reads (Opt-in)** | Measured replay | `327,605 tk` | **`+0.1%`** | **`-0.8%`** | `-2.2%` | `-11.6%` | `2.5%` |
| **Auto-Handoff Only** *(New Chats, Mode 1 hooks)* | Measured replay | `224,550 tk` | **`-0.0%`** | **`-19.2%`** | `-25.7%` | `-46.5%` | `13.9%` |
| **Mode 2c: Compact Reads + Auto-Handoff** | Measured replay | `327,605 tk` | **`+0.1%`** | **`-19.7%`** | `-25.7%` | `-46.4%` | `15.2%` |

*Note on the positive (`+0.1%`) median:* short plain-editing sessions with few noisy command outputs pay the fixed `190 tok/turn` rule/skill prompt cost (`~0.1–0.2%`) without offsetting compression hits.

### Breakdown by Session Type

| Session Type | Sessions (`n`) | Mode 1 Avg | Mode 1 Median | Mode 1 p90 | Mode 2a Avg | Mode 2c Avg | Mode 2c p90 |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `log-heavy` | `56` | `-0.7%` | `-0.0%` | `-1.9%` | `-0.9%` | `-20.4%` | `-34.4%` |
| `plain-editing` | `23` | `-0.1%` | `+0.2%` | `+0.1%` | `-0.2%` | `-0.2%` | `+0.1%` |

## 2. Modeled Estimates (Concise Output — Cannot Replay Without an LLM)

`Mode 2b` and `Mode 2 (Max)` add an opt-in terse-reply instruction to `rules/AGENTS.md` (deferred until active warm caches cool). Because output token length changes require live model inference, **these ranges are modeled estimates**, computed by multiplying each session's recorded `output_tokens` bucket (`30.0%` of total cost proxy) by an assumed `-20%` to `-35%` reduction and adding that to measured hook/handoff deltas:

- **`Mode 2b: + Concise Output (no handoff)`:** `-6.8%..-11.3% (estimated)` avg (`-8.0%` median / `-14.0%` p90 at conservative `-20%` output cut).
- **`Mode 2 (Max): All opt-in`:** `-25.7%..-30.2% (estimated)` avg (`-8.5%` median / `-30.8%` p90 at `-20%`).
- **`Mode 2 (Upper Bound, manual phase resets)`:** `-41.5%..-46.0% (estimated)` avg (models manual `/handoff` resets at every settled phase switch instead of automated cold/cap triggers).

These are excluded from the main `README.md` table until measured in a live LLM evaluation (`eval/README.md`).

## 3. Cost-Proxy Weights & Sensitivity (`--weights`)

Because Google Antigravity does not publish an internal subscription quota formula, `benchmark.py` and the HUD use a **proxy weight vector** derived from public 2025–2026 frontier coding-model API pricing ratios (`1.0×` uncached input tokens, `0.05×` cached input reads, `6.0×` output tokens). Other model families price cache reads and output differently, so `--modes --report` evaluates four regimes over the exact same replayed token sums:

| Weight Regime (`in / cache / out`) | Basis | Mode 1 (Default) | Mode 2a | Auto-Handoff Only | Mode 2c |
| :--- | :--- | ---: | ---: | ---: | ---: |
| **Default (`1 / 0.05 / 6`)** | Frontier hybrid proxy | `-0.7%` | `-0.8%` | `-19.2%` | `-19.7%` |
| **Anthropic-style (`1 / 0.10 / 5`)** | 10% cache read / 5× output | `-0.8%` | `-1.0%` | `-22.7%` | `-23.2%` |
| **Output-light (`1 / 0.05 / 3`)** | Lower output multiplier | `-0.8%` | `-1.0%` | `-22.6%` | `-23.2%` |
| **Cache-heavy (`1 / 0.25 / 5`)** | Expensive cache reads | `-0.8%` | `-1.0%` | `-25.9%` | `-26.4%` |

## 4. Synthetic Worst-Case Workload (`python3 scripts/benchmark.py --demo`)

The 8-step synthetic fixture (`benchmark.py --demo`) tests extreme noisy tool outputs in isolation (unbounded `package-lock.json`, 900-line file, duplicate re-read, 301-case pytest fail, code+lockfile `git diff`, 900-line log with 1 buried `ERROR`, 120-row JSON API array, and 74-hit SSH grep). Under those synthetic conditions, direct tool tokens drop **`-76.4%`** (`36,383 → 8,594` tokens, `27,789` tokens avoided). **Do not expect `-76.4%` in normal sessions** — real coding chats mix many short file reads and edits with occasional noisy commands (compare `Mode 1: -0.7%` avg / `+0.1%` median above).

## 5. Hook Latency Budget

Measured on Linux (Perl v5.40.1, `JSON::PP` 4.16) via `python3 scripts/benchmark.py --demo` and Perl `Time::HiRes` microbenchmarks:

| Path | Target budget | Measured (median) | Notes |
| :--- | ---: | ---: | :--- |
| `cmd_hook.pl` fast-path miss (~83% of commands) | `< 10 ms` | `~2.5 ms` | Conservative keyword pre-check avoids loading `JSON::PP` and `cmd_hook_core.pl`. |
| `read_hook.pl` (active `JSON::PP` parse) | `< 30 ms` | `~14.1 ms` | Includes ~`6.1 ms` Perl `require JSON::PP` compile time + `0.03 ms` decode. |
| `cmd_hook.pl` hit (active `JSON::PP` parse) | `< 30 ms` | `~16.5 ms` | Loads `TsJson` (`JSON::PP`) + `cmd_hook_core.pl` quote-aware classifier. |
| `benchmark.py --demo` (8 hook hits) | `< 30 ms` | `16.6 ms` | Up from `8.9 ms` pre-`JSON::PP` (`require JSON::PP` load floor `6.11 ms`). |
| 10 MB malformed / oversized payload (all hooks) | `< 200 ms` | `15–30 ms` | `TsJson::read_stdin_capped()` stops past 2 MB and fails open to `allow`. |
