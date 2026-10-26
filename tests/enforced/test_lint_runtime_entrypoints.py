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
property the restructure aims at is narrower than it sounds -- the dev trees
are not among the paths projected into `~/.claude`, so nothing the harness
loads on its own initiative reaches them, while the repository itself stays
readable at its new location by anyone who names it. Do not let a green run
here be read as isolation.

WHERE THIS LIVES. `tests/enforced/` is the directory the required `checks` job
runs whole, so a suite guarding this repository's own guards is covered by
being here rather than by being named in the workflow. A suite added here is
enforced from its first commit; one added elsewhere is not run by CI at all.

Run:  nix shell nixpkgs#python3Packages.pytest -c pytest tests/enforced -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# `scripts/` is part of the runtime subset, so it moves into `runtime/` with
# everything else. Both spellings are legitimate -- before the migration and
# after it -- and hard-coding one is the same mistake the lint itself made by
# looking for `settings.json` in a single place.
_LINT_CANDIDATES = (REPO_ROOT / "runtime" / "scripts" / "lint-runtime-entrypoints.py",
                    REPO_ROOT / "scripts" / "lint-runtime-entrypoints.py")
LINT = next((p for p in _LINT_CANDIDATES if p.exists()), _LINT_CANDIDATES[-1])


def make_repo(root: Path, files: dict[str, str]) -> Path:
    """A throwaway config repo."""
    root.mkdir(parents=True, exist_ok=True)
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


def test_the_relocated_config_files_are_still_read(tmp_path):
    """Reported in review, and it had already happened.

    The runtime subset moves under `runtime/`, so `settings.json` lands one
    level deeper. Looking in one place only, the lint found nothing, read
    nothing, and printed **clean** -- a green tick with no content behind it.
    """
    root = make_repo(tmp_path, {
        "runtime/settings.json": settings("bash $HOME/.claude/tests/x.sh"),
        "tests/x.sh": "echo hi\n",
    })
    proc = run_lint(root)
    assert proc.returncode == 1, proc.stdout + proc.stderr


def test_an_empty_input_is_an_error_not_a_pass(tmp_path):
    """A check that cannot find its input must not report success.

    This is the general form of the bug above: whatever moves next, the lint
    says so instead of going quiet.
    """
    root = make_repo(tmp_path, {"hooks/guard.py": "pass\n"})
    proc = run_lint(root)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "empty input" in proc.stderr


def test_a_relocated_tree_is_still_checked_for_real(tmp_path):
    """Non-vacuity, on the moved layout specifically.

    The clean case and the violation case have to come from the same tree
    shape, or "clean" means only that the file was missing again.
    """
    clean = make_repo(tmp_path / "clean", {
        "runtime/settings.json": settings("bash $HOME/.claude/hooks/ok.sh"),
        "runtime/hooks/ok.sh": "echo hi\n",
    })
    assert run_lint(clean).returncode == 0
    dirty = make_repo(tmp_path / "dirty", {
        "runtime/settings.json": settings("bash $HOME/.claude/docs/x.sh"),
        "docs/x.sh": "echo hi\n",
    })
    assert run_lint(dirty).returncode == 1


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


def test_a_mention_is_reported_and_called_a_mention(tmp_path):
    """Shell semantics are not modelled, and the wording must not pretend they are.

    `echo tests/x.sh` is reported exactly like `bash tests/x.sh`, because the
    lint reads path tokens and does not work out which word is the program. The
    conservatism is deliberate -- a declared entrypoint has no business naming a
    dev-only path at all -- but it means the diagnostic may only claim the path
    was *named*. Pinned so nobody later reads the message as an execution claim,
    or "fixes" the over-report into a dataflow analysis.
    """
    root = make_repo(tmp_path, {
        "settings.json": settings("echo tests/x.sh"),
        "tests/x.sh": "echo hi\n",
    })
    proc = run_lint(root)
    assert proc.returncode == 1
    assert "names dev-only path" in proc.stderr
    assert "executes" not in proc.stderr


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
    # Normalisation.
    'bash $HOME/.claude/hooks/../tests/x.sh',
    'bash $HOME/.claude/./tests/x.sh',
    'bash $HOME/.claude/tests/../tests/x.sh',
    'bash $HOME/.claude/docs/../experiments/x.sh',
    'bash /home/someone/.claude/hooks/../tests/x.sh',
    'bash ~/.claude/hooks/../tests/x.sh',
    'bash /home/someone/.claude//tests/x.sh',
    # How the config dir gets named. Every one of these walked past the
    # five-literal-prefix resolver the first revision shipped, because the
    # first path segment ended up holding something like `$HOME"` or `X=$HOME`
    # rather than `tests`.
    'bash "$HOME"/.claude/tests/x.sh',
    'bash ${CLAUDE_CONFIG_DIR:-$HOME/.claude}/tests/x.sh',
    'bash $CLAUDE_CONFIG_DIR/tests/x.sh',
    'BASH_ENV=$HOME/.claude/tests/x.sh bash -c :',
    # The shape settings.json already uses for vendor/, so this is the
    # spelling most likely to appear by accident.
    'CLAUDE_PLUGIN_ROOT=$HOME/.claude/tests python3 run.py',
    'python3 run.py --config=$HOME/.claude/tests/x.sh',
    # The directory itself, with no trailing separator.
    'cd $HOME/.claude/tests && bash x.sh',
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


