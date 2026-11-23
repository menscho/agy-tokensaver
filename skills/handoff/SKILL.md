---
name: handoff
description: >-
  Generate a compact (~500-token) zero-loss session continuation packet so you
  can reset a 100K+ context chat into a fresh ~15K chat (saving 60-75% on long
  sessions) without losing any state or decisions.
disable-model-invocation: true
---

# Zero-Loss Session Handoff (`/handoff`)

Run the local handoff generator script:

```bash
python3 "$HOME/.gemini/config/plugins/token-saver/scripts/handoff_pack.py" "$(pwd)"
```

Then enrich the printed packet with **at most 5 terse bullets** capturing any in-memory decisions, verified root causes, or exact next step that aren't visible in `git status`, and output the final continuation prompt inside a single fenced markdown block ready for the user to paste into a fresh chat (or run `/compact`).
