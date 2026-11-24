# Architecture

`agy-tokensaver` is a zero-dependency Perl 5 + Python 3 stdlib plugin for Google Antigravity.

## Hook Lifecycle

```mermaid
flowchart LR
    A["Antigravity Tool Call"] --> B["PreToolUse Hook (Perl + TsJson / JSON::PP)"]
    B -->|Fast-path miss or error| C["{\"decision\":\"allow\"} (fail-open)"]
    B -->|read_file / grep / find / board / write| D["Block trap / trim range / inject excludes"]
    B -->|run_command hit| E["cmd_hook_core.pl (quote-aware classifier)"]
    E -->|Unsafe redirect / daemon / kill-switch| C
    E -->|Safe compressible class| F["cmd_wrap.pl (on-demand scripts/compressors/*.pl)"]
    F --> G["Byte-exact raw spill (0600, per-user tmp)"]
    F --> H["Compressed output + [full: path] pointer"]
    D & H --> I["PostToolUse / PostInvocation / Stop Hooks"]
    I --> J["Local plugin_data/token-saver/ (events, reads, alerts)"]
    J --> K["MCP App HUD (mcp_app/server.py + dashboard.html)"]
```

## Module Layout

- **Perl Pre/Post/Stop Hooks (`scripts/*.pl`):**
  - `scripts/lib/TsJson.pm`: shared `JSON::PP` decoder with strict type checks and a 2 MB fail-open payload cap.
  - `scripts/cmd_hook.pl` → `scripts/cmd_hook_core.pl` + on-demand `scripts/lib/{cmd_pwsh,cmd_classify}.pl`.
  - `scripts/cmd_wrap.pl` + `scripts/lib/TsSpill.pl` + on-demand `scripts/compressors/{common,json,test,build,git,log,ssh,pwsh}.pl`.
  - `scripts/read_hook.pl` + on-demand `scripts/lib/{read_media,read_dedupe}.pl`.
  - `scripts/{write_hook,find_hook,grep_hook,board_hook,swarm_hook,post_edit_hook,post_invocation_alerts,stop_telemetry}.pl`.
- **MCP App HUD (`mcp_app/`):**
  - `server.py`: JSON-RPC stdio MCP server serving `dashboard.html` with vendored `@modelcontextprotocol/ext-apps@2.0.4`.
  - `config.py`, `rules_sync.py`, `rollover.py`, `transcript_io.py`, `telemetry.py`, `snapshots.py`, `updater.py`, and facade `core.py`.
- **CLI Utilities (`scripts/`):**
  - `ts_inspect.py` (`outline`, `query`, `purge`, `json`, `csv`, `ipynb`, `sqlite`, `svg`, `har`, `html`, `sql`, `schema`).
  - `handoff_pack.py`, `audit.py`, `benchmark.py`, `transcript_replay.py` + `replay_io.py` + `replay_modes.py`, `make_benchmark_chart.py`, `stats.py`.

## On-Disk State & Data Formats

All state stays strictly local (zero network telemetry):

| Path | Format | Purpose |
| :--- | :--- | :--- |
| `$TMPDIR/agy-tokensaver-<uid>/<12hex>.log` | Raw bytes (`0600` in `0700` dir) | Byte-exact command output spills (auto-pruned after 7 days / 200 files, or `ts_inspect.py purge`). |
| `~/.gemini/antigravity/plugin_data/token-saver/hud_config.json` | JSON object | Active HUD mode (`zero_loss`, `compact_reads`, `concise_no_handoff`, `handoff_only`, `small_loss`), threshold, and deferred rule-sync flag. |
| `.../plugin_data/token-saver/events.jsonl` | NDJSON | Per-hook telemetry records (`ts`, `source`, `category`, `action`, `raw_bytes`, `kept_bytes`, `est_tokens_saved`). |
| `.../plugin_data/token-saver/reads/<cid>.tsv` | TSV | Compaction-verified seen line ranges (`path`, `mtime`, `size`, `md5`, `start`, `end`, `step`, `epoch`). |
| `.../plugin_data/token-saver/rollover_state.json` & `rollover_history.json` | JSON | Tracks `snapshotted_convs` separately from `rolled_convs` plus archived handoff packets. |
