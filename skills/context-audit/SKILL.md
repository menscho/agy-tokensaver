---
name: context-audit
description: User-only. Audit config/workspace for token leaks.
disable-model-invocation: true
---

# Context & Cache Leak Audit

Audits oversized always-on rules, unused MCP servers/plugins, cache-busting hooks and workspace bloat.

Run the local audit script against the current working directory (and global Antigravity config):

```bash
python3 "$HOME/.gemini/config/plugins/token-saver/scripts/audit.py" "$(pwd)"
```

Review the structured findings and present an actionable checklist to the user:
1. **Always-On Rule Budget:** Flag `AGENTS.md` / `GEMINI.md` files > 2,000 tokens or approaching the 24 KB / 20,000-token cap; suggest moving procedural workflows to on-demand `SKILL.md` files.
2. **Plugin & MCP Schema Overhead:** List active plugins and MCP servers; flag unused servers whose tool schemas inflate every turn.
3. **Cache-Busting Hooks:** Flag any `PreInvocation` hooks that inject dynamic output on every turn.
4. **Workspace Bloat Traps:** Highlight huge lockfiles, bundles, logs, or data dumps present in the workspace.