@pytest.mark.parametrize("field", [
    "apiKeyHelper",
    "awsAuthRefresh",
    "awsCredentialExport",
    "otelHeadersHelper",
])
def test_the_other_command_fields_are_checked(tmp_path, field):
    """hooks and statusLine are not the only fields the harness runs.

    Each of these is in the installed harness's own settings schema. The first
    revision read two fields and called them "the two files that declare what
    the harness will execute".
    """
    data = {"hooks": {}, field: "bash $HOME/.claude/tests/helper.sh"}
    root = make_repo(tmp_path, {
        "settings.json": json.dumps(data, indent=2),
        "tests/helper.sh": "echo hi\n",
    })
    proc = run_lint(root)
    assert proc.returncode == 1, field
    assert field in proc.stderr


@pytest.mark.parametrize("name", ["BASH_ENV", "PYTHONPATH"])
def test_env_is_an_execution_path(tmp_path, name):
    """`env` is not run directly, and reaches every hook regardless.

    BASH_ENV is sourced by each non-interactive bash; PYTHONPATH decides what
    `import` resolves to. A dev-only path in either is an execution path.
    """
    root = make_repo(tmp_path, {
        "settings.json": json.dumps(
            {"env": {name: "$HOME/.claude/tests"}}, indent=2),
        "tests/x.sh": "echo hi\n",
    })
    assert run_lint(root).returncode == 1, name


@pytest.mark.parametrize("value", [
    # Reported by review against 549cfe0; both scanned clean. `env` is the
    # surface this lint explicitly claims to cover, and a search path is the
    # ordinary shape of that surface -- so the hole was in the common case,
    # not an exotic one.
    "$HOME/.claude/tests:/usr/lib/python3",
    "$HOME/.claude/tests:$HOME/.claude/hooks",
    # The same list read the other way round. The first of these was already
    # caught before the fix, by accident: the resolver kept only the final
    # config-dir marker, so a dev-only entry survived exactly when it came
    # last. Keep both, so a regression cannot pass by catching one order.
    "$HOME/.claude/hooks:$HOME/.claude/tests",
    "/usr/lib/python3:$HOME/.claude/tests",
    # Three entries, dev-only in the middle: no end of the list is special.
    "/usr/lib/python3:$HOME/.claude/tests:$HOME/.claude/hooks",
])
def test_a_dev_tree_anywhere_in_a_path_list_is_found(tmp_path, value):
    """A path list is one token until `:` is treated as the separator it is."""
    root = make_repo(tmp_path, {
        "settings.json": json.dumps({"env": {"PYTHONPATH": value}}, indent=2),
        "tests/helper.py": "pass\n",
        "hooks/ok.py": "pass\n",
    })
    proc = run_lint(root)
    assert proc.returncode == 1, value
    # The report names the offending entry, not the whole list, or a reviewer
    # has to find it again by eye.
    assert "'$HOME/.claude/tests'" in proc.stderr, proc.stderr


def test_a_path_list_of_runtime_trees_is_still_clean(tmp_path):
    """The separator fix must not make every list look guilty.

    Splitting on `:` only matters if the entries are then judged on their own
    merits -- this is the case that would fail if the split turned into a
    substring search.
    """
    root = make_repo(tmp_path, {
        "settings.json": json.dumps({"env": {
            "PYTHONPATH": "$HOME/.claude/hooks:/usr/lib/python3:$HOME/.claude/mcps",
        }}, indent=2),
        "hooks/ok.py": "pass\n",
        "mcps/.keep": "",
    })
    proc = run_lint(root)
    assert proc.returncode == 0, proc.stderr


def test_an_mcp_server_env_path_list_is_checked(tmp_path):
    """Same hole, same fix, on the other side of check B.

    The entry is the bare directory on purpose. `$HOME/.claude/tests/stubs:...`
    was caught even before the separator fix, because the unsplit token still
    began `tests/` -- so writing it that way here would have tested nothing.
    """
    root = make_repo(tmp_path, {
        "settings.json": settings("python3 $HOME/.claude/hooks/ok.py"),
        "hooks/ok.py": "pass\n",
        "mcp-servers.json": json.dumps({"mcpServers": {"broker": {
            "command": "python3",
            "args": ["$HOME/.claude/mcps/broker/server.py"],
            "env": {"PYTHONPATH": "$HOME/.claude/tests:/usr/lib/python3"},
        }}}, indent=2),
        "mcps/broker/server.py": "pass\n",
        "tests/stubs/.keep": "",
    })
    assert run_lint(root).returncode == 1


def test_a_marketplace_source_path_is_checked(tmp_path):
    """A marketplace source path is loaded as plugin code."""
    root = make_repo(tmp_path, {
        "settings.json": json.dumps({"extraKnownMarketplaces": {
            "local": {"source": {"source": "directory",
                                 "path": "$HOME/.claude/tests/fixtures"}}}}, indent=2),
        "tests/fixtures/.keep": "",
    })
    assert run_lint(root).returncode == 1


def test_an_mcp_server_env_is_checked(tmp_path):
    root = make_repo(tmp_path, {
        "settings.json": settings("python3 $HOME/.claude/hooks/ok.py"),
        "hooks/ok.py": "pass\n",
        "mcp-servers.json": json.dumps({"mcpServers": {"broker": {
            "command": "python3",
            "args": ["$HOME/.claude/mcps/broker/server.py"],
            "env": {"PYTHONPATH": "$HOME/.claude/tests/stubs"},
        }}}, indent=2),
        "mcps/broker/server.py": "pass\n",
        "tests/stubs/.keep": "",
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
