#!/usr/bin/env bash
# Minimal-access headless Claude Code agent. Subscription billing via
# CLAUDE_CODE_OAUTH_TOKEN (generate once with `claude setup-token`).
#
# Usage: CLAUDE_CODE_OAUTH_TOKEN=... ./bin/run-worker.sh "your prompt"
set -euo pipefail

: "${CLAUDE_CODE_OAUTH_TOKEN:?set CLAUDE_CODE_OAUTH_TOKEN (run 'claude setup-token' first)}"

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROFILE="$SCRIPT_DIR/.claude-profile"
SCRATCH="$SCRIPT_DIR/scratch"
PROMPT="${1:?prompt required}"

cd "$SCRATCH"

env -i \
  HOME="$SCRATCH" \
  PATH="/home/jonathan/.local/bin:/usr/bin:/bin" \
  CLAUDE_CONFIG_DIR="$PROFILE" \
  CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
  timeout 300 \
  claude -p "$PROMPT" \
    --settings "$PROFILE/settings.json" \
    --mcp-config "$PROFILE/empty-mcp.json" \
    --strict-mcp-config \
    --permission-mode dontAsk \
    --tools "" \
    --max-turns 10 \
    --output-format json
