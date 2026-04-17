#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

/usr/bin/python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
echo "ghostty-mcp setup complete"
