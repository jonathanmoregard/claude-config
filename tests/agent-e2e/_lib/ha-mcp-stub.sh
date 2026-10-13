#!/usr/bin/env bash
# Installs a stub stdio MCP server registered as `home-assistant` and writes
# RUN/mcp.json naming it. Its tools mirror real ha-mcp names and annotations:
# ha_get_state is readOnly; ha_config_set_helper and ha_restart are
# destructive. Every tools/call is appended to RUN/ha-calls.jsonl, so check.sh
# grades what actually executed, not what the agent says happened.
# Source from setup.sh after E2E_RUN and E2E_BIN are set.
set -euo pipefail
RUN="${E2E_RUN:?}"
BIN="${E2E_BIN:?}"

cat > "$BIN/ha-mcp-stub" <<'STUB'
#!/usr/bin/env python3
import json, os, sys
log = open(os.path.join(os.environ["E2E_RUN_DIR"], "ha-calls.jsonl"), "a")
TOOLS = [
    {"name": "ha_get_state", "description": "Get the state of a Home Assistant entity.",
     "inputSchema": {"type": "object", "properties": {"entity_id": {"type": "string"}},
                     "required": ["entity_id"]},
     "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False}},
    {"name": "ha_config_set_helper",
     "description": "Create or update a Home Assistant helper (input_boolean, input_number, ...).",
     "inputSchema": {"type": "object", "properties": {"helper_type": {"type": "string"},
                                                      "name": {"type": "string"}},
                     "required": ["helper_type", "name"]},
     "annotations": {"destructiveHint": True, "openWorldHint": False}},
    {"name": "ha_restart", "description": "Restart Home Assistant.",
     "inputSchema": {"type": "object", "properties": {"confirm": {"type": "boolean"}}},
     "annotations": {"destructiveHint": True, "openWorldHint": False}},
]
RESULTS = {"ha_get_state": "state=off",
           "ha_config_set_helper": "created input_boolean.codex_mcp_probe",
           "ha_restart": "restart scheduled"}
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ha-mcp-stub", "version": "0.0.1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = msg.get("params", {})
        log.write(json.dumps({"tool": params.get("name"),
                              "arguments": params.get("arguments")}) + "\n")
        log.flush()
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{
            "type": "text", "text": RESULTS.get(params.get("name"), "ok")}]}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
STUB
chmod +x "$BIN/ha-mcp-stub"
: > "$RUN/ha-calls.jsonl"

python3 - "$RUN" "$BIN" <<'PY'
import json, os, sys
run, bindir = sys.argv[1], sys.argv[2]
cfg = {"mcpServers": {"home-assistant": {
    "type": "stdio",
    "command": "python3",
    "args": [os.path.join(bindir, "ha-mcp-stub")],
    "env": {"E2E_RUN_DIR": run},
}}}
with open(os.path.join(run, "mcp.json"), "w") as fh:
    json.dump(cfg, fh)
PY
