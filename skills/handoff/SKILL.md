---
name: handoff
description: User-only. Compact continuation packet to restart a 100K+ chat in a fresh one.
disable-model-invocation: true
---

# Session Handoff (`/handoff`)

Run the local generator (read-only: git `--no-optional-locks`, transcript read only):

```bash
python3 "$HOME/.gemini/config/plugins/token-saver/scripts/handoff_pack.py" "$(pwd)"
```

It prints the packet: workspace/branch, last goal, last assistant state, recent commits, one merged
list of uncommitted changes (`XY path (+a/-b)`), files edited in this conversation and files it
consulted. Add **at most 5 terse bullets** for in-memory decisions, verified root causes, or the exact
next step that the packet does not show, and output the result in a single fenced markdown block
ready to paste into a fresh chat.

Best moment: after an agent run has finished (not mid-run), especially when you return after the
prompt cache expired - resuming a 150K chat then re-reads all 150K uncached; the packet costs ~16K.
