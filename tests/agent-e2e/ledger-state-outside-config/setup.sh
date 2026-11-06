#!/usr/bin/env bash
# Workspace: the stub home-assistant MCP server (see _lib/ha-mcp-stub.sh).
#
# The ledger records permission events, not tool traffic, so the scenario needs
# a call that actually reaches a permission decision. ha_get_state is read-only
# and no allow rule covers it, which makes it the one call certain to be held
# rather than executed outright — see permission-mode-manual-asks, which turns
# on the same property.
set -euo pipefail
source "$(dirname "$0")/../_lib/ha-mcp-stub.sh"
