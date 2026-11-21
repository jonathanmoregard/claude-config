#!/usr/bin/env bash
# Workspace: the staged candidate's real app runner, as the agent's only MCP
# server, with its state root moved into this run (the real HOME is read-only
# for the agent). It drives a real host unit against the dedicated smoke
# worktree. The control arm has no runner, so it gets no MCP config at all.
set -euo pipefail

work=$1
run=$2
source=$HOME/.claude/mcps/klaffat-app-runner
[[ -f $source/server.py ]] || exit 0

cp -r "$source" "$run/runner"
python3 - "$run" <<'PY'
import json, sys
from pathlib import Path

run = Path(sys.argv[1])
config_path = run / "runner" / "config.json"
config = json.loads(config_path.read_text(encoding="utf-8"))
config["appRunner"]["stateRoot"] = str(run / "state")
config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
PY
manifest=$HOME/.claude/mcp-servers.json
timeout_ms=$(jq -r '."klaffat-app-runner".timeout // empty' "$manifest" 2>/dev/null || true)
[[ -n $timeout_ms ]] || { echo "no klaffat-app-runner timeout in $manifest" >&2; exit 2; }
command=$(jq -r '."klaffat-app-runner".command // empty' "$manifest" 2>/dev/null || true)
[[ $command == /run/current-system/sw/bin/python3 ]] || {
  echo "klaffat-app-runner command is '$command' in $manifest; the stub dispatcher always runs /run/current-system/sw/bin/python3" >&2; exit 2; }
python3 "$(dirname "$0")/../_lib/mcp_stub.py" klaffat-app-runner "$run/runner/server.py" --timeout "$timeout_ms"
