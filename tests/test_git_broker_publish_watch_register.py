"""MCP registration for the git-broker-publish-watch scenario."""

from __future__ import annotations

import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests/agent-e2e/git-broker-publish-watch/register_mcp.py"

spec = importlib.util.spec_from_file_location("register_mcp", SCRIPT)
reg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reg)

LIVE = "/home/jonathan/.claude/mcps/github-mux/server.py"
ARM = {"github": {"type": "stdio", "command": "uv", "args": ["run", "--script", LIVE],
                  "env": {}, "timeout": 600000}}
POLICY = {"github": {"type": "stdio", "command": "/packaged/github-mux",
                     "args": ["--config", "/nix/store/x/config.json"],
                     "env": {"A": "1"}, "timeout": 300000}}


def test_policy_entry_is_registered_verbatim_with_home_and_cache(tmp_path):
    policy = tmp_path / "mcp-servers.json"
    policy.write_text(json.dumps(POLICY))
    out = reg.github_entry(ARM, tmp_path / "home", tmp_path / "run", policy)
    assert out == {**POLICY["github"],
                   "env": {"A": "1", "HOME": "/home/jonathan",
                           "UV_CACHE_DIR": str(tmp_path / "run/uv-cache")}}


def test_policy_without_github_falls_back_to_the_arm_entry(tmp_path):
    policy = tmp_path / "mcp-servers.json"
    policy.write_text(json.dumps({"other": {}}))
    out = reg.github_entry(ARM, tmp_path / "home", tmp_path / "run", policy)
    assert out["args"][2] == f"{tmp_path}/home/.claude/mcps/github-mux/server.py"


def test_no_policy_file_uses_the_arm_entry_under_the_arm_home(tmp_path):
    out = reg.github_entry(ARM, tmp_path / "home", tmp_path / "run", tmp_path / "absent")
    assert out["command"] == "uv"
    assert out["args"][2] == f"{tmp_path}/home/.claude/mcps/github-mux/server.py"
    assert out["env"]["HOME"] == "/home/jonathan"


def test_no_github_anywhere_registers_nothing(tmp_path):
    assert reg.github_entry({}, tmp_path, tmp_path, tmp_path / "absent") is None


def test_broker_argv_rewrites_the_script_and_passes_the_rest_after_dashes(tmp_path):
    cfg = {"args": ["/home/jonathan/.claude/mcps/git-broker/server.py", "--x", "y"],
           "env": {"K": "v"}, "timeout": 5}
    argv = reg.broker_argv(cfg, tmp_path, "stub.py")
    assert argv[1:] == ["stub.py", "git_broker",
                        f"{tmp_path}/.claude/mcps/git-broker/server.py",
                        "--env=K=v", "--env=HOME=/home/jonathan", "--timeout=5",
                        "--", "--x", "y"]


def test_broker_argv_without_extra_args_has_no_separator(tmp_path):
    cfg = {"args": ["/home/jonathan/.claude/mcps/git-broker/server.py"]}
    assert "--" not in reg.broker_argv(cfg, tmp_path, "stub.py")
