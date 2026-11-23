---
name: token-stats
description: >-
  Show live (net, notice-adjusted) token savings, prompt cache-hit rates, read_file blocks/caps, CLI
  compression ratios, and hook latency overhead recorded by the token-saver plugin.
disable-model-invocation: true
---

# Token Saver — Usage & Cache-Hit Dashboard

Run the local telemetry aggregator script and present its report cleanly to the user:

```bash
python3 "$HOME/.gemini/config/plugins/token-saver/scripts/stats.py"
```

If the user passes a specific timeframe (e.g. "today", "7d", "all") or asks for raw per-session breakdown, pass `--days 1`, `--days 7`, or `--sessions` to `stats.py`.

Present the key metrics clearly:
1. Total tokens saved (direct + estimated replay savings) and compression ratio.
2. Prompt cache-hit rate (`cache_read_tokens / (input_tokens + cache_read_tokens)`) and any detected cache-drop events.
3. Savings breakdown by layer (`cmd_wrap` CLI categories vs. `read_guard` blocks/caps).
4. Measured hook overhead (ms) to verify speed stays within budget.
5. Concrete recommendations if any cache drops or high-volume unfiltered patterns were detected.
