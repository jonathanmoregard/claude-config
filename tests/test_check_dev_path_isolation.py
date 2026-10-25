#!/usr/bin/env python3
"""The gate that keeps dev-only code off the agent's execution path.

WHAT THIS GUARDS. `~/.claude` is simultaneously the live harness config and
this repository, so `tests/` (229 tracked files, 46 scenario `prompt.md`, 3
committed executable stubs), `docs/`, `experiments/` and `research/` all sit
inside the directory the agent reads. Claude Code supplies no boundary there,
and this is the load-bearing detail: Read/Edit deny rules cover the built-in
file tools and the Bash file commands the harness recognises, not arbitrary
subprocesses; and the sandbox covers shell commands only -- hooks, local MCP
servers and the file tools run outside it with the user's full access. A
`settings.json` hook command resolving under `tests/` would therefore be fenced
by nothing at all.

Published evidence says that is the soft entry point rather than a theoretical
one. Across 21,454 agentic pull requests, test-related changes carried 12.2%
of agentic PR vulnerabilities against 2.2% for human ones, and 96.4% of the
vulnerable agentic PRs merged with no security discussion. The supply-chain
literature names "Test File Abuse" as a reviewer-fatigue technique, and the XZ
backdoor hid its payload in files named to look like test data.

So the gate's two failure modes are opposites, and both are silent:

  * staying quiet when runtime config does reach a dev-only tree -- the whole
    reason it exists. A gate that only ever passes is worth nothing, so every
    violation shape below is asserted to actually fail, not merely to run.
  * speaking when nothing is wrong -- a gate crying wolf gets switched off, and
    this repo has 13 legitimate `tests/...` references sitting in comments and
    docstrings (`hooks/bash-guard.py:82`, `hooks/deny-hints.py:32`,
    `hooks/path-edit-guard.py:19`, `self-compact-trigger.py:158`, ...). Prose
    pointing at a test is correct practice and must stay legal, which is why
    check C reads Python through `ast` and only flags strings handed to
    exec-ish calls.

Every violation case below was confirmed to depend on the gate's logic, not on
an incidental error, by neutralising `DEV_ONLY` and observing each one stop
failing.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE = REPO_ROOT / "scripts" / "check-dev-path-isolation.py"


def make_repo(root: Path, files: dict[str, str]) -> Path:
    """A throwaway config repo. The gate reads `git ls-files`, so files are added."""
    for rel, body in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def run_gate(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE), "--repo-root", str(root)],
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
    proc = run_gate(root)
    assert proc.returncode == 0, proc.stderr


def test_the_live_repo_passes():
    """The gate must be green on the tree it ships in, or it cannot be wired up."""
    proc = run_gate(REPO_ROOT)
    assert proc.returncode == 0, proc.stderr


# ------------------------------------------------------- A: settings.json hooks

def test_hook_running_a_test_script_fails(tmp_path):
    root = make_repo(tmp_path, {
        "settings.json": settings("bash $HOME/.claude/tests/smoke.sh"),
        "tests/smoke.sh": "echo hi\n",
    })
    proc = run_gate(root)
    assert proc.returncode == 1
    assert "tests/smoke.sh" in proc.stderr
    assert "outside the sandbox" in proc.stderr


def test_hook_with_absolute_config_path_fails(tmp_path):
    """The real settings.json uses a literal /home/jonathan/.claude/... hook."""
    root = make_repo(tmp_path, {
        "settings.json": settings("/home/someone/.claude/tests/agent-e2e/_lib/gh"),
        "tests/agent-e2e/_lib/gh": "#!/bin/sh\n",
    })
    proc = run_gate(root)
    assert proc.returncode == 1
    assert "tests/agent-e2e/_lib/gh" in proc.stderr


def test_hook_with_tilde_config_path_fails(tmp_path):
    root = make_repo(tmp_path, {
        "settings.json": settings("bash ~/.claude/tests/smoke.sh"),
        "tests/smoke.sh": "echo hi\n",
    })
    assert run_gate(root).returncode == 1


def test_statusline_command_is_checked(tmp_path):
    root = make_repo(tmp_path, {
        "settings.json": settings(
            "python3 $HOME/.claude/hooks/ok.py",
            status_line="bash $HOME/.claude/tests/statusline.sh",
        ),
        "hooks/ok.py": "pass\n",
        "tests/statusline.sh": "echo x\n",
    })
    proc = run_gate(root)
    assert proc.returncode == 1
    assert "statusLine" in proc.stderr


def test_compound_hook_command_is_checked(tmp_path):
    """Hooks in this repo are frequently `guard || exit 0; real-command`."""
    root = make_repo(tmp_path, {
        "settings.json": settings(
            '[ "${CLAUDE_CODE_ENTRYPOINT:-cli}" = "cli" ] || exit 0; '
            "bash $HOME/.claude/tests/smoke.sh"
        ),
        "tests/smoke.sh": "echo hi\n",
    })
    assert run_gate(root).returncode == 1


def test_broken_settings_json_is_reported_not_crashed(tmp_path):
    root = make_repo(tmp_path, {"settings.json": "{not json"})
    proc = run_gate(root)
    assert proc.returncode == 1
    assert "invalid JSON" in proc.stderr


# ------------------------------------------------------ B: mcp-servers.json

def test_mcp_server_running_test_code_fails(tmp_path):
    root = make_repo(tmp_path, {
        "settings.json": settings("python3 $HOME/.claude/hooks/ok.py"),
        "hooks/ok.py": "pass\n",
        "mcp-servers.json": json.dumps({"mcpServers": {
            "e2e-broker": {
                "command": "python3",
                "args": ["$HOME/.claude/tests/agent-e2e/broker.py"],
            }
        }}, indent=2),
        "tests/agent-e2e/broker.py": "pass\n",
    })
    proc = run_gate(root)
    assert proc.returncode == 1
    assert "e2e-broker" in proc.stderr
    assert "full user access" in proc.stderr


def test_registered_runtime_mcp_server_passes(tmp_path):
    """The dormant broker under mcps/ is legal; only a tests/ path is not."""
    root = make_repo(tmp_path, {
        "settings.json": settings("python3 $HOME/.claude/hooks/ok.py"),
        "hooks/ok.py": "pass\n",
        "mcp-servers.json": json.dumps({"mcpServers": {
            "git_broker": {
                "command": "python3",
                "args": ["/home/someone/.claude/mcps/git-broker/server.py"],
            }
        }}, indent=2),
        "mcps/git-broker/server.py": "pass\n",
    })
    assert run_gate(root).returncode == 0


# ----------------------------------------------- C: runtime Python and shell

def test_runtime_python_executing_test_code_fails(tmp_path):
    root = make_repo(tmp_path, {
        "settings.json": settings("python3 $HOME/.claude/hooks/guard.py"),
        "hooks/guard.py": (
            "import subprocess\n"
            "subprocess.run(['bash', 'tests/smoke.sh'], check=True)\n"
        ),
        "tests/smoke.sh": "echo hi\n",
    })
    proc = run_gate(root)
    assert proc.returncode == 1
    assert "tests/smoke.sh" in proc.stderr


def test_runtime_python_importing_from_tests_fails(tmp_path):
    root = make_repo(tmp_path, {
        "settings.json": settings("python3 $HOME/.claude/hooks/guard.py"),
        "hooks/guard.py": "from tests.helpers import stub\n",
        "tests/helpers.py": "stub = 1\n",
    })
    proc = run_gate(root)
    assert proc.returncode == 1
    assert "imports from dev-only tree" in proc.stderr


def test_docstring_pointing_at_a_test_is_legal(tmp_path):
    """13 real references in this repo are exactly this shape. They must pass."""
    root = make_repo(tmp_path, {
        "settings.json": settings("python3 $HOME/.claude/hooks/guard.py"),
        "hooks/guard.py": (
            '"""A guard.\n\n'
            "``tests/test_bash_guards.py`` asserts that settings.json still\n"
            "declares this hook, and tests/test_deny_globs.py pins the matcher.\n"
            '"""\n'
            "# See also tests/smoke.sh for the live lane.\n"
            "import sys\n"
            "sys.exit(0)\n"
        ),
        "tests/test_bash_guards.py": "def test_x():\n    assert True\n",
    })
    proc = run_gate(root)
    assert proc.returncode == 0, proc.stderr


