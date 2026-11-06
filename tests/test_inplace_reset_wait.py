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

# `runtime/` once the layout migration has landed, the repository root before
# it. Only the projected subset moves; tests/ stays at the root.
RUNTIME = Path(__file__).resolve().parents[1]
if (RUNTIME / "runtime").is_dir():
    RUNTIME = RUNTIME / "runtime"

SPEC = importlib.util.spec_from_file_location(
    "inplace_reset", RUNTIME / "inplace_reset.py")
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


# --- Leaving the turn before the clear ---------------------------------------
#
# Regression, sid f90449b2 on 2026-08-27: the handoff wait breaks the moment the
# file exists, which is the session's Write TOOL CALL, not the end of its turn.
# `/clear` went out 11 seconds before `end_turn` and the TUI queued it as text —
# `queue-operation enqueue` carrying the write-instruction with `/clear` on the
# end, then `remove`. The command never ran and the handoff turn was spent for
# nothing.

def test_a_session_still_in_its_turn_does_not_get_cleared(registry):
    registry("busy")
    assert ir.wait_for_turn_to_end(os.getpid(), SID, deadline_seconds=0) is False


def test_the_clear_proceeds_once_the_turn_has_ended(registry):
    registry("idle")
    assert ir.wait_for_turn_to_end(os.getpid(), SID, deadline_seconds=0) is True


def test_a_turn_that_ends_while_we_wait_is_waited_out(registry, monkeypatch):
    # The whole point: busy now, idle a moment later, and the clear still lands.
    registry("busy")
    calls = {"n": 0}
    real = ir.session_is_working

    def flips(spid, sid):
        calls["n"] += 1
        if calls["n"] >= 3:
            registry("idle")
        return real(spid, sid)

    monkeypatch.setattr(ir, "session_is_working", flips)
    monkeypatch.setattr(ir.time, "sleep", lambda _s: None)
    assert ir.wait_for_turn_to_end(os.getpid(), SID) is True
    assert calls["n"] >= 3


def test_an_unreadable_registry_does_not_block_the_clear(tmp_path, monkeypatch):
    # Degrading to "not working" restores the pre-fix behaviour — send now — and
    # the post-clear confirmation still refuses if the clear did not land. The
    # alternative, refusing whenever the registry is unreadable, would hand a
    # missing file a veto over every reset.
    home = tmp_path / "home"
    (home / ".claude" / "sessions").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert ir.wait_for_turn_to_end(os.getpid(), SID, deadline_seconds=0) is True


def test_the_turn_wait_is_bounded_and_settles_before_sending():
    # Bounded: the instruction it answers says "write the file and then stop", so
    # a tail longer than this is not that turn's tail.
    assert 0 < ir.CLEAR_IDLE_WAIT_SECONDS <= 300
    # ...and it must clear the measured 11s gap between the Write tool call and
    # `end_turn` with room to spare.
    assert ir.CLEAR_IDLE_WAIT_SECONDS >= 60
    assert ir.TURN_SETTLE_SECONDS > 0


def test_grace_is_longer_than_a_turn_and_cap_bounds_the_wait():
    assert ir.WRITE_IDLE_GRACE_SECONDS >= 240
    assert ir.WRITE_WAIT_CAP_SECONDS > ir.WRITE_IDLE_GRACE_SECONDS
    # The cap has to clear a long single tool call: a deep research call runs
    # 10-15 minutes and the session cannot read the instruction until it ends.
    assert ir.WRITE_WAIT_CAP_SECONDS >= 900


# --- Draining background subagents before the clear --------------------------
#
# A clear ends the context, not the work. Subagents this session dispatched keep
# running and the clear interrupts every one of them, so the reset waits them out
# first. Liveness is read from each transcript's LAST ENTRY, never from mtime:
# a transcript sits unchanged for the whole of one long tool call.

@pytest.fixture
def subagents(tmp_path, monkeypatch):
    """Write fake subagent transcripts for SID and return their directory."""
    home = tmp_path / "home"
    d = home / ".claude" / "projects" / "-some-project" / SID / "subagents"
    d.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    def write(agent_id, entry, age=0.0):
        p = d / f"agent-{agent_id}.jsonl"
        p.write_text(json.dumps(entry) + "\n" if not isinstance(entry, str) else entry)
        if age:
            import time
            os.utime(p, (time.time() - age, time.time() - age))
        return p

    return write


def _assistant(stop_reason, blocks):
    return {"message": {"role": "assistant", "stop_reason": stop_reason, "content": blocks}}


def test_a_finished_agent_does_not_hold_the_reset(subagents):
    subagents("done", _assistant("end_turn", [{"type": "text", "text": "result"}]))
    assert ir.live_subagents(SID) == []


def test_an_agent_waiting_on_a_tool_is_live(subagents):
    subagents("midtool", _assistant("tool_use", [{"type": "tool_use", "name": "Read"}]))
    assert ir.live_subagents(SID) == ["midtool"]


def test_an_already_interrupted_agent_does_not_hold_the_reset(subagents):
    # A casualty of some EARLIER clear must not pin this one for three hours.
    subagents("killed", {"message": {"role": "user", "content": [
        {"type": "text", "text": ir.INTERRUPTED_MARK}]}})
    assert ir.live_subagents(SID) == []


def test_a_stale_transcript_is_treated_as_a_dead_agent(subagents):
    subagents("corpse", _assistant("tool_use", [{"type": "tool_use", "name": "Read"}]),
              age=ir.SUBAGENT_STALE_SECONDS + 60)
    assert ir.live_subagents(SID) == []


def test_an_unparseable_transcript_cannot_veto_every_future_reset(subagents):
    subagents("corrupt", "{ this is not json\n")
    assert ir.live_subagents(SID) == []


def test_only_this_session_s_agents_are_waited_on(tmp_path, monkeypatch):
    home = tmp_path / "home"
    other = home / ".claude" / "projects" / "-some-project" / "other-sid" / "subagents"
    other.mkdir(parents=True)
    (other / "agent-notours.jsonl").write_text(
        json.dumps(_assistant("tool_use", [{"type": "tool_use", "name": "Read"}])) + "\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert ir.live_subagents(SID) == []


def test_a_missing_projects_tree_degrades_to_nothing_running(tmp_path, monkeypatch):
    # Degrading to "nothing live" keeps the previous behaviour (clear proceeds)
    # rather than making an unreadable directory block every reset forever.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert ir.live_subagents(SID) == []


def test_the_drain_ceiling_is_three_hours_and_exceeds_staleness():
    # Set by the operator 2026-08-20. A review or fix agent legitimately runs
    # over an hour; cutting one short costs the work, waiting costs only time.
    assert ir.SUBAGENT_WAIT_CAP_SECONDS == 3 * 60 * 60
    # A dead agent must be skippable well inside the ceiling, or it holds the
    # drain open for the full three hours.
    assert ir.SUBAGENT_STALE_SECONDS < ir.SUBAGENT_WAIT_CAP_SECONDS
    # ...but must still clear the longest observed silent gap in a live agent.
    assert ir.SUBAGENT_STALE_SECONDS >= 900
