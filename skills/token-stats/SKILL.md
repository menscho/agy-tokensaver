---
name: token-stats
description: User-only. Token-saver savings and cache stats.
disable-model-invocation: true
---

# Token Saver — Usage & Cache-Hit Dashboard

Shows live (net, notice-adjusted) savings, prompt cache-hit rate, read_file blocks/caps, CLI compression ratios and hook latency recorded by the plugin.

Run the local telemetry aggregator script and present its report cleanly to the user:

```bash
python3 "$HOME/.gemini/config/plugins/token-saver/scripts/stats.py"
```

If the user passes a specific timeframe (e.g. "today", "7d", "all") or asks for raw per-session breakdown, pass `--days 1`, `--days 7`, or `--sessions` to `stats.py`.

Present the key metrics clearly:
1. Total tokens saved (direct + estimated replay savings) and compression ratio.
2. Prompt cache-hit rate (`cache_read_tokens / (input_tokens + cache_read_tokens)`) and any detected cache-drop events.
3. Savings breakdown by layer (`read_guard` blocks/caps, `write_guard`, and Auto-Handoff rollovers).
4. Measured hook overhead (ms) to verify speed stays within budget.
5. Concrete recommendations if any cache drops or large repeated file reads were detected.
