"""Every terminal path of an in-place reset ATTEMPT leaves a machine-readable
outcome — and the two paths that are not an attempt leave none.

Regression, sid 6f11dbf0 on 2026-09-04: the reset aborted ("still mid-turn 120s
after the handoff landed"), the trigger's state stayed armed:false, and the
session never retried — the only record of the abort was a prose line in
inplace-reset.log, which the trigger rightly does not parse. outcome.json in the
session dir is the machine channel; the log stays human-forensic.

The record is best-effort, like log(): an unwritable outcome file must never
turn a completed reset into a failure, nor block an abort path that destroyed
nothing.
"""
import fcntl
import importlib.util
import json
import os
import time
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
def sessions(tmp_path, monkeypatch):
    d = tmp_path / "sessions"
    d.mkdir()
    monkeypatch.setenv("CLAUDE_SESSIONS_DIR", str(d))
    return d


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """A fake ~/.claude/sessions/<pid>.json — same harness as
    test_inplace_reset_wait.py."""
    home = tmp_path / "home"
    (home / ".claude" / "sessions").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    def write(status, sid=SID, pid=os.getpid()):
        (home / ".claude" / "sessions" / f"{pid}.json").write_text(
            json.dumps({"pid": pid, "sessionId": sid, "status": status}))

    return write


def read_outcome(sessions):
    return json.loads((sessions / SID / "outcome.json").read_text())


# --- The helper itself --------------------------------------------------------

def test_write_outcome_shape(sessions):
    ir.write_outcome(SID, "aborted", "mid_turn_timeout")
    o = read_outcome(sessions)
    assert o["status"] == "aborted"
    assert o["reason"] == "mid_turn_timeout"
    assert isinstance(o["at"], int)
    assert abs(o["at"] - time.time()) < 5


def test_a_second_outcome_replaces_the_first(sessions):
    ir.write_outcome(SID, "aborted", "mid_turn_timeout")
    ir.write_outcome(SID, "ok", "reset_complete")
    assert read_outcome(sessions)["status"] == "ok"


def test_unwritable_outcome_never_raises(tmp_path, monkeypatch):
    # Point the sessions root at a FILE so every mkdir/write under it fails.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    monkeypatch.setenv("CLAUDE_SESSIONS_DIR", str(blocked))
    ir.write_outcome(SID, "ok", "reset_complete")  # must swallow, not raise


# --- Terminal paths through main() --------------------------------------------

def test_abort_before_lock_records_an_outcome(sessions, monkeypatch):
    monkeypatch.setattr(ir, "session_pid", lambda start=None: None)
    assert ir.main(["--sid", SID]) == 1
    o = read_outcome(sessions)
    assert o["status"] == "aborted"
    assert o["reason"] == "no_session_pid"


def test_invalid_sid_writes_no_outcome_anywhere(sessions):
    # The sid IS the path component the record would be filed under; an invalid
    # one has no valid session dir to write into.
    assert ir.main(["--sid", "../escape"]) == 1
    assert list(sessions.rglob("outcome.json")) == []


def test_a_lock_bounce_refuses_loudly_but_writes_no_outcome(sessions):
    # Losing the flock is not an attempt, it is a bounce off someone else's, so
    # it leaves no machine record — only the human-forensic REFUSING line. The
    # refusal itself is unchanged: non-zero, and nothing touched.
    d = sessions / SID
    d.mkdir(parents=True)
    fd = os.open(str(d / "reset.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert ir.main(["--sid", SID, "--session-pid", str(os.getpid())]) == 1
        assert not (d / "outcome.json").exists()
        assert "REFUSING" in (sessions / "inplace-reset.log").read_text()
    finally:
        os.close(fd)


def test_a_lock_bounce_cannot_clobber_the_holders_outcome(sessions):
    # THE RACE THIS PATH EXISTS TO AVOID. flock and the old write_outcome call
    # were milliseconds apart; a holder finishing inside that gap had its real
    # terminal record replaced by the bouncer's "lock_held" — and since 8bbdb9b
    # a lock_held record buys NO re-arm, so the crossing silently lost the retry
    # the holder's abort had earned. Here the holder's record is already on disk
    # (the same ordering, at its worst) and must survive untouched.
    d = sessions / SID
    d.mkdir(parents=True)
    fd = os.open(str(d / "reset.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        ir.write_outcome(SID, "aborted", "mid_turn_timeout")   # the holder finishes
        holder = read_outcome(sessions)
        assert ir.main(["--sid", SID, "--session-pid", str(os.getpid())]) == 1
        assert read_outcome(sessions) == holder
    finally:
        os.close(fd)


# --- End-to-end through the kitty seam ----------------------------------------
#
# Only send_text/kitty_socket/resolve_window are stubbed — the drain, the
# handoff wait, the turn gate, the clear confirmation and the marker handling
# all run for real against the fake registry.

def drive_main(monkeypatch, sessions, registry_write, *, still_mid_turn):
    sent = []
    handoff = ir.handoff_path(SID)

    def fake_send(socket, wid, spid, text, submit=False):
        sent.append(text)
        if len(sent) == 1:
            # The session "writes" its handoff at the Write tool call.
            handoff.parent.mkdir(parents=True, exist_ok=True)
            handoff.write_text("# mission\n")
        if text == "/clear" and not still_mid_turn:
            # A real clear starts a NEW session id; the registry is the proof.
            registry_write("idle", sid="post-clear-new-sid")
        return None

    monkeypatch.setattr(ir, "kitty_socket", lambda spid: "fake-sock")
    monkeypatch.setattr(ir, "resolve_window", lambda socket, spid: (7, None))
    monkeypatch.setattr(ir, "send_text", fake_send)
    monkeypatch.setattr(ir.time, "sleep", lambda _s: None)
    if still_mid_turn:
        registry_write("busy")
        # Collapse the (unchanged) 120s turn gate so the test is instant.
        monkeypatch.setattr(ir, "CLEAR_IDLE_WAIT_SECONDS", 0)
    else:
        registry_write("idle")
    rc = ir.main(["--sid", SID, "--session-pid", str(os.getpid())])
    return rc, sent


def test_mid_turn_abort_writes_outcome_and_keeps_marker(
        sessions, registry, monkeypatch):
    # The dominant abort shape in the corpus (7 of ~21): handoff written, but
    # the session is still mid-turn when the turn gate gives up.
    (sessions / SID).mkdir(parents=True, exist_ok=True)
    (sessions / SID / "request.json").write_text("{}")
    rc, sent = drive_main(monkeypatch, sessions, registry, still_mid_turn=True)
    assert rc == 1
    o = read_outcome(sessions)
    assert o["status"] == "aborted"
    assert o["reason"] == "mid_turn_timeout"
    # Abort interplay unchanged: the marker is deliberately KEPT (idle-handoff
    # picks it up after session exit) and no /clear was ever sent.
    assert (sessions / SID / "request.json").exists()
    assert "/clear" not in sent


def test_successful_reset_writes_ok_outcome_and_consumes_marker(
        sessions, registry, monkeypatch):
    (sessions / SID).mkdir(parents=True, exist_ok=True)
    (sessions / SID / "request.json").write_text("{}")
    rc, sent = drive_main(monkeypatch, sessions, registry, still_mid_turn=False)
    assert rc == 0
    assert read_outcome(sessions)["status"] == "ok"
    # Success interplay unchanged: marker renamed so the fork path cannot
    # double-act on this session.
    assert not (sessions / SID / "request.json").exists()
    assert (sessions / SID / "consumed.json").exists()
    assert sent[1] == "/clear"
