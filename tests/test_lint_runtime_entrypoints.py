#!/usr/bin/env python3
"""A lint over the entrypoints the harness executes on its own initiative.

WHAT IT GUARDS. `~/.claude` is simultaneously the live harness config and this
repository, so `tests/` (229 tracked files, 46 scenario `prompt.md`), `docs/`,
`experiments/` and `research/` all sit inside the directory the agent reads.
Claude Code supplies no boundary there: Read/Edit deny rules cover the built-in
file tools and the Bash file commands the harness recognises, not arbitrary
subprocesses; and the sandbox covers shell commands only -- hooks, local MCP
servers and the file tools run outside it with the user's full access. A
`settings.json` hook command resolving under `tests/` would be fenced by
nothing at all.

Published evidence says that is the soft entry point rather than a theoretical
one. Across 21,454 agentic pull requests, test-related changes carried 12.2%
of agentic PR vulnerabilities against 2.2% for human ones, and 96.4% of the
vulnerable agentic PRs merged with no security discussion. The supply-chain
literature names "Test File Abuse" as a reviewer-fatigue technique, and the XZ
backdoor hid its payload in files named to look like test data.

WHAT THESE TESTS DO AND DO NOT ESTABLISH. The lint reads two structured files
and asks whether a declared path names a dev-only tree. These tests pin that
behaviour, in both directions, because both failure modes are silent:

  * staying quiet when an entrypoint does name a dev-only tree -- the whole
    reason the lint exists;
  * firing on the many legitimate mentions, which would get it switched off.

They establish nothing about whether dev-only code can be executed. It can:
anything that does not name the path in these two files passes untouched. The
boundary is the read-only bind mount that keeps those trees out of the agent's
namespace. Do not let a green run here be read as isolation.

Run:  nix shell nixpkgs#python3Packages.pytest -c pytest tests/test_lint_runtime_entrypoints.py -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LINT = REPO_ROOT / "scripts" / "lint-runtime-entrypoints.py"


def make_repo(root: Path, files: dict[str, str]) -> Path:
    """A throwaway config repo."""
    for rel, body in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def run_lint(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(LINT), "--repo-root", str(root)],
        capture_output=True, text=True,
    )


def settings(*commands: str, status_line: str | None = None) -> str:
    """A settings.json whose PreToolUse hooks run `commands`."""
    data: dict = {
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": c}
                                              for c in commands]}
            ]
        }
    }
    if status_line is not None:
        data["statusLine"] = {"type": "command", "command": status_line}
    return json.dumps(data, indent=2)


# ---------------------------------------------------------------- the clean case

def test_clean_repo_passes(tmp_path):
    root = make_repo(tmp_path, {
        "settings.json": settings("python3 $HOME/.claude/hooks/guard.py"),
        "hooks/guard.py": "import sys\nsys.exit(0)\n",
        "tests/test_guard.py": "def test_x():\n    assert True\n",
    })
    proc = run_lint(root)
    assert proc.returncode == 0, proc.stderr


def test_the_live_repo_passes():
    """The lint must be green on the tree it ships in, or it cannot be wired up."""
    proc = run_lint(REPO_ROOT)
    assert proc.returncode == 0, proc.stderr


def test_not_a_config_repo_is_a_usage_error(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(LINT), "--repo-root", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2


# ------------------------------------------------------- A: settings.json hooks

def test_hook_running_a_test_script_fails(tmp_path):
    root = make_repo(tmp_path, {
        "settings.json": settings("bash $HOME/.claude/tests/smoke.sh"),
        "tests/smoke.sh": "echo hi\n",
    })
    proc = run_lint(root)
    assert proc.returncode == 1
    assert "tests/smoke.sh" in proc.stderr


def test_hook_with_absolute_config_path_fails(tmp_path):
    root = make_repo(tmp_path, {
        "settings.json": settings("bash /home/someone/.claude/tests/smoke.sh"),
        "tests/smoke.sh": "echo hi\n",
    })
    assert run_lint(root).returncode == 1


def test_hook_with_tilde_config_path_fails(tmp_path):
    root = make_repo(tmp_path, {
        "settings.json": settings("bash ~/.claude/tests/smoke.sh"),
        "tests/smoke.sh": "echo hi\n",
    })
    assert run_lint(root).returncode == 1


def test_statusline_command_is_checked(tmp_path):
    root = make_repo(tmp_path, {
        "settings.json": settings(
            "python3 $HOME/.claude/hooks/ok.py",
            status_line="bash $HOME/.claude/tests/fake-statusline.sh",
        ),
        "hooks/ok.py": "pass\n",
        "tests/fake-statusline.sh": "echo hi\n",
    })
    proc = run_lint(root)
    assert proc.returncode == 1
    assert "statusLine" in proc.stderr


def test_compound_hook_command_is_checked(tmp_path):
    """The dev-only path is the second of two commands, not the program."""
    root = make_repo(tmp_path, {
        "settings.json": settings(
            '[ -f /tmp/x ] || exit 0; bash "$HOME/.claude/tests/helper.sh" --flag'
        ),
        "tests/helper.sh": "echo hi\n",
    })
    assert run_lint(root).returncode == 1


def test_broken_settings_json_is_reported_not_crashed(tmp_path):
    root = make_repo(tmp_path, {"settings.json": "{not json"})
    proc = run_lint(root)
    assert proc.returncode == 1
    assert "invalid JSON" in proc.stderr


# ---------------------------------------------------------- B: mcp-servers.json

def test_mcp_server_running_test_code_fails(tmp_path):
    root = make_repo(tmp_path, {
        "settings.json": settings("python3 $HOME/.claude/hooks/ok.py"),
        "hooks/ok.py": "pass\n",
        "mcp-servers.json": json.dumps({"mcpServers": {"broker": {
            "command": "python3",
            "args": ["$HOME/.claude/tests/agent-e2e/broker.py"],
        }}}, indent=2),
        "tests/agent-e2e/broker.py": "pass\n",
    })
    proc = run_lint(root)
    assert proc.returncode == 1
    assert "broker" in proc.stderr


def test_registered_runtime_mcp_server_passes(tmp_path):
    root = make_repo(tmp_path, {
        "settings.json": settings("python3 $HOME/.claude/hooks/ok.py"),
        "hooks/ok.py": "pass\n",
        "mcp-servers.json": json.dumps({"mcpServers": {"research": {
            "command": "python3",
            "args": ["$HOME/.claude/mcps/research-agent/server.py"],
        }}}, indent=2),
        "mcps/research-agent/server.py": "pass\n",
    })
    assert run_lint(root).returncode == 0


# ------------------------------------------------------------------ normalisation
#
# These are the ones that matter most, and the ones the first revision got
# wrong: `hooks/../tests/x.sh` and `./tests/x.sh` name exactly the file that
# `tests/x.sh` names, and a check comparing the first path segment literally
# sees three unrelated strings. A reviewer found the `..` spelling scanning
# clean straight through check A.

@pytest.mark.parametrize("command", [
    'bash $HOME/.claude/hooks/../tests/x.sh',
    'bash $HOME/.claude/./tests/x.sh',
    'bash $HOME/.claude/tests/../tests/x.sh',
    'bash $HOME/.claude/docs/../experiments/x.sh',
    'bash /home/someone/.claude/hooks/../tests/x.sh',
    'bash ~/.claude/hooks/../tests/x.sh',
])
def test_a_denormalised_hook_path_still_fails(tmp_path, command):
    root = make_repo(tmp_path, {
        "settings.json": settings(command),
        "tests/x.sh": "echo hi\n",
        "experiments/x.sh": "echo hi\n",
    })
    assert run_lint(root).returncode == 1, command


def test_a_denormalised_mcp_arg_still_fails(tmp_path):
    root = make_repo(tmp_path, {
        "settings.json": settings("python3 $HOME/.claude/hooks/ok.py"),
        "hooks/ok.py": "pass\n",
        "mcp-servers.json": json.dumps({"mcpServers": {"broker": {
            "command": "python3",
            "args": ["$HOME/.claude/mcps/../tests/broker.py"],
        }}}, indent=2),
        "tests/broker.py": "pass\n",
    })
    assert run_lint(root).returncode == 1


def test_a_path_climbing_out_of_the_config_dir_is_not_ours(tmp_path):
    """`$HOME/.claude/../tests` is some other tree, and not this lint's business."""
    root = make_repo(tmp_path, {
        "settings.json": settings("bash $HOME/.claude/../tests/x.sh"),
    })
    assert run_lint(root).returncode == 0


