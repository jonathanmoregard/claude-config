#!/usr/bin/env python3
"""Register the arm's github and git_broker MCP servers for the scenario.

    register_mcp.py RUN HOME MCP_STUB

The managed MCP allowlist matches exact argv, so on a host with
/etc/claude-code/mcp-servers.json the github entry must be that file's, verbatim.

HOME is the staged dir setup.sh can read the arm's mcp-servers.json from. The
servers are spawned inside the sandbox, so the paths written for them name
the agent's HOME, E2E_HOME (under bwrap the real HOME's path, where the staged
dir is mounted; RUN/home itself is hidden there).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

LIVE_HOME = "/home/jonathan"
POLICY_SERVERS = Path("/etc/claude-code/mcp-servers.json")
DISPATCHER_PYTHON = "/run/current-system/sw/bin/python3"


def arm_path(arg: str, home: Path) -> str:
    live = f"{LIVE_HOME}/.claude/"
    return str(home / ".claude" / arg[len(live):]) if arg.startswith(live) else arg


def github_entry(arm_servers: dict, home: Path, run: Path,
                 policy_file: Path | None = None) -> dict | None:
    if "github" not in arm_servers:
        return None
    policy_file = POLICY_SERVERS if policy_file is None else policy_file
    if policy_file.is_file():
        cfg = json.loads(policy_file.read_text(encoding="utf-8")).get("github")
        if cfg is None:
            print(f"{policy_file} has no github entry, so the allowlist would drop "
                  "the arm's github server", file=sys.stderr)
            sys.exit(2)
    else:
        # Pre-policy host: the arm's own server.py and config.json, under the arm HOME.
        cfg = dict(arm_servers["github"])
        cfg["args"] = [arm_path(arg, home) for arg in cfg.get("args", [])]
    cfg = dict(cfg)
    # HOME is the live path, where the scenario's `lend` puts the credentials
    # under bwrap; uv's cache stays in the run rather than in either HOME.
    cfg["env"] = {**(cfg.get("env") or {}), "HOME": LIVE_HOME,
                  "UV_CACHE_DIR": str(run / "uv-cache")}
    return cfg


def broker_argv(cfg: dict, home: Path, mcp_stub: str) -> list[str]:
    if cfg.get("command") != DISPATCHER_PYTHON:
        print(f"git_broker command is {cfg.get('command')!r}, but the stub dispatcher "
              f"always runs {DISPATCHER_PYTHON}", file=sys.stderr)
        sys.exit(2)
    env = {**(cfg.get("env") or {}), "HOME": LIVE_HOME}
    script, *script_args = cfg["args"]
    options = [f"--env={k}={v}" for k, v in env.items()]
    if "timeout" in cfg:
        options.append(f"--timeout={cfg['timeout']}")
    argv = [sys.executable, mcp_stub, *options, "git_broker", arm_path(script, home)]
    if script_args:
        argv += ["--", *script_args]
    return argv


def main(run: Path, home: Path, mcp_stub: str) -> None:
    servers = json.loads((home / ".claude" / "mcp-servers.json").read_text(encoding="utf-8"))
    home = Path(os.environ.get("E2E_HOME") or home)
    github = github_entry(servers, home, run)
    if github is not None:
        (run / "mcp.json").write_text(json.dumps({"mcpServers": {"github": github}}),
                                      encoding="utf-8")
    if "git_broker" in servers:
        subprocess.run(broker_argv(servers["git_broker"], home, mcp_stub), check=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
