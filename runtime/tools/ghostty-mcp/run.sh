#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

if [ ! -d "$VENV" ]; then
    echo "ERROR: venv not found — run setup.sh first" >&2
    exit 1
fi

exec "$VENV/bin/python" "$SCRIPT_DIR/server.py"
