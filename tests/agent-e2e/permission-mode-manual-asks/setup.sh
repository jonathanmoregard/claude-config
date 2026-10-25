#!/usr/bin/env bash
# Workspace: the stub home-assistant MCP server (see _lib/ha-mcp-stub.sh).
# ha_get_state is read-only but no allow rule covers it, so it is exactly the
# call whose fate defaultMode decides.
set -euo pipefail
source "$(dirname "$0")/../_lib/ha-mcp-stub.sh"
