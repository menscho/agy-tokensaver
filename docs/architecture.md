# Architecture

`agy-tokensaver` is a dependency-free Perl 5 + Python 3 stdlib plugin for Google Antigravity.

## Lifecycle

```mermaid
flowchart LR
    A["Antigravity Tool Call"] --> B["PreToolUse Hook (Perl + TsJson / JSON::PP)"]
    B -->|off / small / safe| C["{\"decision\":\"allow\"} (fail-open)"]
    B -->|read_file / write / find / grep| D["Block lockfile/binary/dup, trim overlap, or cap range"]
    D --> E["PostToolUse / PostInvocation / Stop Hooks"]
    E --> F["Local plugin_data/token-saver/ (events, reads, alerts, sessions)"]
    F --> G["MCP App HUD (mcp_app/server.py + dashboard.html)"]
    G -->|Settled large or cold chat| H["handoff_pack.py -> Archive / New Chat"]
```

Notice that `run_command` and shell execution are not wrapped or modified.

## Modules

- **Perl hooks (`scripts/`):**
  - `scripts/lib/TsJson.pm`: shared `JSON::PP` decoder with type checks, `hooks_disabled()` kill-switch check, and a 2 MB fail-open payload cap.
  - `scripts/read_hook.pl` + `scripts/lib/{read_media,read_dedupe}.pl`: binary/lockfile/generated-code gate, compaction-safe duplicate & overlap guard, dirty-hunk narrowing, and optional Compact Reads.
  - `scripts/{write_hook,find_hook,grep_hook,post_edit_hook,post_invocation_alerts,stop_telemetry}.pl` + `scripts/lib/TsLoopGuard.pl`.
- **MCP App dashboard (`mcp_app/`):**
  - `server.py`: JSON-RPC stdio server rendering `dashboard.html` with vendored `@modelcontextprotocol/ext-apps`.
  - `config.py`, `rules_sync.py`, `rollover.py`, `transcript_io.py`, `telemetry.py`, `snapshots.py`, `updater.py`, and facade `core.py`.
- **Utilities (`scripts/`):**
  - `handoff_pack.py`, `ts_inspect.py`, `audit.py`, `benchmark.py`, `transcript_replay.py` + `replay_io.py` + `replay_modes.py`, `make_benchmark_chart.py`, `stats.py`.

## On-Disk State

All hook state stays local (only user-triggered updates and localhost `RefreshMcpServers` use HTTP; see [SECURITY.md](../SECURITY.md)):

| Path | Contents |
| :--- | :--- |
| `~/.gemini/antigravity/plugin_data/token-saver/hud_config.json` | Active `profile` (`auto_handoff` default, `handoff_only`, `small_loss`, `zero_loss`, `compact_reads`), `mode` (`smart_auto`, `prepare_only`, `off`), and threshold. |
| `.../plugin_data/token-saver/events.jsonl` | Hook event log (`ts`, `source`, `category`, `action`, `raw_bytes`, `kept_bytes`, `est_tokens_saved`). |
| `.../plugin_data/token-saver/reads/<cid>.tsv` | Compaction-verified seen line ranges (`path`, `mtime`, `size`, `md5`, `start`, `end`, `step`, `epoch`). |
| `.../plugin_data/token-saver/rollover_state.json` & `rollover_history.json` | Tracked auto-handoffs, archive snapshots, and compact packets. |
