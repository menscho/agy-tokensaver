#!/usr/bin/env python3
"""Comprehensive Antigravity context, rule-budget, MCP, hook, skill, and workspace leak auditor."""

import json
import os
import re
import sys
from pathlib import Path

NATIVE_DUP_MCPS = {"filesystem", "fs", "shell", "bash", "terminal", "git", "fetch", "puppeteer", "playwright"}
BLOAT_DIRS = {"node_modules", "dist", "build", ".next", ".nuxt", "target", ".venv", "venv", "__pycache__", "coverage", ".cache", "vendor", ".terraform"}
LOCK_NAMES = {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock",
    "poetry.lock", "composer.lock", "Gemfile.lock", "go.sum", "uv.lock", "bun.lockb", "bun.lock",
    "Pipfile.lock", "flake.lock", "Podfile.lock", "packages.lock.json", "mix.lock", "pubspec.lock",
    "Package.resolved", "gradle.lockfile", "conda-lock.yml", "pdm.lock", "deno.lock",
}


def est_tokens(n_bytes: int) -> int:
    return n_bytes // 4


def parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    fm = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line.strip())
        if m:
            fm[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return fm


def expanded_rule_bytes(p: Path) -> int:
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    total = len(txt.encode("utf-8", errors="replace"))
    for m in re.finditer(r"@\[[^\]]*\]\(([^)]+)\)", txt):
        inc = (p.parent / m.group(1)).resolve()
        try:
            if inc.is_file():
                total += inc.stat().st_size
        except OSError:
            pass
    return total


def resolve_plugins(cfg_dir: Path, ws: Path) -> list[dict]:
    overrides: dict[str, object] = {}
    cfg_json = cfg_dir / "config.json"
    if cfg_json.is_file():
        try:
            data = json.loads(cfg_json.read_text(encoding="utf-8"))
            overrides = data.get("plugins", {}) if isinstance(data, dict) else {}
        except Exception:
            pass

    roots = [
        ("builtin", Path.home() / ".gemini" / "antigravity" / "builtin" / "plugins"),
        ("global", cfg_dir / "plugins"),
    ]
    for dot in (".agents", ".agent", "_agents", "_agent"):
        roots.append(("workspace", ws / dot / "plugins"))

    found = []
    seen_dirs = set()
    for scope, rdir in roots:
        if not rdir.is_dir():
            continue
        for pdir in sorted(rdir.iterdir()):
            if not pdir.is_dir() or not (pdir / "plugin.json").is_file():
                continue
            rp = str(pdir.resolve())
            if rp in seen_dirs:
                continue
            seen_dirs.add(rp)
            manifest = {}
            try:
                manifest = json.loads((pdir / "plugin.json").read_text(encoding="utf-8"))
            except Exception:
                pass
            dname = pdir.name
            default_enabled = not bool(manifest.get("disabled", False))
            ov = overrides.get(dname)
            enabled = bool(ov["enabled"]) if isinstance(ov, dict) and "enabled" in ov else default_enabled
            found.append({"name": manifest.get("name", dname), "dir": dname, "scope": scope, "enabled": enabled, "path": pdir})
    return found


def inspect_rules(roots: list[tuple[str, Path]]) -> tuple[list[dict], list[str]]:
    found, warnings, seen = [], [], set()
    for scope, root in roots:
        if not root.exists():
            continue
        # Check same-dir AGENTS.md + GEMINI.md duplication
        if (root / "AGENTS.md").is_file() and (root / "GEMINI.md").is_file():
            warnings.append(f"Both AGENTS.md and GEMINI.md exist in {root} — consolidate to avoid duplicate rule tokens.")
        candidates = [root / "AGENTS.md", root / "GEMINI.md"]
        for dot in (".agents", ".agent", "_agents", "_agent"):
            candidates.extend([root / dot / "AGENTS.md", root / dot / "GEMINI.md"])
            rsub = root / dot / "rules"
            if rsub.is_dir():
                candidates.extend(sorted(rsub.glob("*.md")))
        if (root / "rules").is_dir():
            candidates.extend(sorted((root / "rules").glob("*.md")))
        for p in candidates:
            if not p.is_file():
                continue
            rp = str(p.resolve())
            if rp in seen:
                continue
            seen.add(rp)
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm = parse_frontmatter(txt)
            trigger = fm.get("trigger", "always_on")
            sz = expanded_rule_bytes(p)
            tok = est_tokens(sz)
            found.append({"path": rp, "scope": scope, "trigger": trigger, "bytes": sz, "est_tokens": tok})
            if sz > 24000:
                warnings.append(f"Rule exceeds 24KB cap and will be truncated: {rp} ({sz:,}B)")
            if p.parent.name == "rules" and p.name not in ("AGENTS.md", "GEMINI.md") and trigger == "always_on" and tok > 300:
                warnings.append(f"Rule {p.name} ({tok} tok) is always_on; consider trigger: glob or model_decision.")
    return found, warnings


def inspect_hooks(hook_paths: list[Path]) -> list[str]:
    warnings = []
    for p in hook_paths:
        if not p.is_file():
            continue
        # Check misplaced hooks/hooks.json in native plugins
        if p.parent.name == "hooks" and (p.parent.parent / "plugin.json").is_file():
            warnings.append(f"Misplaced {p}: native plugins only load <plugin_root>/hooks.json.")
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for name, spec in data.items():
            if not isinstance(spec, dict) or spec.get("enabled") is False:
                continue
            if spec.get("PreInvocation") and not ("token-saver" in str(p) and name == "token-saver-handoff"):
                warnings.append(f"PreInvocation hook '{name}' in {p}: runs synchronously before EVERY turn; avoid dynamic text injection.")
            if spec.get("PostInvocation") and "token-saver" not in str(p):
                warnings.append(f"PostInvocation hook '{name}' in {p}: ensure it emits empty systemMessage (metadata.user_markdown only) to avoid per-turn bloat.")
            for ev in ("PreToolUse", "PostToolUse"):
                for grp in spec.get(ev, []) or []:
                    m = grp.get("matcher", "")
                    if m in ("", "*"):
                        warnings.append(f"Wildcard {ev} hook '{name}' in {p} matches EVERY tool call (adds latency to all tools).")
    return warnings


def inspect_mcps(mcp_paths: list[Path]) -> tuple[list[dict], list[str]]:
    servers, warnings = [], []
    for p in mcp_paths:
        if not p.is_file() or p.stat().st_size == 0:
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        mmap = data.get("mcpServers", data) if isinstance(data, dict) else {}
        if not isinstance(mmap, dict):
            continue
        for name, cfg in mmap.items():
            if not isinstance(cfg, dict) or cfg.get("disabled"):
                continue
            eager_tools = [t for t, tc in (cfg.get("tools") or {}).items() if isinstance(tc, dict) and tc.get("eager")]
            servers.append({"name": name, "source": str(p), "eager_count": len(eager_tools)})
            if eager_tools:
                warnings.append(f"MCP '{name}' has {len(eager_tools)} eager tool(s) injecting schemas on EVERY turn.")
            if name.lower() in NATIVE_DUP_MCPS:
                warnings.append(f"MCP '{name}' likely duplicates native Antigravity tools (pure schema tax).")
    return servers, warnings


def inspect_skills_and_agents(roots: list[Path]) -> tuple[int, list[str]]:
    catalog_tok = 0
    warnings = []
    for root in roots:
        sdir = root / "skills"
        if sdir.is_dir():
            for sk in sorted(sdir.glob("*/SKILL.md")):
                try:
                    fm = parse_frontmatter(sk.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                if fm.get("disable-model-invocation", "").lower() == "true":
                    continue
                desc = fm.get("description", "")
                catalog_tok += est_tokens(len(fm.get("name", "")) + len(desc) + 20)
                if len(desc) > 280:
                    warnings.append(f"Skill '{sk.parent.name}' has a verbose description ({len(desc)} chars) loaded on every turn.")
        adir = root / "agents"
        if adir.is_dir():
            for ag in sorted(adir.glob("*.md")):
                try:
                    fm = parse_frontmatter(ag.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                if fm.get("inheritMcp", "").lower() != "false" or fm.get("inheritCustomizations", "").lower() != "false":
                    warnings.append(f"Subagent '{ag.stem}' doesn't set inheritMcp: false & inheritCustomizations: false.")
    return catalog_tok, warnings


def inspect_workspace(ws: Path) -> tuple[list[dict], list[str]]:
    traps: list[dict[str, object]] = []
    ig_warns: list[str] = []
    if not ws.is_dir():
        return traps, ig_warns
    has_git = (ws / ".git").exists()
    gi_txt = (ws / ".gitignore").read_text(encoding="utf-8", errors="replace") if (ws / ".gitignore").is_file() else ""
    if has_git and not gi_txt:
        ig_warns.append("Git repo has no .gitignore — build/vendor dirs may be indexed.")
    for bd in sorted(BLOAT_DIRS):
        if (ws / bd).is_dir() and bd not in gi_txt:
            ig_warns.append(f"Bloat directory '{bd}/' exists on disk and is not listed in .gitignore.")

    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in BLOAT_DIRS and d != ".git"]
        rel_depth = len(Path(root).relative_to(ws).parts)
        if rel_depth >= 3:
            dirs[:] = []
            continue
        for fn in files:
            p = Path(root) / fn
            try:
                sz = p.stat().st_size
            except OSError:
                continue
            suf = p.suffix.lower()
            if fn in LOCK_NAMES and sz > 10_000:
                traps.append({"path": str(p), "type": "lockfile", "bytes": sz})
            elif suf in {".ipynb", ".har", ".heapsnapshot", ".cpuprofile", ".drawio", ".excalidraw", ".sqlite", ".sqlite3", ".db", ".tfstate"} and sz > 12_000:
                traps.append({"path": str(p), "type": suf.lstrip("."), "bytes": sz})
            elif suf == ".svg" and sz > 20_000:
                traps.append({"path": str(p), "type": "large_svg", "bytes": sz})
            elif sz > 150_000 and suf in {".json", ".sql", ".csv", ".log", ".xml", ".js", ".map", ".snap"}:
                traps.append({"path": str(p), "type": "large_dump", "bytes": sz})
    return sorted(traps, key=lambda x: int(x["bytes"]) if isinstance(x["bytes"], int) else 0, reverse=True)[:15], ig_warns


def discover_transcript_leaks(max_sessions: int = 30) -> tuple[dict[str, int], list[tuple[str, int, int]], list[tuple[str, int]]]:
    """Mine recent transcripts for 0-call MCP servers and top unwrapped/large CLI commands (rtk discover style)."""
    gemini = Path.home() / ".gemini"
    txs: list[Path] = []
    for sub in ("antigravity", "antigravity-cli"):
        txs.extend((gemini / sub / "brain").glob("*/.system_generated/logs/transcript.jsonl"))
    txs.sort(key=lambda p: p.stat().st_mtime if p.is_file() else 0, reverse=True)
    mcp_calls: dict[str, int] = {}
    cmd_counts: dict[str, int] = {}
    cmd_bytes: dict[str, int] = {}
    big_reads: dict[str, int] = {}
    for tx in txs[:max_sessions]:
        try:
            with tx.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"tool_calls"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    for tc in obj.get("tool_calls") or []:
                        s = str(tc)
                        for m in re.finditer(r"mcp_([a-zA-Z0-9_-]+)_", s):
                            mcp_calls[m.group(1)] = mcp_calls.get(m.group(1), 0) + 1
                        if "CommandLine" in s:
                            mc = re.search(r'"CommandLine"\s*:\s*"([^"]{2,120})"', s)
                            if mc:
                                head = re.sub(r"^\s*(?:cd\s+\S+\s*&&\s*)?", "", mc.group(1)).split()[0]
                                head = Path(head.replace("\\", "/")).name.lower()
                                cmd_counts[head] = cmd_counts.get(head, 0) + 1
                                cmd_bytes[head] = cmd_bytes.get(head, 0) + len(s)
                        if "read_file" in s and "EndLine" not in s:
                            mp = re.search(r'"Path"\s*:\s*"([^"]+)"', s)
                            if mp:
                                ext = Path(mp.group(1)).suffix.lower() or "(noext)"
                                big_reads[ext] = big_reads.get(ext, 0) + 1
        except OSError:
            continue
    top_cmds = sorted(((k, cmd_counts[k], cmd_bytes[k]) for k in cmd_counts), key=lambda x: -x[1])[:12]
    top_unb = sorted(big_reads.items(), key=lambda x: -x[1])[:8]
    return mcp_calls, top_cmds, top_unb


def main() -> int:
    discover_mode = "--discover" in sys.argv
    pos_args = [a for a in sys.argv[1:] if not a.startswith("-")]
    ws = Path(pos_args[0]).resolve() if pos_args else Path.cwd()
    cfg = Path.home() / ".gemini" / "config"

    plugins = resolve_plugins(cfg, ws)
    enabled_plugins = [p for p in plugins if p["enabled"]]

    rule_roots = [("global", cfg), ("workspace", ws)]
    hook_paths = [cfg / "hooks.json", ws / ".agents" / "hooks.json"]
    mcp_paths = [cfg / "mcp_config.json", ws / ".agents" / "mcp_config.json"]
    comp_roots = [cfg, ws / ".agents"]

    for p in enabled_plugins:
        rule_roots.append((p["scope"], p["path"]))
        hook_paths.extend([p["path"] / "hooks.json", p["path"] / "hooks" / "hooks.json"])
        mcp_paths.append(p["path"] / "mcp_config.json")
        comp_roots.append(p["path"])

    rules, rule_warns = inspect_rules(rule_roots)
    hook_warns = inspect_hooks(hook_paths)
    mcps, mcp_warns = inspect_mcps(mcp_paths)
    skill_tok, comp_warns = inspect_skills_and_agents(comp_roots)
    traps, ig_warns = inspect_workspace(ws)
    mcp_calls, top_cmds, top_unb = discover_transcript_leaks()
    for m in mcps:
        nm = m["name"]
        if nm not in ("token_saver_ui", "usage_counter_ui") and not any(nm.replace("-", "_") in k.replace("-", "_") for k in mcp_calls):
            mcp_warns.append(f"MCP '{nm}' had 0 calls in the last 30 sessions — disabling it removes its schema tax on every turn.")
    for r in rules:
        if r["trigger"] == "always_on":
            try:
                txt = Path(r["path"]).read_text(encoding="utf-8", errors="replace")
                if re.search(r"\b(?:current[_\s]?date|timestamp|\$\(date\)|today'?s date)\b", txt, re.I):
                    rule_warns.append(f"Cache-buster warning: always-on rule '{r['path']}' references dynamic date/timestamp (arXiv:2601.06007).")
            except OSError:
                pass

    print("=== Antigravity Context & Cache Audit (v2) ===")
    print(f"Workspace : {ws}")
    print("Plugins   :", ", ".join(f"{p['dir']}({'on' if p['enabled'] else 'off'}/{p['scope']})" for p in plugins) or "(none)")
    print()

    ao_rules = [r for r in rules if r["trigger"] == "always_on"]
    ao_non_builtin = sum(r["est_tokens"] for r in ao_rules if r["scope"] != "builtin")
    print(f"1. Rules ({len(ao_rules)} always-on, ~{ao_non_builtin:,} / 20,000 budget; {len(rules)-len(ao_rules)} scoped)")
    for r in rules:
        print(f"   - [{r['trigger']:14s}] {r['path']} ({r['bytes']:,}B, ~{r['est_tokens']:,} tok)")
    for w in rule_warns:
        print(f"   ! {w}")
    print()

    print(f"2. MCP Servers ({len(mcps)} active) & Skill/Agent Catalog (~{skill_tok:,} startup tok)")
    for m in mcps:
        print(f"   - MCP: {m['name']} (eager tools: {m['eager_count']}) [{m['source']}]")
    if not mcps:
        print("   - MCP: (none active — 0 MCP schema overhead)")
    for w in mcp_warns + comp_warns:
        print(f"   ! {w}")
    print()

    print("3. Cache-Stability & Hook Check")
    if hook_warns:
        for w in hook_warns:
            print(f"   ! {w}")
    else:
        print("   OK — no cache-busting PreInvocation or wildcard hooks detected.")
    print()

    print(f"4. Workspace Hygiene & Bloat Traps ({len(traps)} traps)")
    for w in ig_warns:
        print(f"   ! {w}")
    for t in traps:
        print(f"   - [{t['type']}] {t['path']} ({t['bytes'] // 1024:,}KB, ~{est_tokens(min(t['bytes'], 46080)):,} tok/read)")
    if not traps and not ig_warns:
        print("   OK — no obvious workspace bloat traps in top 3 directory levels.")

    if discover_mode:
        print("\n5. Transcript Discovery (last 30 sessions — rtk discover style)")
        print("   Top run_command prefixes:")
        for bn, cnt, _b in top_cmds:
            print(f"     - {bn:20s}: {cnt:4d} calls")
        print("   Top unbounded read_file extensions:")
        for ext, cnt in top_unb:
            print(f"     - {ext:16s}: {cnt:4d} unbounded reads")
        if mcp_calls:
            print("   Active MCP tool calls:")
            for k, v in sorted(mcp_calls.items(), key=lambda kv: -kv[1]):
                print(f"     - {k:24s}: {v:4d} calls")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
