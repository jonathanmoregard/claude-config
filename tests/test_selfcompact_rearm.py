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
SELF_COMPACT_RETRY_SPACING_SECONDS, default 0, measured from the previous
attempt's TERMINAL time) — never an on/off switch.
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
        lambda sid, instructions="": (
            spawned.append((sid, instructions)), "stubbed")[1])

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

    def write_manual_request(instructions):
        """What `--manual` (i.e. /self-compact) leaves on disk. It stays there
        until a reset LANDS, which renames it to consumed.json."""
        d = sessions / SID
        d.mkdir(parents=True, exist_ok=True)
        (d / "request.json").write_text(json.dumps(
            {"session_id": SID, "cwd": str(cwd), "transcript_path": str(transcript),
             "manual": True, "instructions": instructions,
             "requested_at_epoch": int(time.time())}))

    def request():
        return json.loads((sessions / SID / "request.json").read_text())

    return types.SimpleNamespace(
        sessions=sessions, run=run, spawned=spawned, state=state,
        patch_state=patch_state, write_outcome=write_outcome,
        write_manual_request=write_manual_request, request=request)


def test_abort_outcome_rearms_and_fires_again(harness):
    assert harness.run(350_000) == 0
    st = harness.state()
    assert st["fires"] == 1 and st["armed"] is False

    now = int(time.time())
    harness.patch_state(last_fired_at=now - 1000)
    harness.write_outcome("aborted", at=now - 500)  # abort AFTER the fire
    assert harness.run(360_000) == 0

    st = harness.state()
    assert st["fires"] == 2
    assert st["rearm_retries"] == 1
    assert st["armed"] is False                     # the retry fire disarms again
    assert st["last_abort_consumed_at"] == now - 500
    assert len(harness.spawned) == 2


def test_the_measured_timeline_retries_on_the_next_stop(harness):
    """The incident's OWN timing, replayed: fire T, abort T+200, Stop T+205.

    MEASURED sid 6f11dbf0: fired 13:04:59Z, aborted 13:08:19Z — an abort lands
    seconds to minutes after the fire, never at the fire. The re-arm promises
    the retry rides the next turn boundary ("a Stop event IS a turn boundary —
    the one moment the dominant abort shape, still-mid-turn, is guaranteed to
    have just ended"), so the Stop right after that abort must refire.
    """
    assert harness.run(350_000) == 0
    now = int(time.time())
    harness.patch_state(last_fired_at=now - 205)                    # fire, T
    harness.write_outcome("aborted", at=now - 5)                    # abort, T+200
    assert harness.run(360_000) == 0                                # Stop, T+205

    st = harness.state()
    assert st["fires"] == 2
    assert st["rearm_retries"] == 1
    assert len(harness.spawned) == 2


def test_spacing_is_measured_from_the_terminal_time_not_the_fire(harness, monkeypatch):
    # THE ANCHOR, pinned in the direction that distinguishes it: the fire is
    # long past (1000s) and the abort is fresh (10s), so a last_fired_at-anchored
    # bound would allow this retry and a terminal-anchored one refuses it.
    monkeypatch.setenv("SELF_COMPACT_RETRY_SPACING_SECONDS", "600")
    assert harness.run(350_000) == 0
    now = int(time.time())
    harness.patch_state(last_fired_at=now - 1000)
    harness.write_outcome("aborted", at=now - 10)
    assert harness.run(360_000) == 0
    st = harness.state()
    assert st["fires"] == 1
    assert st.get("rearm_retries", 0) == 0
    assert "last_abort_consumed_at" not in st  # unconsumed: a later Stop retries
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
    json.dumps({"status": "aborted", "reason": "x",
                "at": True}),                              # bool `at` (isinstance
                                                           # int is True; not an epoch)
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


