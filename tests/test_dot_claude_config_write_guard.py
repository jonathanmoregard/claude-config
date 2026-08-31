"""Contract for hooks/dot-claude-config-write-guard.py.

The guard refuses Write/Edit/NotebookEdit calls aimed at the versioned config
surface of the LIVE ~/.claude checkout, and stays out of the way everywhere
else: worktrees, project-local .claude dirs, untracked runtime state under
~/.claude, and the sibling-owned data trees.

Every case runs the hook as a real subprocess over its real stdin contract, so
a change to the JSON shape it emits turns these red.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO_ROOT, "hooks", "dot-claude-config-write-guard.py")
LIVE = os.path.join(os.path.expanduser("~"), ".claude")


def run(payload):
    """Run the hook over `payload`; return (exit_code, parsed_stdout_or_None)."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    proc = subprocess.run(
        [sys.executable, "-S", "-E", HOOK],
        input=text,
        capture_output=True,
        text=True,
        timeout=20,
    )
    out = proc.stdout.strip()
    return proc.returncode, (json.loads(out) if out else None)


def write_payload(path, tool="Write"):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {key: path, "content": "x"},
    }


def assert_denied(payload):
    code, out = run(payload)
    assert code == 0, "the hook decides via JSON, never via exit code"
    assert out is not None, "expected a decision, got nothing"
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    reason = hso["permissionDecisionReason"]
    # The refusal has to carry the route out, not just the refusal.
    assert "worktree add" in reason
    assert "origin/master" in reason
    return reason


def assert_allowed(payload):
    code, out = run(payload)
    assert code == 0
    assert out is None, f"expected silence, got {out!r}"


# --- the config surface is refused ------------------------------------------

GUARDED = [
    "settings.json",
    "settings-patch.json",
    "keybindings.json",
    "mcp-servers.json",
    "CLAUDE.md",
    "hooks/some-hook.py",
    "skills/some-skill/SKILL.md",
    "agents/some-agent.md",
    "commands/some-command.md",
    "scripts/some-script.sh",
    "mcps/safe-bash/server.py",
    "templates/some-template.md",
    "bin/some-bin",
    ".githooks/pre-commit",
]


@pytest.mark.parametrize("rel", GUARDED)
def test_live_config_surface_is_refused(rel):
    reason = assert_denied(write_payload(os.path.join(LIVE, rel)))
    assert rel in reason, "the refusal names the file it refused"


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_every_file_tool_is_covered(tool):
    assert_denied(write_payload(os.path.join(LIVE, "settings.json"), tool=tool))


def test_tilde_spelling_is_refused():
    assert_denied(write_payload("~/.claude/settings.json"))


def test_symlinked_skill_entry_is_refused_and_redirected():
    """A skills/ entry symlinked into another repo must go through that repo."""
    reason = assert_denied(write_payload(os.path.join(LIVE, "skills/writing-skills/SKILL.md")))
    assert "symlink" in reason


# --- everything else is left alone ------------------------------------------

ALLOWED_UNDER_LIVE = [
    "tmp/scratch.txt",
    "projects/-home-jonathan/memory/note.md",
    "homunculus/instincts/x.md",
    "reflections/sessions/abc/manifest.json",
    "proposals/2026-08-31-something.md",
    "permission-ledger/README.md",
    "recursive-self-improvement/config/config.json",
    "logs/review-agent.log",
    "docs/notes.md",
]


@pytest.mark.parametrize("rel", ALLOWED_UNDER_LIVE)
def test_runtime_and_sibling_owned_trees_are_untouched(rel):
    assert_allowed(write_payload(os.path.join(LIVE, rel)))


def test_worktree_is_the_sanctioned_path():
    home = os.path.expanduser("~")
    assert_allowed(write_payload(os.path.join(home, "worktrees/claude-slug/settings.json")))
    assert_allowed(write_payload(os.path.join(home, "worktrees/claude-slug/skills/x/SKILL.md")))


def test_project_local_claude_dir_is_not_the_user_config():
    assert_allowed(write_payload("/home/jonathan/Repos/someproject/.claude/settings.json"))


def test_lookalike_sibling_directory_is_not_matched():
    assert_allowed(write_payload(os.path.expanduser("~/.claude-backup/settings.json")))


def test_path_traversal_out_of_the_config_root_is_not_matched():
    assert_allowed(write_payload(os.path.join(LIVE, "..", "notes", "settings.json")))


def test_non_file_tools_pass_through():
    assert_allowed(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": os.path.join(LIVE, "settings.json")},
        }
    )


# --- fails open, never closed, on malformed input ---------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "not json at all",
        "[]",
        json.dumps({"tool_name": "Write"}),
        json.dumps({"tool_name": "Write", "tool_input": "a string"}),
        json.dumps({"tool_name": "Write", "tool_input": {}}),
        json.dumps({"tool_name": "Write", "tool_input": {"file_path": None}}),
    ],
)
def test_malformed_input_exits_clean_and_silent(payload):
    code, out = run(payload)
    assert code == 0
    assert out is None
