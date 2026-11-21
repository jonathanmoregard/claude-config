"""The one fixed dev-MCP command: DEV_MCP_PROFILE picks an entry of a fixed table."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
DISPATCH = ROOT / "runtime/scripts/dev-mcp/dispatch.py"
LIB = ROOT / "tests/agent-e2e/_lib/mcp_stub.py"
OURS = ("DEV_MCP_PROFILE", "DEV_MCP_REPO", "DEV_MCP_TOOLS",
        "E2E_RUN", "E2E_MCP_STUB", "E2E_MCP_STUB_ARGS")

ECHO = "import json, sys\nprint(json.dumps(sys.argv))\n"
INIT_STUB = """\
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": msg["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "stub", "version": "0"}}}) + "\\n")
        sys.stdout.flush()
"""
# The interpreter constant swapped for this one, so an accepted exec runs on
# hosts without /run/current-system; everything else is the real main().
PATCHED_MAIN = (
    "import importlib.util, sys\n"
    "spec = importlib.util.spec_from_file_location('dispatch', sys.argv[1])\n"
    "mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)\n"
    "mod.PYTHON = sys.executable\n"
    "mod.main()\n")


def load():
    spec = importlib.util.spec_from_file_location("dev_mcp_dispatch", DISPATCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def child_env(**env):
    base = {k: v for k, v in os.environ.items() if k not in OURS}
    return {**base, **{k: str(v) for k, v in env.items() if v is not None}}


def dispatch(patched=False, input=None, **env):
    argv = ([sys.executable, "-c", PATCHED_MAIN, str(DISPATCH)] if patched
            else [sys.executable, str(DISPATCH)])
    return subprocess.run(argv, env=child_env(**env), input=input,
                          capture_output=True, text=True, timeout=30)


def refused(out, needle):
    assert out.returncode != 0
    assert out.stdout == ""
    assert needle in out.stderr


@pytest.fixture
def run(tmp_path):
    d = tmp_path / "run"
    (d / "bin").mkdir(parents=True)
    return d


def stub_env(run, stub, args=None):
    return {"DEV_MCP_PROFILE": "e2e-stub", "E2E_RUN": run, "E2E_MCP_STUB": stub,
            "E2E_MCP_STUB_ARGS": args}


# --- profile selection -------------------------------------------------------

def test_refuses_without_a_profile():
    refused(dispatch(), "DEV_MCP_PROFILE")


def test_refuses_an_unknown_profile():
    refused(dispatch(DEV_MCP_PROFILE="anything"), "unknown profile")


def test_the_profile_table_is_fixed():
    assert sorted(load().PROFILES) == ["e2e-stub", "llmux"]


# --- e2e-stub ----------------------------------------------------------------

def test_e2e_stub_execs_the_fixed_interpreter_on_the_target(run):
    stub = run / "bin" / "s"
    stub.write_text(ECHO)
    argv = load().resolve(stub_env(run, stub, json.dumps(["--config", "a b.json"])))
    assert argv == ["/run/current-system/sw/bin/python3", str(stub.resolve()),
                    "--config", "a b.json"]


def test_e2e_stub_runs_a_stub_inside_the_run_dir(run):
    stub = run / "bin" / "s"
    stub.write_text(ECHO)
    out = dispatch(patched=True, **stub_env(run, stub, json.dumps(["-x", "y"])))
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)[1:] == ["-x", "y"]


def test_e2e_stub_stdio_round_trips_an_mcp_initialize(run):
    stub = run / "bin" / "init-stub"
    stub.write_text(INIT_STUB)
    req = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                      "clientInfo": {"name": "t", "version": "0"}}}
    out = dispatch(patched=True, input=json.dumps(req) + "\n", **stub_env(run, stub))
    assert out.returncode == 0, out.stderr
    reply = json.loads(out.stdout.splitlines()[0])
    assert reply["id"] == 1
    assert reply["result"]["serverInfo"]["name"] == "stub"


def test_e2e_stub_refuses_a_target_outside_the_run_dir(run, tmp_path):
    (tmp_path / "elsewhere.py").write_text(ECHO)
    refused(dispatch(**stub_env(run, tmp_path / "elsewhere.py")), "outside")


def test_e2e_stub_refuses_a_dotdot_escape(run, tmp_path):
    (tmp_path / "elsewhere.py").write_text(ECHO)
    refused(dispatch(**stub_env(run, run / "bin" / ".." / ".." / "elsewhere.py")), "outside")


def test_e2e_stub_refuses_a_symlink_leading_out_of_the_run_dir(run, tmp_path):
    real = tmp_path / "elsewhere.py"
    real.write_text(ECHO)
    (run / "bin" / "link").symlink_to(real)
    refused(dispatch(**stub_env(run, run / "bin" / "link")), "outside")


def test_e2e_stub_refuses_a_sibling_sharing_the_run_dir_prefix(run, tmp_path):
    sibling = tmp_path / "run-other"
    sibling.mkdir()
    (sibling / "s.py").write_text(ECHO)
    refused(dispatch(**stub_env(run, sibling / "s.py")), "outside")


def test_e2e_stub_refuses_a_missing_target(run):
    refused(dispatch(**stub_env(run, run / "bin" / "nope")), "not a file")


def test_e2e_stub_refuses_a_directory_target(run):
    refused(dispatch(**stub_env(run, run / "bin")), "not a file")


def test_e2e_stub_refuses_when_no_target_is_named(run):
    refused(dispatch(**stub_env(run, None)), "E2E_MCP_STUB")


def test_e2e_stub_refuses_when_no_run_dir_is_set(run):
    (run / "bin" / "s").write_text(ECHO)
    refused(dispatch(**stub_env(None, run / "bin" / "s")), "E2E_RUN")


@pytest.mark.parametrize("bad", ["not json", '"--flag"', "[1, 2]", '{"a": 1}'])
def test_e2e_stub_refuses_args_that_are_not_a_json_list_of_strings(run, bad):
    (run / "bin" / "s").write_text(ECHO)
    refused(dispatch(**stub_env(run, run / "bin" / "s", bad)), "E2E_MCP_STUB_ARGS")


# --- llmux -------------------------------------------------------------------

def fake_gdocs(root: pathlib.Path) -> pathlib.Path:
    repo = root / "gdocs-review-mcp"
    (repo / ".git").mkdir(parents=True)
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("")
    (repo / "mockdocs").mkdir()
    (repo / "mockdocs" / "serve.py").write_text("")
    return repo


def add_worktree(repo: pathlib.Path, wt: pathlib.Path, gitdir_line=None) -> pathlib.Path:
    admin = repo / ".git" / "worktrees" / wt.name
    admin.mkdir(parents=True)
    (admin / "commondir").write_text("../..\n")
    (wt / ".venv" / "bin").mkdir(parents=True)
    (wt / ".venv" / "bin" / "python").write_text("")
    (wt / "mockdocs").mkdir()
    (wt / "mockdocs" / "serve.py").write_text("")
    (wt / ".git").write_text(f"gitdir: {gitdir_line or admin}\n")
    return wt


@pytest.fixture
def llmux(tmp_path, monkeypatch):
    mod = load()
    repo = fake_gdocs(tmp_path)
    monkeypatch.setattr(mod, "GDOCS_REPO", str(repo))
    return mod, repo


def llmux_env(repo, tools="docs docs_preview"):
    return {"DEV_MCP_PROFILE": "llmux", "DEV_MCP_REPO": str(repo), "DEV_MCP_TOOLS": tools}


def expected(repo, *tools):
    return [f"{repo}/.venv/bin/python", f"{repo}/mockdocs/serve.py",
            "--transport", "stdio", "--single-user", "--tools", *tools]


def test_llmux_execs_the_mockdocs_server_of_the_repo(llmux):
    mod, repo = llmux
    assert mod.resolve(llmux_env(repo)) == expected(repo, "docs", "docs_preview")


def test_llmux_takes_tools_as_a_json_list(llmux):
    mod, repo = llmux
    assert mod.resolve(llmux_env(repo, '["docs"]')) == expected(repo, "docs")


def test_llmux_accepts_a_symlink_to_the_repo(llmux, tmp_path):
    mod, repo = llmux
    link = tmp_path / "link"
    link.symlink_to(repo)
    assert mod.resolve(llmux_env(link)) == expected(repo, "docs", "docs_preview")


def test_llmux_accepts_a_worktree_of_the_repo(llmux, tmp_path):
    mod, repo = llmux
    wt = add_worktree(repo, tmp_path / "wt")
    assert mod.resolve(llmux_env(wt)) == expected(wt, "docs", "docs_preview")


def test_llmux_accepts_a_worktree_with_a_relative_gitdir(llmux, tmp_path):
    mod, repo = llmux
    wt = tmp_path / "wt"
    add_worktree(repo, wt, gitdir_line="../gdocs-review-mcp/.git/worktrees/wt")
    assert mod.resolve(llmux_env(wt)) == expected(wt, "docs", "docs_preview")


def test_llmux_refuses_another_repo(llmux, tmp_path):
    mod, _ = llmux
    other = fake_gdocs(tmp_path / "elsewhere")
    with pytest.raises(mod.Refused, match="not"):
        mod.resolve(llmux_env(other))


def test_llmux_refuses_a_worktree_of_another_repo(llmux, tmp_path):
    mod, _ = llmux
    other = fake_gdocs(tmp_path / "elsewhere")
    wt = add_worktree(other, tmp_path / "wt")
    with pytest.raises(mod.Refused, match="not"):
        mod.resolve(llmux_env(wt))


def test_llmux_refuses_a_subdirectory_of_the_repo(llmux):
    mod, repo = llmux
    with pytest.raises(mod.Refused):
        mod.resolve(llmux_env(repo / "mockdocs"))


def test_llmux_refuses_without_a_repo(llmux):
    mod, _ = llmux
    with pytest.raises(mod.Refused, match="DEV_MCP_REPO"):
        mod.resolve({"DEV_MCP_PROFILE": "llmux", "DEV_MCP_TOOLS": "docs"})


@pytest.mark.parametrize("tools", ["", "  ", "[]", "--evil", "docs --evil", '["a b"]',
                                   "[1]", "{}", "docs;rm"])
def test_llmux_refuses_tools_that_are_not_plain_names(llmux, tools):
    mod, repo = llmux
    with pytest.raises(mod.Refused, match="DEV_MCP_TOOLS"):
        mod.resolve(llmux_env(repo, tools))


def test_llmux_refuses_a_repo_without_its_venv(llmux):
    mod, repo = llmux
    (repo / ".venv" / "bin" / "python").unlink()
    with pytest.raises(mod.Refused, match="python"):
        mod.resolve(llmux_env(repo))


def test_llmux_production_constant_is_the_real_checkout():
    assert load().GDOCS_REPO == "/home/jonathan/Repos/gdocs-review-mcp"


def test_llmux_refusal_reaches_stderr_and_exit_code(tmp_path):
    other = fake_gdocs(tmp_path)
    refused(dispatch(**llmux_env(other)), "DEV_MCP_REPO")


# --- the scenario helper -----------------------------------------------------

def write_entry(run, *argv, served=None):
    env = {**os.environ, "E2E_RUN": str(run)}
    if served is not None:
        env["DEV_MCP_DISPATCH_SERVED"] = str(served)
    out = subprocess.run([sys.executable, str(LIB), *argv], env=env,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads((run / "mcp.json").read_text())["mcpServers"]


def test_helper_writes_the_fixed_dispatcher_entry(run, tmp_path):
    served = tmp_path / "served" / "dispatch.py"
    served.parent.mkdir()
    served.write_text("")
    stub = run / "bin" / "s"
    entry = write_entry(run, "vault", str(stub), "--", "-x", "y", served=served)["vault"]
    assert entry["type"] == "stdio"
    assert entry["command"] == "/run/current-system/sw/bin/python3"
    assert entry["args"] == [str(served)]
    assert entry["env"]["DEV_MCP_PROFILE"] == "e2e-stub"
    assert entry["env"]["E2E_RUN"] == str(run)
    assert entry["env"]["E2E_MCP_STUB"] == str(stub)
    assert json.loads(entry["env"]["E2E_MCP_STUB_ARGS"]) == ["-x", "y"]


def test_helper_falls_back_to_this_checkouts_dispatcher(run, tmp_path):
    servers = write_entry(run, "vault", str(run / "bin" / "s"), served=tmp_path / "absent.py")
    assert servers["vault"]["args"] == [str(DISPATCH)]


def test_helper_merges_extra_env_and_keeps_earlier_servers(run, tmp_path):
    absent = tmp_path / "absent.py"
    write_entry(run, "a", str(run / "bin" / "a"), served=absent)
    servers = write_entry(run, "b", str(run / "bin" / "b"), "--env", "K=v=w", served=absent)
    assert set(servers) == {"a", "b"}
    assert servers["b"]["env"]["K"] == "v=w"
    assert "E2E_MCP_STUB_ARGS" not in servers["b"]["env"]
    assert "timeout" not in servers["b"]


def test_helper_sets_a_timeout(run, tmp_path):
    servers = write_entry(run, "a", str(run / "bin" / "a"), "--timeout", "600000",
                          served=tmp_path / "absent.py")
    assert servers["a"]["timeout"] == 600000
