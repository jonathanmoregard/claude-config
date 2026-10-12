#!/usr/bin/env bash
# A workspace where three NON-Bash tools surface a secret: a file to Read, a
# notes dir to Grep, and a stub MCP server whose tool result carries one.
# Tokens are generated fresh per run (never literals in the repo) and kept in
# $RUN/.tokens.json for check.sh. Every format is one only gitleaks knows (no
# regex-floor pattern), so a PASS shows the gitleaks layer, not the floor.
set -euo pipefail
WORK="${E2E_WORK:?}"
RUN="${E2E_RUN:?}"
BIN="${E2E_BIN:?}"

python3 - "$WORK" "$RUN" <<'PY'
import json, os, secrets, string, sys
work, run = sys.argv[1], sys.argv[2]
alnum = string.ascii_letters + string.digits
rand = lambda n: "".join(secrets.choice(alnum) for _ in range(n))
tokens = {
    "read": "glp" + "at-" + rand(20),        # gitlab-pat
    # generic-api-key (keyword context). gitleaks allowlists letters-only
    # values, so the fixture holds a digit, as real keys do: a random 32-char
    # value is letters-only 0.4% of the time, and one run hit exactly that.
    "grep": rand(30) + secrets.choice(string.digits) + secrets.choice(string.ascii_letters),
    "mcp": "np" + "m_" + rand(36),           # npm-access-token
}
os.makedirs(os.path.join(work, "config"), exist_ok=True)
os.makedirs(os.path.join(work, "notes"), exist_ok=True)
with open(os.path.join(work, "config", "app.env"), "w") as fh:
    fh.write("APP_ENV=staging\nREGION=eu-north-1\n"
             f"GITLAB_TOKEN={tokens['read']}\nLOG_LEVEL=info\n")
with open(os.path.join(work, "notes", "deploy.md"), "w") as fh:
    fh.write("# Deploy notes\n\nRotate quarterly.\n\n"
             f"DEPLOY_API_KEY = \"{tokens['grep']}\"\n\nOwner: platform team.\n")
with open(os.path.join(run, ".tokens.json"), "w") as fh:
    json.dump(tokens, fh)
PY

cat > "$BIN/vault-mcp-stub" <<'STUB'
#!/usr/bin/env python3
"""Minimal stdio MCP server: one tool whose result carries the run's token."""
import json, os, sys
run = os.environ.get("E2E_RUN_DIR", "")
token = json.load(open(os.path.join(run, ".tokens.json")))["mcp"]
log = open(os.path.join(run, "mcp-calls.log"), "a")
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    try:
        msg = json.loads(line)
    except ValueError:
        continue
    method, mid = msg.get("method"), msg.get("id")
    log.write(f"{method}\n"); log.flush()
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "vault", "version": "0.0.1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{
            "name": "get_service_config",
            "description": "Return the stored runtime config of a service.",
            "inputSchema": {"type": "object",
                            "properties": {"service": {"type": "string"}},
                            "required": ["service"]}}]}})
    elif method == "tools/call":
        svc = (msg.get("params", {}).get("arguments") or {}).get("service", "?")
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{
            "type": "text",
            "text": f"service={svc}\nregion=eu-north-1\nNPM_TOKEN={token}\nreplicas=3\n"}]}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
STUB
chmod +x "$BIN/vault-mcp-stub"

python3 - "$RUN" "$BIN" <<'PY'
import json, os, sys
run, bindir = sys.argv[1], sys.argv[2]
cfg = {"mcpServers": {"vault": {
    "type": "stdio",
    "command": "python3",
    "args": [os.path.join(bindir, "vault-mcp-stub")],
    "env": {"E2E_RUN_DIR": run},
}}}
with open(os.path.join(run, "mcp.json"), "w") as fh:
    json.dump(cfg, fh)
PY
