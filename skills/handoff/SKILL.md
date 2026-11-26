---
name: handoff
description: User-only. Compact handoff packet for 100K+ chats.
disable-model-invocation: true
---

# Session Handoff (`/handoff`)

To immediately start a fresh continuation chat in the same project and archive this chat via `ConversationService`:

```bash
python3 "$HOME/.gemini/config/plugins/token-saver/scripts/handoff_exec.py" --workspace "$(pwd)"
```

Or to only print/copy the compact packet without opening a new chat (read-only: git `--no-optional-locks`, transcript read only):

```bash
python3 "$HOME/.gemini/config/plugins/token-saver/scripts/handoff_pack.py" "$(pwd)"
```

It prints the packet: workspace/branch, last goal, last assistant state, recent commits, one merged
list of uncommitted changes (`XY path (+a/-b)`), files edited in this conversation and files it
consulted. When printing manually, add **at most 5 terse bullets** for in-memory decisions, verified
root causes, or the exact next step that the packet does not show.

Best moment: after an agent run has finished (not mid-run), especially when you return after the
prompt cache expired - resuming a 150K chat then re-reads all 150K uncached; the packet costs ~16K.
