# Security Policy

## Reporting a Vulnerability

Please report security issues privately through GitHub Security Advisories on `menscho/agy-tokensaver` rather than opening a public issue. Include reproduction steps, OS/Perl/Python versions, and any relevant input payload.

## Outbound and Local Network Calls

Hooks and local telemetry never open network connections or phone home. The only HTTP or CLI network calls in this repository are:

| Endpoint / Protocol | File | When It Fires |
| :--- | :--- | :--- |
| `GET https://api.github.com/repos/menscho/agy-tokensaver/releases/latest` | `mcp_app/updater.py`, `install.py` | Only when you click Check/Install Update in the dashboard or run `install.py --remote`. |
| `GET` release `SHA256SUMS.txt` and `agy-tokensaver-v*.zip` | `mcp_app/updater.py`, `install.py` | Only when you trigger Install Update or `install.py --remote`. |
| `gh attestation verify` (GitHub build provenance) | `mcp_app/updater.py`, `install.py` | Optional during update/remote install if `gh` CLI is installed. |
| `POST http://${ANTIGRAVITY_LS_ADDRESS}/exa.language_server_pb.LanguageServerService/RefreshMcpServers` | `mcp_app/updater.py`, `install.py` | Localhost loopback only; fires once after install or update completes. |

## What Gets Written Where

| Location | Contents | Permissions / Retention |
| :--- | :--- | :--- |
| `~/.gemini/antigravity/plugin_data/token-saver/` | Local telemetry (`events.jsonl`, `sessions.json`), HUD settings (`hud_config.json`), read/loop tracking (`reads/`), alerts (`alerts/`), and archived handoffs (`handoffs/`). | Local per-user directory. Preserved across plugin updates; deleted only if you remove it manually or run `install.py --uninstall --purge-stats`. |
| `$TMPDIR/agy-tokensaver-<uid>` (Unix) / `%TEMP%\agy-tokensaver-<user>` (Windows) | Optional handoff markdown export (`handoff_pack.py`) and downscaled JPEGs if Compact Reads is enabled. | Directory created with `0700` on Unix. Clean anytime with `python3 scripts/ts_inspect.py purge`. |
| `~/.gemini/config/plugins/token-saver/` | Installed plugin scripts, skills, and MCP app files. | Read and executed locally by Antigravity. |

## Kill Switch

- Set the HUD dropdown to **Off**, or set `AGY_TS_HOOKS=off` in your environment, to make every hook return `{"decision":"allow"}` / `{}` immediately.

## Updater and Supply-Chain Trust Model

- `install.py` and the HUD updater install only from tagged GitHub Releases (`v*`) and verify downloaded archives against `SHA256SUMS.txt` attached to the same release.
- Comparing a release zip against a checksum on the same release proves download integrity against transit corruption, not authenticity against a compromised release upload. Releases built by `.github/workflows/release.yml` also include GitHub Artifact Attestations (`actions/attest-build-provenance`), verifiable with:

```bash
gh attestation verify agy-tokensaver-vX.Y.Z.zip --repo menscho/agy-tokensaver
```

Published release tags (`v*`) are immutable and must never be moved or deleted. Enable GitHub tag deletion/update protection rulesets (`refs/tags/v*`) in repository settings.
