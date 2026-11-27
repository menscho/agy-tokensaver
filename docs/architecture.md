# Architecture

`agy-tokensaver` is a dependency-free Python 3.9+ stdlib plugin for Google Antigravity combining Auto-Handoff for long chats, 5 eager-loaded batched MCP tools, a `SessionStart` workspace snapshot (under 1.5 KB, 250 ms scan limit), PreToolUse/PostInvocation guards, and Max-mode rules. Fresh installs start in Max (`FRESH_INSTALL_CONFIG` in `install.py`); the runtime fallback when no settings file exists stays the lossless Auto-Handoff + Read Guards profile.

> The batched MCP accelerator tools, `SessionStart` snapshot, water-filling/head-tail output compaction, and `--cli` Swarm runner are a Python port of designs from [INK-Q/agy-fast](https://github.com/INK-Q/agy-fast) (MIT License; notice in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)).

## Lifecycle

```mermaid
flowchart LR
    S["SessionStart Hook"] --> SS["session_start_hook.py (<=1.5KB snapshot, 250ms scan limit)"]
    A["Antigravity Tool Call"] --> B["PreToolUse Hook (python3 -S + hook_common)"]
    B -->|off / small / safe| C["{\"decision\":\"allow\"} (fail-open)"]
    B -->|read_file / write / find / grep| D["Block lockfile/binary/dup, trim overlap, or cap range"]
    D --> E["PostToolUse / PostInvocation / Stop Hooks"]
    E --> F["Local plugin_data/token-saver/ (events, reads, alerts, sessions)"]
    M["Eager MCP Accelerator Calls"] --> G["mcp_app/server.py (HUD + 5 Batched Tools <=8.8KB)"]
    F --> G
    G -->|Settled large or cold chat| H["handoff_pack.py -> Archive / New Chat"]
```

Notice that native `run_command` and shell execution are never wrapped or modified.

## Modules

- **Auto-Handoff & MCP App Server (`mcp_app/` + `scripts/handoff_pack.py`):**
  - `mcp_app/server.py`: unified pure-stdlib JSON-RPC stdio server exposing the `dashboard.html` MCP App (`open_token_saver` + app-only `ts_*` tools), the 5 eager batched accelerator tools (`batch_search`, `apply_edits`, `quick_outline`, `read_slices`, `run_checks`), MCP `roots/list` workspace resolution, `--tools`/`--schema-compat` host compatibility, and `--cli` direct Swarm runner mode.
  - `mcp_app/fast_utils.py`: UTF-8 byte-safe truncation (`≤ 8,800` bytes), water-filling budget allocator, ANSI/spinner/noise stripping, green build/pass phase folding, Head-Tail failure compaction, `call_mcp_tool` corruption detection, glob/walk helpers, fuzzy path scorer, hardened subprocess execution (with Windows PowerShell unwrapping), and host compatibility.
  - `mcp_app/fast_search.py`, `fast_outline.py`, `fast_edits.py`, `fast_checks.py`, `fast_snapshot.py`, `fast_cli.py`: modular implementations of all 5 accelerator tools, startup snapshot builder, and direct CLI runner.
  - `mcp_app/`: `config.py`, `rules_sync.py`, `rollover.py`, `transcript_io.py`, `telemetry.py`, `snapshots.py`, `updater.py`, and facade `core.py`.
  - `scripts/handoff_pack.py`: extracts user/assistant state, git changes, recent errors, verified test commands, and edited symbols (from both built-in edit tools and `apply_edits`) into a compact (typically 300–550 token) handoff packet.
- **Python 3.9+ stdlib hooks (`scripts/`, invoked with `python3 -S`):**
  - `scripts/session_start_hook.py`: fail-open `SessionStart` hook injecting a `≤ 1.5 KB` workspace snapshot (250 ms circuit-breaker, nested repo detection, OS/`$HOME` skip list).
  - `scripts/hook_common.py`: shared `json.loads` payload decoder with strict type checks, `hooks_disabled()` kill-switch check, loop-guard state, and a 2 MB fail-open payload cap.
  - `scripts/read_hook.py`: binary/lockfile/generated-code gate, compaction-safe duplicate & overlap guard, dirty-hunk narrowing, duplicate image guard, and optional Compact Reads.
  - `scripts/{write_hook,find_hook,grep_hook,post_edit_hook,pre_invocation_handoff,post_invocation_alerts,stop_telemetry}.py`.
- **Utilities (`scripts/`):**
  - `ts_inspect.py`, `audit.py`, `benchmark.py`, `transcript_replay.py` + `replay_io.py` + `replay_modes.py`, `make_benchmark_chart.py`, `stats.py`.

## On-Disk State

All hook and accelerator state stays local. The only HTTP calls are user-triggered updates and localhost calls to the Antigravity language server (`RefreshMcpServers`, and `ConversationService` when a handoff opens a new chat); see [SECURITY.md](../SECURITY.md):

| Path | Contents |
| :--- | :--- |
| `~/.gemini/antigravity/plugin_data/token-saver/hud_config.json` | Active `profile`, `mode` (`smart_auto`, `prepare_only`, `off`), `startup_snapshot`, and threshold. |
| `.../plugin_data/token-saver/events.jsonl` | Hook and batched accelerator event log (`ts`, `source`, `category`, `action`, `raw_bytes`, `kept_bytes`, `est_tokens_saved`). |
| `.../plugin_data/token-saver/reads/<cid>.tsv` | Compaction-verified seen line ranges (`path`, `mtime`, `size`, `md5`, `start`, `end`, `step`, `epoch`). |
| `.../plugin_data/token-saver/rollover_state.json` & `rollover_history.json` | Tracked auto-handoffs, archive snapshots, and compact packets. |
