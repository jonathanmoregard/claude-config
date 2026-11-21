#!/usr/bin/env bash
# Workspace: a shallow clone of ~/Repos/claude-config whose origin is the real GitHub repo,
# plus one new file to publish. The arm's own git_broker and github MCP servers,
# whichever its mcp-servers.json registers, are the agent's only MCP servers
# (the control arm registers neither, so it gets none). Bash runs in
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
git clone -q --depth 1 --branch master file:///home/jonathan/Repos/claude-config "$repo"
git -C "$repo" remote set-url origin https://github.com/jonathanmoregard/claude-config.git
token="$(basename "$(dirname "$run")")-$(basename "$run")-$(date +%s)"
mkdir -p "$repo/experiments"
printf 'e2e/git-broker-%s\n' "$token" >"$work/branch.txt"
printf 'Scratch file from the git-broker agent e2e (%s). The PR is closed by the grader.\n' \
  "$token" >"$repo/experiments/e2e-git-broker-probe.txt"

python3 - "$run" "$HOME" "$(dirname "$0")/../_lib/mcp_stub.py" <<'PY'
import json, subprocess, sys
from pathlib import Path

run, home, mcp_stub = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
servers = json.loads((home / ".claude" / "mcp-servers.json").read_text(encoding="utf-8"))
if "github" in servers:
    # github-mux is a uv script, not a stdlib one the dispatcher can run:
    # registered as-is, with the arm's own server.py and config.json.
    cfg = dict(servers["github"])
    live = "/home/jonathan/.claude/"
    cfg["args"] = [str(home / ".claude" / arg[len(live):]) if arg.startswith(live) else arg
                   for arg in cfg.get("args", [])]
    # The harness mounts the real HOME read-only, so uv's default cache is unwritable.
    cfg["env"] = {**(cfg.get("env") or {}), "HOME": "/home/jonathan",
                  "UV_CACHE_DIR": str(run / "uv-cache")}
    (run / "mcp.json").write_text(json.dumps({"mcpServers": {"github": cfg}}), encoding="utf-8")
if "git_broker" in servers:
    cfg = servers["git_broker"]
    env = {**(cfg.get("env") or {}), "HOME": "/home/jonathan"}
    argv = [sys.executable, mcp_stub, "git_broker",
            str(home / ".claude" / "mcps" / "git-broker" / "server.py"),
            *(f"--env={k}={v}" for k, v in env.items())]
    if "timeout" in cfg:
        argv.append(f"--timeout={cfg['timeout']}")
    subprocess.run(argv, check=True)

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
