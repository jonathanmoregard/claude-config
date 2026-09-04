"""An aborted in-place reset must not permanently disarm self-compact.

Regression, sid 6f11dbf0 on 2026-09-04: the trigger fired at 409k tokens
(13:04:59Z), inplace_reset aborted "still mid-turn 120s after the handoff
landed" (13:08:19Z) — nothing destroyed, handoff and marker intact — and the
session never retried: `armed:false` was final until a dip below the threshold,
and a 400k session does not dip. fires:1, no further attempt, self-compact
silently gone for the rest of the session.

The fix is a second, bounded re-arm channel: inplace_reset.py records every
terminal result in <sid>/outcome.json, and the trigger consumes an abort
outcome (once) to re-arm and fire again in the same Stop invocation. Bounds are
env-overridable INPUTS (SELF_COMPACT_MAX_RETRIES, default 3;
SELF_COMPACT_RETRY_SPACING_SECONDS, default 900) — never an on/off switch.
"""
import importlib.util
import io
import json
import time
import types
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "self_compact_trigger",
    Path(__file__).resolve().parent.parent / "self-compact-trigger.py")
sct = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sct)

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Drive sct.main() hermetically: stdin payload, faked occupancy, stubbed
    spawn. CLAUDE_SESSIONS_DIR isolates all state into tmp, per conftest idiom.
    """
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("CLAUDE_SESSIONS_DIR", str(sessions))
    monkeypatch.delenv("REFLECT_SUBPROCESS", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.delenv("SELF_COMPACT_MAX_RETRIES", raising=False)
    monkeypatch.delenv("SELF_COMPACT_RETRY_SPACING_SECONDS", raising=False)

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("{}\n")
    cwd = tmp_path / "cwd"
    cwd.mkdir()

    spawned = []
    monkeypatch.setattr(
        sct, "spawn_inplace_reset",
        lambda sid, instructions="": (spawned.append(sid), "stubbed")[1])

    def run(tokens):
        payload = {"session_id": SID, "transcript_path": str(transcript),
                   "cwd": str(cwd), "hook_event_name": "Stop"}
        monkeypatch.setattr(sct.sys, "stdin", io.StringIO(json.dumps(payload)))
        occ = sct.context_usage.Occupancy(
            total_tokens=tokens, window=1_000_000,
            fill_pct=tokens / 1_000_000, user_messages=5,
            model="claude-opus-5", estimated=False)
        monkeypatch.setattr(sct.context_usage, "occupancy", lambda tp: occ)
        return sct.main()

    def state():
        return json.loads((sessions / SID / "state.json").read_text())

    def patch_state(**kv):
        p = sessions / SID / "state.json"
        st = json.loads(p.read_text())
        st.update(kv)
        p.write_text(json.dumps(st))

    def write_outcome(status, reason="mid_turn_timeout", at=None):
        d = sessions / SID
        d.mkdir(parents=True, exist_ok=True)
        (d / "outcome.json").write_text(json.dumps(
            {"status": status, "reason": reason,
             "at": int(at if at is not None else time.time())}))

    return types.SimpleNamespace(
        sessions=sessions, run=run, spawned=spawned, state=state,
        patch_state=patch_state, write_outcome=write_outcome)


def test_abort_outcome_rearms_and_fires_again(harness):
    assert harness.run(350_000) == 0
    st = harness.state()
    assert st["fires"] == 1 and st["armed"] is False

    now = int(time.time())
    harness.patch_state(last_fired_at=now - 1000)   # spacing (900s) elapsed
    harness.write_outcome("aborted", at=now - 500)  # abort AFTER the fire
    assert harness.run(360_000) == 0

    st = harness.state()
    assert st["fires"] == 2
    assert st["rearm_retries"] == 1
    assert st["armed"] is False                     # the retry fire disarms again
    assert st["last_abort_consumed_at"] == now - 500
    assert len(harness.spawned) == 2


def test_retry_before_spacing_elapsed_does_not_rearm(harness):
    assert harness.run(350_000) == 0
    harness.write_outcome("aborted")  # at=now; last_fired_at=now; 900s not elapsed
    assert harness.run(360_000) == 0
    st = harness.state()
    assert st["fires"] == 1
    assert st.get("rearm_retries", 0) == 0
    assert len(harness.spawned) == 1


def test_retry_cap_stops_rearming_and_logs_once(harness):
    assert harness.run(350_000) == 0
    now = int(time.time())
    harness.patch_state(last_fired_at=now - 1000, rearm_retries=3)
    harness.write_outcome("aborted", at=now - 500)

    assert harness.run(360_000) == 0
    st = harness.state()
    assert st["fires"] == 1
    assert st["rearm_capped"] is True

    # A second Stop against the same capped state must not log a second time.
    assert harness.run(360_000) == 0
    assert harness.state()["fires"] == 1
    log_text = (harness.sessions / "trigger.log").read_text()
    assert log_text.count("re-arm capped") == 1


def test_success_outcome_does_not_rearm_and_resets_counter(harness):
    assert harness.run(350_000) == 0
    now = int(time.time())
    harness.patch_state(last_fired_at=now - 1000, rearm_retries=2)
    harness.write_outcome("ok", reason="reset_complete", at=now - 500)
    assert harness.run(360_000) == 0
    st = harness.state()
    assert st["fires"] == 1           # control: success never buys another fire
    assert st["rearm_retries"] == 0   # retry budget returned
    assert len(harness.spawned) == 1


def test_same_abort_outcome_cannot_buy_two_rearms(harness):
    assert harness.run(350_000) == 0
    now = int(time.time())
    harness.patch_state(last_fired_at=now - 2000)
    harness.write_outcome("aborted", at=now - 1500)
    assert harness.run(360_000) == 0
    assert harness.state()["fires"] == 2

    # Backdate the retry fire so ONLY the consumed-once guard can refuse: the
    # outcome is newer than last_fired_at and spacing has elapsed again.
    harness.patch_state(last_fired_at=now - 2000)
    assert harness.run(360_000) == 0
    st = harness.state()
    assert st["fires"] == 2
    assert st["rearm_retries"] == 1


def test_an_outcome_older_than_the_fire_is_stale(harness):
    # An abort left over from an EARLIER crossing answers a fire that already
    # got its retry story; it must not un-disarm the current one.
    assert harness.run(350_000) == 0
    now = int(time.time())
    harness.patch_state(last_fired_at=now - 1000)
    harness.write_outcome("aborted", at=now - 1500)  # predates the fire
    assert harness.run(360_000) == 0
    assert harness.state()["fires"] == 1


@pytest.mark.parametrize("raw", [
    "{ not json",                                          # garbage bytes
    json.dumps([]),                                        # non-dict JSON
    json.dumps("aborted"),                                 # non-dict JSON (string)
    json.dumps({"status": "aborted", "reason": "x"}),      # no `at` at all
    json.dumps({"status": "aborted", "reason": "x",
                "at": "soon"}),                            # non-int `at`
])
def test_a_corrupt_outcome_record_never_rearms(harness, raw):
    # outcome.json is the one Stop-hook read of a file ANOTHER process writes;
    # a corrupt or half-shaped record must read as "no reset has reported
    # back" — it must never crash the hook and never buy a fire.
    assert harness.run(350_000) == 0
    # Backdate the fire so ONLY the record's shape can refuse the re-arm.
    harness.patch_state(last_fired_at=int(time.time()) - 1000)
    (harness.sessions / SID / "outcome.json").write_text(raw)
    assert harness.run(360_000) == 0
    st = harness.state()
    assert st["fires"] == 1
    assert st["armed"] is False
    assert st.get("rearm_retries", 0) == 0
    assert len(harness.spawned) == 1


def test_no_outcome_means_no_rearm(harness):
    # The pre-existing silence on repeat Stops (smoke TEST 23) is unchanged
    # when no reset ever reported back.
    assert harness.run(350_000) == 0
    harness.patch_state(last_fired_at=int(time.time()) - 1000)
    assert harness.run(360_000) == 0
    assert harness.state()["fires"] == 1
    assert len(harness.spawned) == 1


def test_dip_below_threshold_still_rearms_and_resets_retries(harness):
    assert harness.run(350_000) == 0
    harness.patch_state(rearm_retries=2, rearm_capped=True)
    assert harness.run(90_000) == 0
    st = harness.state()
    assert st["armed"] is True
    assert st["fires"] == 1           # re-arming spends nothing (smoke TEST 23)
    assert st["rearm_retries"] == 0   # a dip is a new crossing: fresh budget
    assert "rearm_capped" not in st


def test_abort_rearm_cannot_breach_max_fires_per_session(harness):
    assert harness.run(350_000) == 0
    now = int(time.time())
    harness.patch_state(last_fired_at=now - 1000,
                        fires=sct.MAX_FIRES_PER_SESSION)
    harness.write_outcome("aborted", at=now - 500)
    assert harness.run(360_000) == 0
    st = harness.state()
    assert st["fires"] == sct.MAX_FIRES_PER_SESSION
    assert len(harness.spawned) == 1
    # The cap gate must run BEFORE the re-arm channel: a fire-capped session
    # never consumes an outcome (or logs a RE-ARM line per Stop) for a fire it
    # can never have.
    assert st["rearm_retries"] == 0
    assert "RE-ARM" not in (harness.sessions / "trigger.log").read_text()


def test_spacing_bound_is_an_env_input(harness, monkeypatch):
    monkeypatch.setenv("SELF_COMPACT_RETRY_SPACING_SECONDS", "0")
    assert harness.run(350_000) == 0
    harness.write_outcome("aborted")  # at=now; spacing 0 allows an instant retry
    assert harness.run(360_000) == 0
    assert harness.state()["fires"] == 2


def test_garbage_env_bounds_fall_back_to_defaults(harness, monkeypatch):
    # A Stop hook runs at every turn boundary; a typo'd env var must degrade to
    # the documented default, never crash the hook.
    monkeypatch.setenv("SELF_COMPACT_RETRY_SPACING_SECONDS", "soon")
    monkeypatch.setenv("SELF_COMPACT_MAX_RETRIES", "-3")
    assert harness.run(350_000) == 0
    harness.write_outcome("aborted")  # inside the default 900s spacing
    assert harness.run(360_000) == 0
    assert harness.state()["fires"] == 1
