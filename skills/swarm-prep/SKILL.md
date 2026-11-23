---
name: swarm-prep
description: >-
  Generate a compact (~1.5K-token) structural repo digest before launching a
  swarm so workers read exact file:line slices instead of cold-reading 50K+
  tokens of duplicate files each.
disable-model-invocation: true
---

# Swarm Cold-Start Optimizer (`/swarm-prep`)

Run the structural digest script once for the workspace:

```bash
python3 "$HOME/.gemini/config/plugins/token-saver/scripts/repo_digest.py" "$(pwd)" > /tmp/agy-repo-digest.md && head -n 120 /tmp/agy-repo-digest.md
```

When briefing swarm workers (`swarm add`):
1. Paste ONLY the relevant slice of `/tmp/agy-repo-digest.md` (target files + exact `Lstart-Lend` symbols) into each worker's brief so workers skip repo-wide cold-reads.
2. Assign read-only scouts/verifiers to `flash` (or `flash_lite` for pure mechanical sifting) when user didn't request a specific model.
3. Keep orchestrator context lean (<90K) before long `swarm_wait` sleeps to avoid 90s idle TTL cache wipes.
