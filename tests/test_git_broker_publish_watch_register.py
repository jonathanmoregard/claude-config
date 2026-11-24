"""MCP registration for the git-broker-publish-watch scenario."""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests/agent-e2e/git-broker-publish-watch/register_mcp.py"

STUB = ROOT / "tests/agent-e2e/_lib/mcp_stub.py"

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


def test_no_policy_file_uses_the_arm_entry_under_the_arm_home(tmp_path):
    out = reg.github_entry(ARM, tmp_path / "home", tmp_path / "run", tmp_path / "absent")
    assert out["command"] == "uv"
    assert out["args"][2] == f"{tmp_path}/home/.claude/mcps/github-mux/server.py"
    assert out["env"]["HOME"] == "/home/jonathan"


def test_no_github_anywhere_registers_nothing(tmp_path):
    assert reg.github_entry({}, tmp_path, tmp_path, tmp_path / "absent") is None


PYTHON = "/run/current-system/sw/bin/python3"


def test_broker_argv_rewrites_the_script_and_passes_the_rest_after_dashes(tmp_path):
    cfg = {"command": PYTHON, "args": ["/home/jonathan/.claude/mcps/git-broker/server.py", "--x", "y"],
           "env": {"K": "v"}, "timeout": 5}
    argv = reg.broker_argv(cfg, tmp_path, "stub.py")
    assert argv[1:] == ["stub.py", "--env=K=v", "--env=HOME=/home/jonathan", "--timeout=5",
                        "git_broker", f"{tmp_path}/.claude/mcps/git-broker/server.py",
                        "--", "--x", "y"]


def test_broker_argv_without_extra_args_has_no_separator(tmp_path):
    cfg = {"command": PYTHON, "args": ["/home/jonathan/.claude/mcps/git-broker/server.py"]}
    assert "--" not in reg.broker_argv(cfg, tmp_path, "stub.py")


def test_arm_without_github_gets_none_even_when_the_policy_has_it(tmp_path):
    policy = tmp_path / "mcp-servers.json"
    policy.write_text(json.dumps(POLICY))
    assert reg.github_entry({}, tmp_path / "home", tmp_path / "run", policy) is None


def test_policy_lacking_github_fails_setup_when_the_arm_wants_it(tmp_path, capsys):
    policy = tmp_path / "mcp-servers.json"
    policy.write_text(json.dumps({"other": {}}))
    with pytest.raises(SystemExit) as exc:
        reg.github_entry(ARM, tmp_path / "home", tmp_path / "run", policy)
    assert exc.value.code == 2
    assert f"{policy} has no github entry" in capsys.readouterr().err


@pytest.mark.parametrize("cfg", [
    {"args": ["/home/jonathan/.claude/mcps/git-broker/server.py"]},
    {"command": "uv", "args": ["/home/jonathan/.claude/mcps/git-broker/server.py"]},
    {"command": "python3", "args": ["/home/jonathan/.claude/mcps/git-broker/server.py"]},
])
def test_broker_argv_refuses_a_command_the_dispatcher_would_not_run(tmp_path, capsys, cfg):
    with pytest.raises(SystemExit) as exc:
        reg.broker_argv(cfg, tmp_path, "stub.py")
    assert exc.value.code == 2
    assert PYTHON in capsys.readouterr().err


def test_broker_argv_parses_with_the_stub_helper_argparse(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    cfg = {"command": PYTHON, "args": ["/home/jonathan/.claude/mcps/git-broker/server.py", "--x", "y"],
           "env": {"K": "v"}, "timeout": 5}
    argv = reg.broker_argv(cfg, tmp_path, str(STUB))
    spec = importlib.util.spec_from_file_location("mcp_stub", STUB)
    stub = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stub)
    monkeypatch.setattr(stub, "POLICY", tmp_path / "no-policy.json")
    monkeypatch.setenv("E2E_RUN", str(run))
    monkeypatch.setattr("sys.argv", argv[1:])
    stub.main()
    entry = json.loads((run / "mcp.json").read_text())["mcpServers"]["git_broker"]
    assert entry["timeout"] == 5
    assert entry["env"]["K"] == "v"
    assert json.loads(entry["env"]["E2E_MCP_STUB_ARGS"]) == ["--x", "y"]


def test_main_reads_the_staged_home_and_names_the_agents(tmp_path, monkeypatch):
    """setup.sh hands main() the staged dir it can read; the servers in
    mcp.json are spawned inside the sandbox, where that dir is hidden and the
    agent's HOME (E2E_HOME, the real HOME's path) holds the same files."""
    staged = tmp_path / "run/home"
    (staged / ".claude").mkdir(parents=True)
    (staged / ".claude/mcp-servers.json").write_text(json.dumps(ARM))
    monkeypatch.setattr(reg, "POLICY_SERVERS", tmp_path / "absent")
    monkeypatch.setenv("E2E_HOME", "/home/agent")
    reg.main(tmp_path / "run", staged, str(STUB))
    entry = json.loads((tmp_path / "run/mcp.json").read_text())["mcpServers"]["github"]
    assert entry["args"][2] == "/home/agent/.claude/mcps/github-mux/server.py"
