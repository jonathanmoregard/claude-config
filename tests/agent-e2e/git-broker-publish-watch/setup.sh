#!/usr/bin/env bash
# Workspace: a shallow clone of ~/.claude whose origin is the real GitHub repo,
# plus one new file to publish. The arm's own git_broker and github MCP servers,
# exactly as its mcp-servers.json registers them, are the agent's only MCP
# servers (the control arm registers neither, so it gets none). Bash runs in
# Claude's sandbox with the GitHub App host token unreadable (issue #165,
# decision 2026-10-19), so publishing and waiting must go through the broker.
#
# The run dir must sit under ~/worktrees, a broker repository root: run with
# --out ~/worktrees/<dir>. The brokers get HOME=/home/jonathan because the gh
# credential wrapper and the GitHub App wrapper locate their credentials from
# HOME, and the harness points HOME at the throwaway one.
set -euo pipefail

work=$1
run=$2
case $run in
  /home/jonathan/worktrees/*) ;;
  *) echo "run dir must be under /home/jonathan/worktrees (pass --out)" >&2; exit 2 ;;
esac

repo=$work/claude
git clone -q --depth 1 --branch master file:///home/jonathan/.claude "$repo"
git -C "$repo" remote set-url origin https://github.com/jonathanmoregard/claude-config.git
token="$(basename "$(dirname "$run")")-$(basename "$run")-$(date +%s)"
printf 'e2e/git-broker-%s\n' "$token" >"$work/branch.txt"
printf 'Scratch file from the git-broker agent e2e (%s). The PR is closed by the grader.\n' \
  "$token" >"$repo/e2e-git-broker-probe.txt"

python3 - "$run" "$HOME" <<'PY'
import json, sys
from pathlib import Path

run, home = Path(sys.argv[1]), Path(sys.argv[2])
servers = json.loads((home / ".claude" / "mcp-servers.json").read_text(encoding="utf-8"))
chosen = {}
for name in ("git_broker", "github"):
    if name not in servers:
        continue
    cfg = dict(servers[name])
    if name == "git_broker":
        cfg["args"] = [str(home / ".claude" / "mcps" / "git-broker" / "server.py")]
    else:
        # github-mux: run the arm's own server.py and config.json.
        live = "/home/jonathan/.claude/"
        cfg["args"] = [str(home / ".claude" / arg[len(live):]) if arg.startswith(live) else arg
                       for arg in cfg.get("args", [])]
    cfg["env"] = {**(cfg.get("env") or {}), "HOME": "/home/jonathan"}
    chosen[name] = cfg
if chosen:
    (run / "mcp.json").write_text(json.dumps({"mcpServers": chosen}), encoding="utf-8")

settings_path = home / ".claude" / "settings.json"
settings = json.loads(settings_path.read_text(encoding="utf-8"))
settings["sandbox"] = {
    "enabled": True,
    "failIfUnavailable": True,
    "allowUnsandboxedCommands": False,
    "autoAllowBashIfSandboxed": True,
    "filesystem": {"allowWrite": ["~/.local/state", "~/.cache", "~/worktrees", "/tmp",
                                  "~/.nix-memory-pressure"]},
    "network": {"allowedDomains": ["github.com", "api.github.com", "cache.nixos.org",
                                   "channels.nixos.org"],
                "allowAllUnixSockets": True},
}
deny = settings.setdefault("permissions", {}).setdefault("deny", [])
deny.append("Read(//home/jonathan/.cache/gh-tokens/**)")
settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
PY
