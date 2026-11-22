#!/usr/bin/env bash
# Registers broker_stub.py under each server name given (agent_e2e_broker,
# jobs_broker) in RUN/mcp.json, through the dev-MCP dispatcher; calls land in
# RUN/broker-calls.jsonl.
# Copied into $E2E_BIN because a broker-run campaign keeps its scenarios under
# /tmp, which the harness hides from the agent.
# Usage from setup.sh:  source "$(dirname "$0")/../_lib/broker-stub.sh" NAME...
set -euo pipefail
RUN="${E2E_RUN:?}"
BIN="${E2E_BIN:?}"
lib="$(dirname "${BASH_SOURCE[0]}")"
cp "$lib/broker_stub.py" "$BIN/broker_stub.py"
: > "$RUN/broker-calls.jsonl"
for name in "$@"; do
  python3 "$lib/mcp_stub.py" --env "E2E_RUN_DIR=$RUN" "$name" "$BIN/broker_stub.py" -- "$name"
done
