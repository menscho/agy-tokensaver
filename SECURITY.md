# Security Policy

## Reporting a Vulnerability

Please report security issues privately through GitHub Security Advisories on `menscho/agy-tokensaver` rather than opening a public issue. Include reproduction steps, OS/Perl/Python versions, and any relevant input payload.

## Outbound & Local Network Calls

`agy-tokensaver` hooks (`scripts/*.pl`) and local telemetry never open network connections or phone home. The only network/HTTP requests in the repository are:

| Endpoint / Protocol | File | When It Fires |
| :--- | :--- | :--- |
| `GET https://api.github.com/repos/menscho/agy-tokensaver/releases/latest` | `mcp_app/updater.py`, `install.py` | Only when you click Check/Install Update in the HUD or run `install.py` remotely. |
| `GET` release `SHA256SUMS.txt` & `agy-tokensaver-v*.zip` | `mcp_app/updater.py`, `install.py` | Only when you trigger Install Update or `install.py --remote`. |
| `gh attestation verify` (Sigstore / GitHub provenance lookup) | `mcp_app/updater.py`, `install.py` | Optional during update/remote install if `gh` CLI is installed. |
| `POST http://${ANTIGRAVITY_LS_ADDRESS}/exa.language_server_pb.LanguageServerService/RefreshMcpServers` | `mcp_app/updater.py`, `install.py` | Localhost loopback only; fires once after install/update finishes when Antigravity env vars (`ANTIGRAVITY_LS_ADDRESS`, `ANTIGRAVITY_CSRF_TOKEN`) are present. |

## What Gets Written Where

| Location | Contents | Permissions / Retention |
| :--- | :--- | :--- |
| `$TMPDIR/agy-tokensaver-<uid>` (Unix) / `%TEMP%\agy-tokensaver-<user>` (Windows) | Byte-exact raw command outputs ("spill files") whenever `cmd_wrap.pl` compresses output, plus downscaled screenshot JPEGs in opt-in Mode 2. **May contain sensitive command output** (tokens, `.env` values, SSH logs). | Directory created with `0700` on Unix (owner-only) and user-only `icacls` ACL on Windows. Spill files are created with `0600` (`sysopen` + `chmod`), never following symlinks. Files older than **7 days** (`AGY_TS_SPILL_DAYS`, configurable) or beyond 200 files are pruned automatically. Purge manually anytime with `python3 scripts/ts_inspect.py purge`. |
| `~/.gemini/antigravity/plugin_data/token-saver/` | Local telemetry (`events.jsonl`, `sessions.json`), HUD settings (`hud_config.json`), read/loop tracking (`reads/`), alerts (`alerts/`), and archived handoff snapshots (`handoffs/`). | Local per-user directory. Preserved across plugin updates; deleted only if you remove it manually or uninstall with data removal. |
| `~/.gemini/config/plugins/token-saver/` | Installed plugin scripts, rules, and MCP app files. | Read/executed locally by Antigravity. |

## Kill Switch & Sensitive Sessions

- Set `AGY_TS_SHELL=off` in your environment to disable all command wrapping (and spill file writes) immediately.
- Run `python3 scripts/ts_inspect.py purge` after handling sensitive logs or secrets to wipe all spill files immediately, or set `AGY_TS_SPILL_DAYS=1` for shorter retention.

## Updater & Supply-Chain Trust Model

- `install.py` and the HUD updater install only from tagged GitHub Releases (`v*`) and verify the downloaded zip against `SHA256SUMS.txt` attached to the same release.
- **Trust boundary:** comparing a release zip against a checksum hosted in the same GitHub Release proves **download integrity** (protection against corruption and transit tampering), not **authenticity** against a compromised release upload.
- Releases built by `.github/workflows/release.yml` include GitHub Artifact Attestations (`actions/attest-build-provenance`). When `gh` is installed, you can (and the updater optionally does) verify provenance cryptographically with:

```bash
gh attestation verify agy-tokensaver-vX.Y.Z.zip --repo menscho/agy-tokensaver
```

## Maintainer Tag Protection Checklist

Published release tags (`v*`) are immutable and must never be moved or deleted. In GitHub repository settings (requires admin access):

1. Open **Settings → Rules → Rulesets → New tag ruleset**.
2. Target pattern: `refs/tags/v*`.
3. Enable **Restrict deletions** and **Restrict updates** (blocks force-pushing or moving tags) and restrict creation to maintainers / CI.