# ------------------------------------------------------------- scope boundaries

@pytest.mark.parametrize("tree", ["tests", "docs", "experiments", "research"])
def test_every_dev_tree_is_covered_not_just_tests(tmp_path, tree):
    root = make_repo(tmp_path, {
        "settings.json": settings(f"bash $HOME/.claude/{tree}/x.sh"),
        f"{tree}/x.sh": "echo hi\n",
    })
    assert run_lint(root).returncode == 1, tree


def test_githooks_remain_legal(tmp_path):
    """git executes `.githooks/`, by design. A different lane."""
    root = make_repo(tmp_path, {
        "settings.json": settings("bash $HOME/.claude/.githooks/pre-push"),
        ".githooks/pre-push": "exit 0\n",
    })
    assert run_lint(root).returncode == 0


# ------------------------------------------------ regressions from the live tree

def test_bare_dev_word_is_not_a_path(tmp_path):
    """`--tools docs docs_preview` names tools. The live mcp-servers.json does this."""
    root = make_repo(tmp_path, {
        "settings.json": settings("python3 $HOME/.claude/hooks/ok.py"),
        "hooks/ok.py": "pass\n",
        "mcp-servers.json": json.dumps({"mcpServers": {"gdocs": {
            "command": "python3",
            "args": ["$HOME/.claude/mcps/gdocs/server.py",
                     "--tools", "docs", "docs_preview"],
        }}}, indent=2),
        "mcps/gdocs/server.py": "pass\n",
    })
    assert run_lint(root).returncode == 0
