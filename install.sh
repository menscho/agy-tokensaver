#!/usr/bin/env bash
# Token Saver (agy-tokensaver) — 1-Command Installer & Stats-Preserving Updater (Linux / macOS / WSL)
# Usage: curl -fsSL https://raw.githubusercontent.com/menscho/agy-tokensaver/main/install.sh | bash
set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 (3.9+) is required." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/install.py" && -f "$SCRIPT_DIR/plugin.json" ]]; then
  exec python3 "$SCRIPT_DIR/install.py" --cli --yes "$@"
fi

TMP_INST="$(mktemp)"
trap 'rm -f "$TMP_INST"' EXIT
curl -fsSL "https://raw.githubusercontent.com/menscho/agy-tokensaver/main/install.py" -o "$TMP_INST"
exec python3 "$TMP_INST" --cli --yes --remote "$@"
