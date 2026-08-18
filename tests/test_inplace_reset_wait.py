"""The handoff wait must be bounded by the session going idle, not by a clock.

Regression: a flat 240s deadline fired against a session that was mid-turn and
gave up 111 seconds before its (correct) handoff landed. An injected instruction
cannot be read until the current turn ends, so patience has to track whether the
session is still working.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "inplace_reset", Path(__file__).resolve().parent.parent / "inplace_reset.py")
ir = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ir)

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A fake ~/.claude/sessions/<pid>.json we can flip between busy and idle."""
    home = tmp_path / "home"
    (home / ".claude" / "sessions").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    def write(status, sid=SID, pid=os.getpid()):
        (home / ".claude" / "sessions" / f"{pid}.json").write_text(
            json.dumps({"pid": pid, "sessionId": sid, "status": status}))

    return write


def test_busy_session_reads_as_working(registry):
    registry("busy")
    assert ir.session_is_working(os.getpid(), SID) is True


def test_idle_session_does_not_read_as_working(registry):
    registry("idle")
    assert ir.session_is_working(os.getpid(), SID) is False


def test_registry_for_a_different_session_is_ignored(registry):
    # Same pid reused by a session that is not ours: its busyness is not ours.
    registry("busy", sid="99999999-0000-0000-0000-000000000000")
    assert ir.session_is_working(os.getpid(), SID) is False


def test_missing_registry_degrades_to_not_working(tmp_path, monkeypatch):
    # Degrading to False means the plain idle timeout applies, which is the
    # behaviour this replaced — never an infinite wait.
    home = tmp_path / "home"
    (home / ".claude" / "sessions").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert ir.session_is_working(os.getpid(), SID) is False


def test_dead_process_does_not_read_as_working(registry):
    registry("busy", pid=2 ** 22 - 1)
    assert ir.session_is_working(2 ** 22 - 1, SID) is False


def test_grace_is_longer_than_a_turn_and_cap_bounds_the_wait():
    assert ir.WRITE_IDLE_GRACE_SECONDS >= 240
    assert ir.WRITE_WAIT_CAP_SECONDS > ir.WRITE_IDLE_GRACE_SECONDS
    # The cap has to clear a long single tool call: a deep research call runs
    # 10-15 minutes and the session cannot read the instruction until it ends.
    assert ir.WRITE_WAIT_CAP_SECONDS >= 900
