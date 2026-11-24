---
name: swarm-prep
description: User-only. Repo digest for swarm worker briefs.
disable-model-invocation: true
---

# Swarm Cold-Start Optimizer (`/swarm-prep`)

The digest is ~1.5-2.5K tokens and replaces 50K+ tokens of duplicate cold reads per worker.

Run the structural digest script once from the workspace root (same command in bash and PowerShell; optional 2nd arg = max files, default 90):

```bash
python3 "$HOME/.gemini/config/plugins/token-saver/scripts/repo_digest.py" .
```

When briefing swarm workers (`swarm add`):
1. Paste ONLY the relevant slice of the digest (target files + exact `Lstart-Lend` symbols) into each worker's brief so workers skip repo-wide cold-reads.
2. Assign read-only scouts/verifiers to `flash` (or `flash_lite` for pure mechanical sifting) when user didn't request a specific model.
3. Keep orchestrator context lean (<90K) before long `swarm_wait` sleeps to avoid 90s idle TTL cache wipes.
