#!/usr/bin/env python3
"""
Fast (~50ms) stdlib structural repository digest generator.
Produces a compact ~1.5K-2.5K token map of files + key symbols with exact
line ranges so swarm workers and fresh sessions never cold-read 50K+ tokens
of identical repo files.
"""

import os
import re
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", ".next", ".nuxt", "out", "target",
    ".venv", "venv", "__pycache__", "coverage", ".cache", "vendor", ".terraform",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".gradle", ".idea", ".vscode",
}

SYM_PATTERNS = {
    ".py": re.compile(r"^(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)"),
    ".ts": re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|interface|type|const\s+\w+\s*=\s*(?:async\s*)?\()(?:\s+([A-Za-z_]\w*))?"),
    ".tsx": re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class|interface|type)\s+([A-Za-z_]\w*)"),
    ".js": re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_]\w*)"),
    ".mjs": re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_]\w*)"),
    ".jsx": re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function|class)\s+([A-Za-z_]\w*)"),
    ".rs": re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn|struct|enum|trait|impl)\s+([A-Za-z_]\w*)"),
    ".go": re.compile(r"^(?:func\s+(?:\([^)]*\)\s*)?|type\s+)([A-Za-z_]\w*)"),
    ".pl": re.compile(r"^sub\s+([A-Za-z_]\w*)"),
    ".sh": re.compile(r"^(?:function\s+)?([A-Za-z_]\w*)\s*\(\s*\)"),
}


def extract_symbols(path: Path, pat: re.Pattern[str], max_syms: int = 14) -> tuple[int, list[str]]:
    syms: list[str] = []
    n_lines = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, 1):
                n_lines = i
                if len(syms) >= max_syms:
                    continue
                m = pat.match(line)
                if m:
                    name = m.group(1) if m.lastindex and m.group(1) else line.strip()[:36]
                    syms.append(f"{name}:L{i}")
    except OSError:
        return 0, []
    return n_lines, syms


def main() -> int:
    ws = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
    max_files = int(sys.argv[2]) if len(sys.argv) > 2 else 90

    entries: list[tuple[str, int, list[str]]] = []
    manifests: list[str] = []
    manifest_names = {"package.json", "Cargo.toml", "pyproject.toml", "go.mod", "Makefile", "docker-compose.yml", "plugin.json", "hooks.json"}

    for root, dirs, files in os.walk(ws):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".git"))
        rel_root = Path(root).relative_to(ws)
        if len(rel_root.parts) > 5:
            dirs[:] = []
            continue
        for fn in sorted(files):
            p = Path(root) / fn
            rel = str(p.relative_to(ws))
            if fn in manifest_names:
                manifests.append(rel)
            pat = SYM_PATTERNS.get(p.suffix.lower())
            if pat:
                n_lines, syms = extract_symbols(p, pat)
                if n_lines > 0:
                    entries.append((rel, n_lines, syms))

    out = [f"# Repo Structural Digest: `{ws.name}` ({len(entries)} code files)"]
    if manifests:
        out.append("Manifests: " + ", ".join(f"`{m}`" for m in manifests[:15]))
    out.append("")
    for rel, n_lines, syms in entries[:max_files]:
        sym_str = (" -> " + ", ".join(syms)) if syms else ""
        out.append(f"- `{rel}` ({n_lines}L){sym_str}")
    if len(entries) > max_files:
        out.append(f"... (+{len(entries) - max_files} more code files)")

    text = "\n".join(out) + "\n"
    print(text)
    print(f"[repo_digest: ~{len(text)//4} tokens]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
