"""The skill must describe the tools that actually exist.

A skill that documents a flag the CLI does not have, or promises a check nobody
wired up, is worse than no skill: it is wrong in a form a future agent trusts.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

from conftest import REPO_ROOT

SKILL = os.path.join(REPO_ROOT, "skills", "adopting-an-external-api", "SKILL.md")
CLI = os.path.join(REPO_ROOT, "bin", "egress-sink")
LINT = os.path.join(REPO_ROOT, "bin", "lint-direct-egress")


def read_skill() -> str:
    with open(SKILL, "r", encoding="utf-8") as fh:
        return fh.read()


def test_skill_frontmatter_is_triggers_only():
    text = read_skill()
    assert text.startswith("---")
    front = text.split("---")[1]
    assert "name:" in front and "description:" in front
    assert front.strip().startswith("name:")
    # Description states WHEN, not the workflow: no step verbs.
    desc = re.search(r"description:\s*(.+)", front).group(1).lower()
    for leaked in ("first investigate", "then register", "step 1", "hardcode"):
        assert leaked not in desc, f"description summarises workflow: {leaked!r}"


def test_every_cli_flag_the_skill_mentions_exists():
    text = read_skill()
    help_out = subprocess.run(
        [sys.executable, CLI, "add", "--help"], capture_output=True, text=True, check=True
    ).stdout
    for flag in re.findall(r"--[a-z-]+", text):
        if flag in ("--interval", "--note"):
            assert flag in help_out, f"skill documents {flag}, CLI does not accept it"


def test_lint_command_named_in_the_skill_runs():
    text = read_skill()
    assert "lint-direct-egress" in text
    out = subprocess.run([sys.executable, LINT, "--help"], capture_output=True, text=True)
    assert out.returncode == 0


def test_skill_carries_no_incident_provenance():
    """Instructions only: a reader must not be able to tell which incident caused it."""
    text = read_skill().lower()
    for banned in ("2026", "cloudflare", "forum", "effectivealtruism", "jonathan"):
        assert banned not in text, f"provenance leaked into the skill: {banned!r}"


def test_systemd_unit_restarts_always():
    """The 'proxy is down so I used the real URL' bypass is closed by restarting."""
    path = os.path.join(REPO_ROOT, "mcps", "egress-guard", "egress-sink.service")
    with open(path, "r", encoding="utf-8") as fh:
        unit = fh.read()
    assert "Restart=always" in unit
    assert "egress-sink serve" in unit
