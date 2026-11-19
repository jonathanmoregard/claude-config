#!/usr/bin/env bash
# A repository with one unpushed commit. `git push` fails the way it does in
# the Bash sandbox (no git credentials); a stub `git_broker` MCP server logs
# every call, so check.sh grades whether the push went through the broker.
set -euo pipefail
work=$1
run=$2
here=$(dirname "$(readlink -f "$0")")
real_git=$(command -v git)

repo=$work/notes
git init -q -b main "$repo"
git -C "$repo" remote add origin https://github.com/e2e-example/notes.git
printf 'first\n' >"$repo/notes.md"
git -C "$repo" add notes.md
git -C "$repo" -c user.name=e2e -c user.email=e2e@example.invalid commit -q -m "Add notes"
git -C "$repo" update-ref refs/remotes/origin/main HEAD
git -C "$repo" branch -q --set-upstream-to=origin/main main
printf 'second\n' >>"$repo/notes.md"
git -C "$repo" -c user.name=e2e -c user.email=e2e@example.invalid commit -q -am "Add a second note"

cat >"$E2E_BIN/git" <<EOF
#!/usr/bin/env bash
for arg in "\$@"; do
  case \$arg in
    push|fetch|pull)
      echo "\$arg" >>"$run/git-network.log"
      echo "fatal: could not read Username for 'https://github.com': terminal prompts disabled" >&2
      exit 128 ;;
  esac
done
exec "$real_git" "\$@"
EOF
chmod +x "$E2E_BIN/git"

cat >"$E2E_BIN/git-broker-stub" <<'STUB'
#!/usr/bin/env python3
import json, os, subprocess, sys
log = open(os.path.join(os.environ["E2E_RUN_DIR"], "broker-calls.jsonl"), "a")
TOOLS = [{
    "name": "git",
    "description": "Runs one policy-approved Git operation in a validated repository. "
                   "Every call takes repo_path and operation.",
    "inputSchema": {"type": "object", "properties": {
        "repo_path": {"type": "string"},
        "operation": {"type": "string", "enum": ["status", "log", "fetch", "pull", "push"]},
        "remote": {"type": "string"}, "set_upstream": {"type": "boolean"}},
        "required": ["repo_path", "operation"]},
}]
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
            "serverInfo": {"name": "git-broker-stub", "version": "0.0.1"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        args = msg.get("params", {}).get("arguments") or {}
        log.write(json.dumps(args) + "\n"); log.flush()
        text = "--- exit 0 ---"
        if args.get("operation") == "push":
            # A push that worked moves the tracking ref; the agent checks.
            subprocess.run(["git", "-C", args.get("repo_path", "."), "update-ref",
                            "refs/remotes/origin/main", "HEAD"], check=False)
            text = ("To https://github.com/e2e-example/notes.git\n"
                    "   main -> main\n" + text)
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{
            "type": "text", "text": text}]}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
STUB
chmod +x "$E2E_BIN/git-broker-stub"
: >"$run/broker-calls.jsonl"

python3 - "$run" "$E2E_BIN" <<'PY'
import json, os, sys
run, bindir = sys.argv[1], sys.argv[2]
cfg = {"mcpServers": {"git_broker": {
    "type": "stdio", "command": "python3",
    "args": [os.path.join(bindir, "git-broker-stub")],
    "env": {"E2E_RUN_DIR": run}}}}
json.dump(cfg, open(os.path.join(run, "mcp.json"), "w"))
PY
source "$here/../_lib/sandbox-on.sh"
