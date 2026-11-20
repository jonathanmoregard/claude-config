#!/usr/bin/env bash
# Registers broker_stub.py under each server name given (ai_router_broker,
# agent_e2e_broker) in RUN/mcp.json; calls land in RUN/broker-calls.jsonl.
# Copied into $E2E_BIN because a broker-run campaign keeps its scenarios under
# /tmp, which the harness hides from the agent.
# Usage from setup.sh:  source "$(dirname "$0")/../_lib/broker-stub.sh" NAME...
set -euo pipefail
RUN="${E2E_RUN:?}"
BIN="${E2E_BIN:?}"
cp "$(dirname "${BASH_SOURCE[0]}")/broker_stub.py" "$BIN/broker_stub.py"
: > "$RUN/broker-calls.jsonl"
python3 - "$RUN" "$BIN/broker_stub.py" "$@" <<'PY'
import json, sys
run, stub, names = sys.argv[1], sys.argv[2], sys.argv[3:]
servers = {n: {"type": "stdio", "command": "python3", "args": [stub, n],
               "env": {"E2E_RUN_DIR": run}} for n in names}
json.dump({"mcpServers": servers}, open(f"{run}/mcp.json", "w"))
PY