def test_a_bool_at_never_buys_a_rearm_even_with_no_usable_last_fired(harness):
    # The one shape where the bool guard is load-bearing. MEASURED against the
    # pre-fix `not isinstance(at, int)`: with last_fired_at backdated (the
    # parametrization above) `at:true` -> 1 is refused as stale either way, but
    # with last_fired_at absent or corrupt (int_field defaults it to 0) the old
    # guard returned True and spent a retry on a record that is not a timestamp.
    # Bools are ints in Python; int_field refuses them on the state side and the
    # outcome side must agree.
    assert harness.run(350_000) == 0
    harness.patch_state(last_fired_at="2026-09-04T13:04:59Z")  # -> int_field 0
    (harness.sessions / SID / "outcome.json").write_text(json.dumps(
        {"status": "aborted", "reason": "x", "at": True}))
    assert harness.run(360_000) == 0
    st = harness.state()
    assert st["fires"] == 1
    assert st.get("rearm_retries", 0) == 0
    assert "last_abort_consumed_at" not in st
    assert len(harness.spawned) == 1


def test_a_lock_held_abort_never_buys_a_rearm(harness):
    # lock_held is written by a SECOND process saying the FIRST still holds
    # reset.lock and is still waiting (up to 900s). It is not the terminal
    # state of the attempt this crossing is stuck on, so consuming it would
    # drain the retry budget against a live holder — every refire hits the same
    # lock — and leave nothing for the holder's real outcome.
    assert harness.run(350_000) == 0
    now = int(time.time())
    harness.patch_state(last_fired_at=now - 1000)
    harness.write_outcome("aborted", reason="lock_held", at=now - 10)
    assert harness.run(360_000) == 0
    st = harness.state()
    assert st["fires"] == 1
    assert st.get("rearm_retries", 0) == 0
    assert "last_abort_consumed_at" not in st

    # The holder finishing overwrites that record, and THAT one buys the retry.
    harness.write_outcome("aborted", reason="mid_turn_timeout", at=now - 5)
    assert harness.run(360_000) == 0
    assert harness.state()["fires"] == 2


@pytest.mark.parametrize("field", ["last_fired_at", "last_abort_consumed_at"])
@pytest.mark.parametrize("bad", ["2026-09-04T13:04:59Z", None, True, {"at": 1}])
def test_a_malformed_state_field_cannot_kill_the_hook(harness, field, bad):
    # state.json is read on every Stop and hand-edited during incidents. A str
    # where an int belongs used to raise TypeError in the `at < last_fired`
    # comparison, and __main__'s blanket except turned that into a silently
    # dead trigger — indistinguishable from a session that never crossed.
    assert harness.run(350_000) == 0
    now = int(time.time())
    harness.patch_state(last_fired_at=now - 1000)   # the fire, then corrupt one field
    harness.patch_state(last_fired_at=now - 1000)   # the fire, then corrupt one field
    harness.patch_state(**{field: bad})
    harness.write_outcome("aborted", at=now - 5)
    assert harness.run(360_000) == 0
    # Coerce-or-default, so the re-arm still works off the outcome record.
    st = harness.state()
    assert st["fires"] == 2
    assert st["rearm_retries"] == 1
    assert st["last_abort_consumed_at"] == now - 5


@pytest.mark.parametrize("counter", ["fires", "crossings", "rearm_retries"])
def test_a_malformed_counter_does_not_crash_the_fire_path(harness, counter):
    # Same class, on the fields the fire path does arithmetic with.
    assert harness.run(350_000) == 0
    harness.patch_state(**{counter: "many"})
    harness.patch_state(last_fired_at=int(time.time()) - 1000)
    harness.write_outcome("aborted", at=int(time.time()) - 5)
    assert harness.run(360_000) == 0
    st = harness.state()
    assert isinstance(st[counter], int)
    assert st["fires"] >= 1 and len(harness.spawned) == 2


