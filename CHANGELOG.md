# Changelog

All notable changes to **agy-tokensaver** (`menscho/agy-tokensaver`) are documented here.

## [1.0.0] — Initial Open-Source Release

- **Fail-Open Perl Hooks (`PreToolUse` / `PostInvocation` / `Stop`):**
  - `cmd_hook.pl`, `cmd_hook_core.pl`, `cmd_wrap.pl`: Quote-aware shell classifier supporting `&&`/`;`/newline chains of known segments, `|| true`, safe read-only stdout filter pipelines (`| grep/rg/sort/uniq/awk/sed/jq`), multi-directory `ls -la`, and test/build/audit Python scripts. Skips redirects, heredocs, `tee`, `-f`/`--follow`, PowerShell syntax, and native `MSWin32` PowerShell shells. Preserves exit/signal codes and TTY prompts, pins error/warning/traceback lines, omits lockfile/vendor-bundle diffs, and saves raw unmodified output to the OS temp directory (`agy-tokensaver-<uid>`).
  - `read_hook.pl`: Recognizes Unix `/`, Windows `C:\`, and UNC paths; blocks binaries, SQLite DBs, 25+ lockfiles, sourcemaps, minified bundles, and unchanged exact/superset-covered re-reads (`>4 KB`, `≤16` steps, same compaction epoch). Only records line ranges guaranteed below `read_file`'s 46,080 B / 800-line truncation ceiling and logs signed slice-sized savings minus denial/overwrite overhead. Auto-caps unbounded data files (`>8 KB`) and large source files (`>16 KB`).
  - `write_hook.pl`, `find_hook.pl`, `swarm_hook.pl`, `post_invocation_alerts.pl`, `stop_telemetry.pl`.
- **Native Full-Page HUD (`mcp_app/`):** Global Antigravity MCP App (`agy-tokensaver`) with `Overview`, `Archive`, and `Updates` tabs; internal RPC tools use `visibility: ["app"]`.
- **Archive & Optional Rollover (`scripts/handoff_pack.py`, `mcp_app/core.py`):** Defaults to `Archive Only` (no automatic new chats; `Smart Auto` and `Off` selectable in the HUD). Targets the exact conversation ID being packed, extracts workspace git state + recent commits + touched files + last goal/assistant summary into a compact continuation prompt, and works cross-platform on Unix and Windows.
- **Tagged, SHA-256 Verified Installer & Updater (`install.py`, `Updates` tab):** Installs and updates only from tagged GitHub Releases verified against `SHA256SUMS.txt`, pins the active Python path in `mcp_config.json` on native Windows, and preserves `~/.gemini/antigravity/plugin_data/token-saver/`.
- **Reproducible Benchmarks & 3-OS CI (`scripts/benchmark.py`, `.github/workflows/test.yml`):** Deterministic 6-step Before/After hook benchmark (`--demo`, routed through `cmd_hook.pl`'s actual decisions), local transcript counterfactual replay (`--transcripts`, signed per-session median/mean/regression count + total quota-weighted savings with `+8k` re-read tax per rollover), and 168 regression assertions running in GitHub Actions on Ubuntu, macOS, and Windows.
- **5 Flash/Flash-Lite Scouts & 3 Isolated Workflows:** `lite-sifter`, `fast-explorer`, `triage-scout`, `vision-scout`, `web-scout`, `/lean-workflow`, `/handoff`, `/swarm-prep`, `/token-stats`, and `/context-audit`.