def test_runtime_shell_sourcing_test_lib_fails(tmp_path):
    root = make_repo(tmp_path, {
        "settings.json": settings("bash $HOME/.claude/hooks/guard.sh"),
        "hooks/guard.sh": "source tests/agent-e2e/_lib/checks.sh\n",
        "tests/agent-e2e/_lib/checks.sh": "ok() { :; }\n",
    })
    proc = run_gate(root)
    assert proc.returncode == 1
    assert "checks.sh" in proc.stderr


def test_runtime_shell_invoking_a_stub_fails(tmp_path):
    root = make_repo(tmp_path, {
        "settings.json": settings("bash $HOME/.claude/hooks/guard.sh"),
        "hooks/guard.sh": 'exec tests/stubs/claude "$@"\n',
        "tests/stubs/claude": "#!/bin/sh\n",
    })
    assert run_gate(root).returncode == 1


def test_shell_comment_mentioning_a_test_is_legal(tmp_path):
    root = make_repo(tmp_path, {
        "settings.json": settings("bash $HOME/.claude/hooks/guard.sh"),
        "hooks/guard.sh": (
            "#!/usr/bin/env bash\n"
            "# tests/worktree-policy.sh covers the refusal path.\n"
            "echo ok   # see tests/smoke.sh too\n"
        ),
    })
    proc = run_gate(root)
    assert proc.returncode == 0, proc.stderr


