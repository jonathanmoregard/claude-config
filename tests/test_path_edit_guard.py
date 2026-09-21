"""Contract for hooks/path-edit-guard.py.

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
HOOK = os.path.join(REPO_ROOT, "hooks", "path-edit-guard.py")
LIVE = os.path.join(os.path.expanduser("~"), ".claude")
CODEX = os.path.join(os.path.expanduser("~"), ".codex")


def test_guard_has_general_path_edit_name():
    general_hook = os.path.join(REPO_ROOT, "hooks", "path-edit-guard.py")
    legacy_hook = os.path.join(
        REPO_ROOT, "hooks", "dot-claude-config-write-guard.py"
    )
    settings_path = os.path.join(REPO_ROOT, "settings.json")

    assert os.path.isfile(general_hook)
    assert not os.path.exists(legacy_hook)
    with open(settings_path, encoding="utf-8") as settings_file:
        settings = settings_file.read()
    assert "$HOME/.claude/hooks/path-edit-guard.py" in settings
    assert "dot-claude-config-write-guard" not in settings


def run(payload, home=None):
    """Run the hook over `payload`; return (exit_code, parsed_stdout_or_None)."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    env = dict(os.environ)
    if home is not None:
        env["HOME"] = str(home)
    proc = subprocess.run(
        # -E only drops PYTHON* variables, so the fake $HOME the tracked-file
        # cases below rely on still reaches os.path.expanduser().
        [sys.executable, "-S", "-E", HOOK],
        input=text,
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
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


def assert_codex_denied(payload):
    code, out = run(payload)
    assert code == 0, "the hook decides via JSON, never via exit code"
    assert out is not None, "expected a decision, got nothing"
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    reason = hso["permissionDecisionReason"]
    assert "~/.claude" in reason
    assert "ai-client-config" in reason
    assert "ai-client-config-codex-sync.service" in reason
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
    "mcps/git-broker/server.py",
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


# --- live Codex tree is sync/runtime-owned, never agent-edited --------------


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_every_file_tool_refuses_codex_tree(tool):
    assert_codex_denied(
        write_payload(os.path.join(CODEX, "config.toml"), tool=tool)
    )


@pytest.mark.parametrize(
    "rel",
    [
        "config.toml",
        "AGENTS.md",
        "skills/x/SKILL.md",
        "auth.json",
        "sessions/x.jsonl",
    ],
)
def test_all_live_codex_paths_are_agent_read_only(rel):
    reason = assert_codex_denied(write_payload(os.path.join(CODEX, rel)))
    assert rel in reason


def test_codex_tilde_spelling_is_refused():
    assert_codex_denied(write_payload("~/.codex/config.toml"))


def test_codex_root_tracks_fake_home(tmp_path):
    target = tmp_path / ".codex" / "config.toml"
    code, out = run(write_payload(str(target)), home=tmp_path)
    assert code == 0
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_external_symlink_into_codex_tree_is_refused(tmp_path):
    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    alias = tmp_path / "codex-alias"
    alias.symlink_to(codex, target_is_directory=True)

    code, out = run(write_payload(str(alias / "config.toml")), home=home)

    assert code == 0
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_codex_lookalike_and_traversal_are_allowed():
    assert_allowed(
        write_payload(os.path.expanduser("~/.codex-backup/config.toml"))
    )
    assert_allowed(write_payload(os.path.join(CODEX, "..", "notes", "x.md")))


# --- everything else is left alone ------------------------------------------

ALLOWED_UNDER_LIVE = [
    "tmp/scratch.txt",
    "projects/-home-jonathan/memory/note.md",
    "homunculus/instincts/x.md",
    "reflections/sessions/abc/manifest.json",
    "proposals/2026-08-31-something.md",
    "permission-ledger/decided.json",
    "permission-ledger/queue/pending.jsonl",
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


# --- anything git TRACKS under ~/.claude is config, whatever it is named ----
#
# An allowlist of guarded directory names always lags the repo. Measured
# 2026-08-31 against this very branch: `permission-ledger/` (19 tracked files,
# including the `on-decision` callback the intake gate treats as a security
# boundary), `recursive-self-improvement/config/config.json` (the sink path's
# single source of truth), `tools/`, `workers/`, `egress_guard/`,
# `dev-container/` and `tests/` were all writable, none gitignored, every write
# dirtying the live checkout the harness reads.
#
# The rule is therefore inverted: git's index decides. These cases run against
# a throwaway $HOME with an index this suite controls — asserting them against
# the real ~/.claude would make the result depend on which commit that checkout
# happens to be sitting on (`permission-ledger/on-decision` is tracked on this
# branch and not yet on master).

TRACKED_BUT_NOT_IN_A_CONFIG_DIR = [
    "permission-ledger/on-decision",
    "permission-ledger/README.md",
    "recursive-self-improvement/config/config.json",
    "tools/ghostty-mcp/ghostty-cycle.sh",
    "workers/bin/run-worker.sh",
    "egress_guard/limiter.py",
    "dev-container/README.md",
    "tests/test_something.py",
    "inplace_reset.py",
]

# Tracked today on master, and still not the guard's business: the proposal
# spine is sibling-owned data written by the nightly reviewer by design.
EXEMPT_EVEN_THOUGH_TRACKED = [
    "proposals/2026-08-31-something.md",
    "proposals/permissions/2026-08-31-bash.md",
    "recursive-self-improvement/proposals/2026-08-31-x.md",
]

# Untracked runtime state beside those tracked files. Denying these would be
# over-denial: the ledger's own drain and the harness write them every day.
UNTRACKED_RUNTIME = [
    "permission-ledger/decided.json",
    "permission-ledger/queue/pending.jsonl",
    "projects/-home-jonathan/memory/note.md",
    "homunculus/instincts/x.md",
    "tmp/scratch.txt",
    "logs/review-agent.log",
    "reflections/sessions/abc/manifest.json",
]


@pytest.fixture(scope="module")
def tracked_home(tmp_path_factory):
    """A throwaway $HOME whose `.claude` is a real git checkout.

    Mirrors the live layout closely enough that the hook runs unmodified: the
    tracked paths are committed, the runtime ones are left untracked.
    """
    home = tmp_path_factory.mktemp("guard-home")
    root = home / ".claude"
    tracked = TRACKED_BUT_NOT_IN_A_CONFIG_DIR + EXEMPT_EVEN_THOUGH_TRACKED + [
        "settings.json",
        "hooks/existing-hook.py",
    ]
    for rel in tracked:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("tracked\n", encoding="utf-8")
    for rel in UNTRACKED_RUNTIME:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("runtime\n", encoding="utf-8")
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Guard Test", GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="Guard Test", GIT_COMMITTER_EMAIL="t@example.invalid",
    )

    def git(*args):
        proc = subprocess.run(("git", "-C", str(root)) + args, env=env,
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr

    git("init", "-q", "-b", "master")
    for rel in tracked:
        git("add", "--", rel)
    git("commit", "-q", "-m", "fixture")
    return home


@pytest.mark.parametrize("rel", TRACKED_BUT_NOT_IN_A_CONFIG_DIR)
def test_tracked_files_outside_the_config_dirs_are_refused(rel, tracked_home):
    code, out = run(write_payload(str(tracked_home / ".claude" / rel)),
                    home=tracked_home)
    assert code == 0
    assert out is not None, (
        "%s is tracked in the live checkout; writing it dirties the tree the "
        "harness reads and no guarded-directory name covers it" % rel)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert rel in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("rel", EXEMPT_EVEN_THOUGH_TRACKED)
def test_the_proposal_spine_stays_writable(rel, tracked_home):
    code, out = run(write_payload(str(tracked_home / ".claude" / rel)),
                    home=tracked_home)
    assert code == 0
    assert out is None, (
        "%s is sibling-owned proposal data the nightly reviewer writes by "
        "design; refusing it breaks the writer the guard was never aimed at"
        % rel)


@pytest.mark.parametrize("rel", UNTRACKED_RUNTIME)
def test_untracked_runtime_state_stays_writable(rel, tracked_home):
    code, out = run(write_payload(str(tracked_home / ".claude" / rel)),
                    home=tracked_home)
    assert code == 0
    assert out is None, "%s is untracked runtime state, not config" % rel


def test_a_new_file_in_a_config_dir_is_refused_even_though_untracked(tracked_home):
    """The tracked-file rule is a floor, not a replacement: creating a brand
    new hook in the live tree is exactly as live as editing an existing one."""
    code, out = run(
        write_payload(str(tracked_home / ".claude" / "hooks" / "brand-new.py")),
        home=tracked_home)
    assert code == 0
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_home_without_a_git_checkout_still_guards_the_config_dirs(tmp_path):
    """git may be absent, or ~/.claude may not be a checkout at all. The
    tracked-file rule then has nothing to say, and the guard must fall back to
    the directory rule rather than falling silent."""
    (tmp_path / ".claude" / "hooks").mkdir(parents=True)
    code, out = run(
        write_payload(str(tmp_path / ".claude" / "hooks" / "x.py")),
        home=tmp_path)
    assert code == 0
    assert out is not None
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


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
