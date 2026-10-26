#!/usr/bin/env bash
# FutureSearch retired 2026-10-26. The arm's own mcp-servers.json decides
# whether a `futuresearch-gate` server exists, exactly as the live config would:
# when it is registered, a local STUB with the gate's tool names stands in for
# it (the real gate would call scanner vendors at boot), so the old arm shows
# the tools an agent would have had. The new arm registers nothing. Every
# tools/call is logged to RUN/fs-calls.jsonl.
set -euo pipefail
RUN="${E2E_RUN:?}"
BIN="${E2E_BIN:?}"

cat > "$BIN/futuresearch-gate-stub" <<'STUB'
#!/usr/bin/env python3
import json, os, sys
log = open(os.path.join(os.environ["E2E_RUN_DIR"], "fs-calls.jsonl"), "a")
def tool(name, desc):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": {"question": {"type": "string"}}}}
TOOLS = [tool("forecast_now", "Submit a FutureSearch forecast and wait for the result."),
         tool("forecast_submit", "Submit a FutureSearch forecast task."),
         tool("forecast_results", "Fetch injection-screened FutureSearch forecast results.")]
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
            "serverInfo": {"name": "futuresearch-gate-stub", "version": "0.0.1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = msg.get("params", {})
        log.write(json.dumps({"tool": params.get("name")}) + "\n"); log.flush()
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{
            "type": "text", "text": "task_id=stub-1 status=pending"}]}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
STUB
chmod +x "$BIN/futuresearch-gate-stub"
: > "$RUN/fs-calls.jsonl"

python3 - "$RUN" "$BIN" "$HOME" <<'PY'
import json, os, sys
run, bindir, home = sys.argv[1:4]
servers = json.load(open(os.path.join(home, ".claude", "mcp-servers.json"), encoding="utf-8"))
if "futuresearch-gate" in servers:
    cfg = {"mcpServers": {"futuresearch-gate": {
        "type": "stdio", "command": "python3",
        "args": [os.path.join(bindir, "futuresearch-gate-stub")],
        "env": {"E2E_RUN_DIR": run}}}}
    with open(os.path.join(run, "mcp.json"), "w") as fh:
        json.dump(cfg, fh)
PY