def test_quoted_hash_is_not_treated_as_a_comment(tmp_path):
    """Stripping comments must respect quotes, or a real exec hides behind one."""
    root = make_repo(tmp_path, {
        "settings.json": settings("bash $HOME/.claude/hooks/guard.sh"),
        "hooks/guard.sh": 'echo "a # b"; bash tests/smoke.sh\n',
        "tests/smoke.sh": "echo hi\n",
    })
    assert run_gate(root).returncode == 1


# ------------------------------------------------------------- scope boundaries

def test_files_inside_the_dev_trees_are_exempt(tmp_path):
    """Tests may run tests. The gate constrains runtime, not the suite."""
    root = make_repo(tmp_path, {
        "settings.json": settings("python3 $HOME/.claude/hooks/ok.py"),
        "hooks/ok.py": "pass\n",
        "tests/smoke.sh": "bash tests/agent-e2e/_lib/checks.sh\nexec tests/stubs/claude\n",
        "tests/runner.py": (
            "import subprocess\n"
            "subprocess.run(['bash', 'tests/smoke.sh'])\n"
        ),
        "tests/agent-e2e/_lib/checks.sh": "ok() { :; }\n",
        "tests/stubs/claude": "#!/bin/sh\n",
    })
    proc = run_gate(root)
    assert proc.returncode == 0, proc.stderr


def test_untracked_violation_is_not_reported(tmp_path):
    """The gate reviews what is committed; `git ls-files` is the boundary."""
    root = make_repo(tmp_path, {
        "settings.json": settings("python3 $HOME/.claude/hooks/ok.py"),
        "hooks/ok.py": "pass\n",
    })
    (root / "hooks" / "sneaky.sh").write_text("bash tests/smoke.sh\n")
    assert run_gate(root).returncode == 0


@pytest.mark.parametrize("tree", ["docs", "experiments", "research"])
def test_every_dev_tree_is_covered_not_just_tests(tmp_path, tree):
    root = make_repo(tmp_path, {
        "settings.json": settings(f"bash $HOME/.claude/{tree}/scratch.sh"),
        f"{tree}/scratch.sh": "echo hi\n",
    })
    proc = run_gate(root)
    assert proc.returncode == 1, f"{tree}/ not covered"
    assert tree in proc.stderr


def test_githooks_remain_legal(tmp_path):
    """git executes .githooks by design -- a different lane from agent runtime."""
    root = make_repo(tmp_path, {
        "settings.json": settings("python3 $HOME/.claude/hooks/ok.py"),
        "hooks/ok.py": "pass\n",
        ".githooks/pre-push": "#!/usr/bin/env bash\nexec .githooks/_forward\n",
        ".githooks/_forward": "#!/bin/sh\n",
    })
    assert run_gate(root).returncode == 0


def test_not_a_config_repo_is_a_usage_error(tmp_path):
    proc = run_gate(tmp_path / "nowhere")
    assert proc.returncode == 2


# ------------------------------------------------ regressions from the live tree
# Both shapes below were reported as violations by the first version of the
# gate, against the real repo. Neither is one.

def test_bare_dev_word_is_not_a_path(tmp_path):
    """`--tools docs docs_preview` names gdocs-review's tools, not a directory."""
    root = make_repo(tmp_path, {
        "settings.json": settings("python3 $HOME/.claude/hooks/ok.py"),
        "hooks/ok.py": "pass\n",
        "mcp-servers.json": json.dumps({"mcpServers": {
            "gdocs-review": {
                "command": "uv",
                "args": ["run", "--directory", "/home/someone/Repos/gdocs-review-mcp",
                         "python", "main.py", "--tools", "docs", "docs_preview"],
            }
        }}, indent=2),
    })
    proc = run_gate(root)
    assert proc.returncode == 0, proc.stderr


def test_cwd_into_another_repos_tests_is_legal(tmp_path):
    """mcps/klaffat-app-runner runs `npm ci` in the klaffat app's own tests/e2e."""
    root = make_repo(tmp_path, {
        "settings.json": settings("python3 $HOME/.claude/hooks/ok.py"),
        "hooks/ok.py": "pass\n",
        "mcps/runner/deps.py": (
            "import subprocess\n"
            "from pathlib import Path\n"
            "print('installing tests/e2e packages')\n"
            "subprocess.run(['npm', 'ci'], cwd=Path.cwd() / 'tests' / 'e2e')\n"
        ),
    })
    proc = run_gate(root)
    assert proc.returncode == 0, proc.stderr
