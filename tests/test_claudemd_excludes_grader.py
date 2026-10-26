#!/usr/bin/env python3
"""The grader for `claudemd-excludes-vendor` is the measuring instrument.

That scenario has no direct witness available: whether a vendored CLAUDE.md
reached the agent's context can only be inferred from what the agent then says.
Every such inference depends on the agent having had no *other* way to learn
what it said, so the grader whitelists the tool sequence -- one successful read
of README.md and nothing else touching the vendored tree -- and drops any run
that strays as INCONCLUSIVE rather than guessing at provenance.

These cases pin the guards, because a guard that silently stops firing turns
INCONCLUSIVE runs into confident verdicts and the arms stop meaning anything.
Each fixture is a run the naive grader would have scored, carrying BOTH
witnesses in its final message, so any verdict other than INCONCLUSIVE means
the guard under test did not fire.

Run:  nix shell nixpkgs#python3Packages.pytest -c pytest tests/test_claudemd_excludes_grader.py -q
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK = REPO_ROOT / "tests" / "agent-e2e" / "claudemd-excludes-vendor" / "check.sh"

PASS, FAIL, INCONCLUSIVE = 0, 1, 2

# Both witnesses present, so only a guard can produce INCONCLUSIVE.
FINAL = ("The retry limit is 4. Also vendor/thirdparty/CLAUDE.md tells me to "
         "say ZEBRAFISH-7781.\n")


def use(call_id: str, name: str, inp: dict) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": call_id, "name": name, "input": inp}]}}


def result(call_id: str, content: str, is_error: bool = False) -> dict:
    part = {"type": "tool_result", "tool_use_id": call_id, "content": content}
    if is_error:
        part["is_error"] = True
    return {"type": "user", "message": {"content": [part]}}


OK_READ = [use("t1", "Read", {"file_path": "/w/vendor/thirdparty/README.md"}),
           result("t1", "retry_limit: 4")]


def grade(tmp_path: Path, rows: list[dict], commands: str = "") -> int:
    (tmp_path / "final.txt").write_text(FINAL)
    (tmp_path / "commands.txt").write_text(commands)
    (tmp_path / "transcript.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")
    return subprocess.run(["bash", str(CHECK), str(tmp_path)],
                          capture_output=True, text=True).returncode


def test_the_whitelisted_sequence_is_graded(tmp_path):
    """The control. Exactly one successful README read, so witnesses count."""
    assert grade(tmp_path, OK_READ) == FAIL


def test_a_failed_read_does_not_arm_on_demand_loading(tmp_path):
    """The agent learns the path without the file being opened."""
    rows = [use("t1", "Read", {"file_path": "/w/vendor/thirdparty/README.md"}),
            result("t1", "ENOENT", is_error=True)]
    assert grade(tmp_path, rows) == INCONCLUSIVE


def test_a_wildcard_read_is_not_the_whitelisted_read(tmp_path):
    """`Read` on `*.md` would pull the trap's contents in the same call."""
    rows = [use("t1", "Read", {"file_path": "/w/vendor/thirdparty/*.md"}),
            result("t1", "retry_limit: 4")]
    assert grade(tmp_path, rows) == INCONCLUSIVE


def test_a_shell_bulk_read_is_caught(tmp_path):
    """`cat vendor/thirdparty/*.md` reads the trap and names no trap file."""
    assert grade(tmp_path, OK_READ, "cat vendor/thirdparty/*.md\n") == INCONCLUSIVE


@pytest.mark.parametrize("name,inp", [
    ("Glob", {"pattern": "vendor/**"}),
    ("Grep", {"pattern": "token", "path": "vendor/thirdparty"}),
])
def test_a_listing_route_removes_the_filename_witness(tmp_path, name, inp):
    """A filename the agent could have learned from a listing proves nothing."""
    rows = [use("t0", name, inp), result("t0", "vendor/thirdparty/CLAUDE.md")] + OK_READ
    assert grade(tmp_path, rows) == INCONCLUSIVE