def test_no_outcome_means_no_rearm(harness):
    # The pre-existing silence on repeat Stops (smoke TEST 23) is unchanged
    # when no reset ever reported back.
    assert harness.run(350_000) == 0
    harness.patch_state(last_fired_at=int(time.time()) - 1000)
    assert harness.run(360_000) == 0
    assert harness.state()["fires"] == 1
    assert len(harness.spawned) == 1


STEER = "land the migration branch first, then read tasks/mission.md"


def test_an_automatic_fire_does_not_downgrade_a_pending_manual_request(harness):
    # /self-compact wrote a steered marker and its reset aborted, so the marker
    # is still pending (only a reset that LANDS renames it to consumed.json).
    # The automatic fire overwrites that file: without carrying the fields it
    # would silently convert the operator's steered handoff into a plain one —
    # the fork prompt loses the text a human typed this session, and
    # idle-handoff.sh re-applies the fill gate a manual request does not rest on.
    harness.write_manual_request(STEER)
    assert harness.run(350_000) == 0

    req = harness.request()
    assert req["manual"] is True
    assert req["instructions"] == STEER
    assert req["tokens"] == 350_000          # still the automatic fire's own claim
    assert harness.spawned == [(SID, STEER)]


def test_an_abort_retry_carries_the_manual_steer_forward(harness):
    # The re-arm path answers the SAME operator request, so it inherits it too.
    harness.write_manual_request(STEER)
    assert harness.run(350_000) == 0
    now = int(time.time())
    harness.patch_state(last_fired_at=now - 205)
    harness.write_outcome("aborted", at=now - 5)
    assert harness.run(360_000) == 0

    assert harness.state()["fires"] == 2
    assert harness.request()["instructions"] == STEER
    assert harness.spawned[-1] == (SID, STEER)


def test_an_automatic_request_never_claims_to_be_manual(harness):
    # Control: with nothing pending, the marker makes only the claim it can
    # support — a measured crossing. `manual` relaxes a downstream gate and is
    # never invented here.
    assert harness.run(350_000) == 0
    req = harness.request()
    assert "manual" not in req and "instructions" not in req
    assert harness.spawned == [(SID, "")]


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
    # The same 600s bound the anchor test refuses under, now satisfied: an
    # operator-set clock delays the retry, it never cancels it.
    monkeypatch.setenv("SELF_COMPACT_RETRY_SPACING_SECONDS", "600")
    assert harness.run(350_000) == 0
    now = int(time.time())
    harness.patch_state(last_fired_at=now - 1000)
    harness.write_outcome("aborted", at=now - 700)
    assert harness.run(360_000) == 0
    assert harness.state()["fires"] == 2


def test_the_default_spacing_lets_the_next_stop_retry(harness):
    # Default 0: the outcome record IS the wait, so the turn boundary that
    # follows an abort carries the retry. Spend stays bounded by MAX_RETRIES.
    assert harness.run(350_000) == 0
    harness.write_outcome("aborted")  # at=now, i.e. the abort just landed
    assert harness.run(360_000) == 0
    assert harness.state()["fires"] == 2


def test_garbage_env_bounds_fall_back_to_defaults(harness, monkeypatch):
    # A Stop hook runs at every turn boundary; a typo'd env var must degrade to
    # the documented default, never crash the hook.
    monkeypatch.setenv("SELF_COMPACT_RETRY_SPACING_SECONDS", "soon")
    monkeypatch.setenv("SELF_COMPACT_MAX_RETRIES", "-3")
    assert harness.run(350_000) == 0
    harness.write_outcome("aborted")
    assert harness.run(360_000) == 0
    # A raw -3 would cap this crossing on the spot (0 >= -3) and log it; the
    # documented fallback (3) leaves the budget spendable. That is what tells
    # the fallback from the garbage, and it is the only thing that can.
    assert "re-arm capped" not in (harness.sessions / "trigger.log").read_text()
    assert "rearm_capped" not in harness.state()
    assert harness.state()["fires"] == 2
    assert harness.state()["rearm_retries"] == 1
