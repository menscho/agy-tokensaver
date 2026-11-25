# Performance, Benchmarks and Latency

Every figure below comes directly from running commands in this repository (`scripts/benchmark.py` and `eval/runner.py`). Pass `--weights` to test different pricing ratios.

![Real-Session Benchmark Chart](assets/benchmark-chart.svg)

## 1. Real-Session Replay (`python3 scripts/benchmark.py --modes --report`)

Replayed across **79 local Antigravity sessions** (`4,294` turns, `123.3M` cost-proxy units; **0 tokens/turn** always-loaded prompt overhead by default; cost mix `37.9%` input / `32.3%` cache read / `29.8%` output).

| Mode | Tool Tok Cut | Median Session | Avg Cost Saved | p90 Session | Max Session | >5% Share |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| **Auto-Handoff + Read Guards (Default)** | `177,153 tk` | `0.0%` | **19.4% lower** | `25.3%` | `46.7%` | `15.2%` |
| **Auto-Handoff + Compact Reads** | `279,326 tk` | `0.0%` | **19.3% lower** | `25.2%` | `46.4%` | `15.2%` |
| **Guards Only (Same Chat)** | `177,153 tk` | `0.0%` | **0.5% lower** | `1.7%` | `6.8%` | `1.3%` |
| **Guards + Compact Reads (Same Chat)** | `279,326 tk` | `0.0%` | **0.6% lower** | `2.3%` | `11.7%` | `2.5%` |

### Breakdown by Session Length (`python3 scripts/benchmark.py --replay`)

| Tier | Sessions (`n`) | Avg Peak | Guards Only | Auto-Handoff Only | Combined (Default) | Upper Bound |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| Short (`<75k`) | `31` | `49k` | `0.2%` lower | `0.0%` | `0.2%` lower | `0.2%` lower |
| Medium (`75k–110k`) | `8` | `91k` | `0.1%` lower | `+0.6%` higher | `+0.5%` higher | `4.6%` lower |
| Long (`110k–200k`) | `10` | `155k` | `0.1%` lower | `2.1%` lower | `2.2%` lower | `13.1%` lower |
| Very long (`200k–400k`) | `22` | `297k` | `0.9%` lower | `11.2%` lower | **12.1% lower** | `35.7%` lower |
| Extreme (`>400k`) | `8` | `479k` | `0.3%` lower | `28.7%` lower | **28.9% lower** | `43.2%` lower |

## 2. Live Antigravity Evaluation (`eval/results/live_eval.json`)

Executed via `ConversationService` on `flash` across **10 tasks × 3 runs × 3 modes = 90 runs** (`6` standard coding/log/lockfile tasks + `4` multi-step 2-phase handoff continuity tasks `H01`–`H04`):

```bash
python3 eval/runner.py --driver antigravity \
  --tasks T01,T07,T14,T19,T25,T30,H01,H02,H03,H04 \
  --modes off,default,max --runs 3 --workers 4 --model flash \
  --out eval/results/live_eval.json
```

| Mode | Total Pass Rate | 95% Wilson CI | Standard Tasks (`T*`, 18 runs) | Handoff Continuity (`H*`, 12 runs) | Retries |
| :--- | ---: | ---: | :--- | :--- | ---: |
| **off** | `30/30` (`100.0%`) | `88.6..100.0%` | `18/18` (`1,210,112` tok; `21,569` out) | `12/12` single-session (`1,187,908` tok) | `0` |
| **default** | `30/30` (`100.0%`) | `88.6..100.0%` | `18/18` (**`-2.0%` tok**, `-2.1%` cost proxy) | `12/12` via fresh chat (`296` tok pkt; `+21.9%` tok) | `0` |
| **max** | `30/30` (`100.0%`) | `88.6..100.0%` | `18/18` (**`-9.8%` tok**, **`-16.8%` out**, `-8.5%` cost proxy) | `12/12` via fresh chat (`286` tok pkt; `+19.8%` tok) | `0` |

*Key finding:* fresh-chat handoffs preserved 100% pass rates across all 24 active continuity runs (`12` Default, `12` Max), while forcing handoffs on short 60k tasks raised total tokens by `+19.8..+21.9%` due to repeating the cold start. This validates gating automatic rollovers behind the `>=75k` cold-cache and `>=110k` adaptive cap thresholds.

## 3. Cost-Proxy Sensitivity (`--weights`)

| Weight Regime (`in / cache / out`) | Basis | Guards Only | Guards + Compact | Default (Auto-Handoff) | + Compact Reads |
| :--- | :--- | ---: | ---: | ---: | ---: |
| **Default (`1 / 0.05 / 6`)** | Frontier API ratio proxy | `0.5%` | `0.6%` | `19.4%` | `19.3%` |
| **Anthropic-style (`1 / 0.10 / 5`)** | 10% cache read / 5× out | `0.5%` | `0.7%` | `22.7%` | `22.6%` |
| **Output-light (`1 / 0.05 / 3`)** | Lower output multiplier | `0.5%` | `0.8%` | `22.7%` | `22.7%` |
| **Cache-heavy (`1 / 0.25 / 5`)** | Expensive cache reads | `0.6%` | `0.8%` | `25.7%` | `25.6%` |

## 4. Synthetic Hook Fixture (`python3 scripts/benchmark.py --demo`)

The 7-step deterministic hook benchmark exercises blocked lockfiles, large source/JSON/log auto-caps, duplicate slice blocks, `node_modules` excludes, and lazy-truncation write blocks: direct tool tokens drop **75.5%** (`39,557` -> `9,694` tokens, median hook call latency **16.0 ms**). Do not extrapolate this synthetic worst-case figure to normal editing sessions.
