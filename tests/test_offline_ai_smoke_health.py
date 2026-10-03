"""offline-ai-smoke-health: silent when the weekly smoke passed recently, loud on
fail or when no pass is on record for 10 days, and a missing file means nothing.

The state file is written by nixos-config's ``offline-ai-smoke`` user timer
(``~/.local/state/offline-ai/smoke.json``). The hook's contract is "run this
file, read its JSON", so every test here shells out to it the way the harness
does, with ``OFFLINE_AI_SMOKE_STATE`` pointing at a temp file.

Two silent failure modes, both tested: speaking on a healthy machine (burns the
one attention budget a SessionStart hook has) and staying quiet when it could
not tell (an unreadable file must be announced, a missing one must not).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import REPO_ROOT

HOOK = Path(REPO_ROOT) / "hooks" / "offline-ai-smoke-health.py"


def _run(tmp_path: Path, state) -> subprocess.CompletedProcess:
    path = tmp_path / "smoke.json"
    if state is not None:
        path.write_text(state if isinstance(state, str) else json.dumps(state))
    env = dict(os.environ)
    env["OFFLINE_AI_SMOKE_STATE"] = str(path)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def run(tmp_path: Path, state):
    """Return the parsed announcement, or None when the hook stayed silent."""
    proc = _run(tmp_path, state)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def test_missing_file_is_silent(tmp_path):
    """No file: the timer has not had its first night, or this host has no offline-ai."""
    proc = _run(tmp_path, None)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", f"the hook spoke with no state file: {proc.stdout!r}"


def test_recent_pass_is_silent(tmp_path):
    proc = _run(tmp_path, {"status": "ok", "last_ok": ago(2), "last_run": ago(2)})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", f"the hook spoke after a recent pass: {proc.stdout!r}"


def test_fail_is_announced_with_reason(tmp_path):
    out = run(tmp_path, {"status": "fail", "last_run": ago(1), "last_ok": ago(8),
                         "reason": "model not loadable or unreachable: did not become ready"})
    assert out is not None, "the hook went silent on a recorded failure"
    assert "FAILED" in out["systemMessage"]
    assert "did not become ready" in out["systemMessage"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "did not become ready" in out["hookSpecificOutput"]["additionalContext"]


def test_no_pass_for_ten_days_is_announced(tmp_path):
    out = run(tmp_path, {"status": "skipped", "reason": "on battery", "last_ok": ago(11), "last_run": ago(1)})
    assert out is not None, "the hook went silent on a 11-day-old pass"
    assert "has not passed" in out["systemMessage"]
    assert "on battery" in out["systemMessage"]


def test_skipped_recently_after_recent_pass_is_silent(tmp_path):
    proc = _run(tmp_path, {"status": "skipped", "reason": "on battery", "last_ok": ago(5), "last_run": ago(1)})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", f"the hook spoke on a skip after a recent pass: {proc.stdout!r}"


def test_naive_timestamps_are_read_as_utc(tmp_path):
    """A writer that drops the tz suffix must not crash the hook into silence."""
    naive_old = (datetime.now(timezone.utc) - timedelta(days=12)).replace(microsecond=0, tzinfo=None).isoformat()
    out = run(tmp_path, {"status": "skipped", "reason": "on battery", "last_ok": naive_old, "last_run": naive_old})
    assert out is not None, "the hook went silent on a stale naive timestamp"
    assert "has not passed" in out["systemMessage"]


def test_unreadable_file_is_announced(tmp_path):
    out = run(tmp_path, "{not json")
    assert out is not None, "the hook went silent on a file it could not read"
    assert "unreadable" in out["systemMessage"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_never_passed_is_announced_once_the_first_run_is_old(tmp_path):
    # Nightly skips keep moving last_run; without first_run as the anchor a host that
    # never passed would stay silent forever.
    out = run(tmp_path, {"first_run": ago(11), "last_run": ago(0), "last_skip": ago(0),
                         "skip_reason": "on battery"})
    assert "has not passed since never" in out["systemMessage"]
    assert "on battery" in out["systemMessage"]


def test_never_passed_but_young_is_silent(tmp_path):
    assert run(tmp_path, {"first_run": ago(2), "last_run": ago(0), "last_skip": ago(0),
                          "skip_reason": "outside the night window 01-07 (hour 10)"}) is None


def test_hook_is_registered_once_in_session_start():
    """The registration is the half most likely to be lost in a merge."""
    settings = json.loads((Path(REPO_ROOT) / "settings.json").read_text())
    commands = [
        h.get("command", "")
        for entry in settings["hooks"]["SessionStart"]
        for h in entry.get("hooks", [])
    ]
    mine = [c for c in commands if "offline-ai-smoke-health.py" in c]
    assert mine, (
        "the offline-ai smoke health hook is not registered in settings.json "
        f"SessionStart; entries present: {commands}"
    )
    assert len(mine) == 1, f"registered more than once: {mine}"
    assert "||" not in mine[0] and "exit 0" not in mine[0], (
        f"the hook is registered behind a guard clause: {mine[0]!r}"
    )
