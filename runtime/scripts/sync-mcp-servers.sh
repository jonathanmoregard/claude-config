#!/usr/bin/env bash
# Sync mcpServers slice between Claude Code live state and tracked file.
#
#   export  ~/.claude.json           → ~/.claude/mcp-servers.json  (capture live)
#   import  ~/.claude/mcp-servers.json → ~/.claude.json            (apply tracked)
#
# import replaces .mcpServers wholesale — removals in the tracked file
# propagate. Atomic write (tmp + mv) so a crash mid-run cannot corrupt
# .claude.json.

set -euo pipefail

LIVE="${HOME}/.claude.json"
TRACKED="${HOME}/.claude/mcp-servers.json"

case "${1:-}" in
  export)
    [[ -f "$LIVE" ]] || { echo "missing $LIVE" >&2; exit 1; }
    jq '.mcpServers' "$LIVE" > "${TRACKED}.tmp"
    mv "${TRACKED}.tmp" "$TRACKED"
    echo "exported → $TRACKED"
    ;;
  import)
    [[ -f "$TRACKED" ]] || { echo "missing $TRACKED" >&2; exit 1; }
    [[ -f "$LIVE" ]] || { echo "missing $LIVE" >&2; exit 1; }
    jq --slurpfile mcp "$TRACKED" '.mcpServers = $mcp[0]' "$LIVE" > "${LIVE}.tmp"
    mv "${LIVE}.tmp" "$LIVE"
    echo "imported → $LIVE"
    ;;
  diff)
    diff <(jq -S '.mcpServers' "$LIVE") <(jq -S '.' "$TRACKED") || true
    ;;
  *)
    echo "usage: $0 {export|import|diff}" >&2
    exit 2
    ;;
esac
