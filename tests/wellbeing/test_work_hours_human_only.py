#!/usr/bin/env python3
"""
Plain-assert tests: work-hours-nudge.py counts only the human's active time
(no pytest on this machine; pytest also collects the test_ functions).

Run:  python3 tests/wellbeing/test_work_hours_human_only.py

Invariant: a turn the harness started on its own -- a background task's
`<task-notification>` (origin.kind "task-notification"), or the summary that
opens a session after compaction (isCompactSummary) -- is not the user being
at the keyboard. A session left running overnight on background agents must
not reach the 8h cap by morning, nor read as an unbroken work block.

Entry shapes are copied from real Claude Code 2.1.321 transcripts.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The scripts live under runtime/, the tree projected into ~/.claude; this
# suite does not, so it reaches across rather than up.
WELLBEING = Path(__file__).resolve().parents[2] / "runtime" / "wellbeing"
# A fixed past day: transcript files written now have an mtime after its
# midnight, which is all the reader's "touched today" check asks.
DAY = datetime(2026, 3, 10)


def _load():
    spec = importlib.util.spec_from_file_location(
        "work_hours_nudge", WELLBEING / "work-hours-nudge.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iso(local_naive: datetime) -> str:
    return local_naive.astimezone().astimezone(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )


def _human(t: datetime) -> dict:
    return {"type": "user", "timestamp": _iso(t), "cwd": "/home/u/p",
            "entrypoint": "cli", "promptSource": "typed",
            "origin": {"kind": "human"},
            "message": {"role": "user", "content": "go on"}}


def _notification(t: datetime) -> dict:
    return {"type": "user", "timestamp": _iso(t), "cwd": "/home/u/p",
            "entrypoint": "cli", "promptSource": "system",
            "origin": {"kind": "task-notification", "producer": "session-task"},
            "message": {"role": "user",
                        "content": "<task-notification>\n<task-id>b1</task-id>"}}


def _compact_summary(t: datetime) -> dict:
    return {"type": "user", "timestamp": _iso(t), "cwd": "/home/u/p",
            "entrypoint": "cli", "isCompactSummary": True,
            "isVisibleInTranscriptOnly": True,
            "message": {"role": "user", "content":
                        "This session is being continued from a previous"
                        " conversation that ran out of context."}}


def _every(start: datetime, end: datetime, minutes: int, make) -> list[dict]:
    out, t = [], start
    while t <= end:
        out.append(make(t))
        t += timedelta(minutes=minutes)
    return out


def _at(h: int, m: int = 0) -> datetime:
    return DAY.replace(hour=h, minute=m)


def _run(entries: list[dict], now: datetime) -> tuple[float, float, str]:
    """(total_hours, continuous_hours, hook stdout) for one transcript at `now`."""
    mod = _load()

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        mod.datetime = _Clock
        mod.TMP = tmp / "tmp"
        mod.STAMP_LATE_MARATHON = mod.TMP / "lm"
        mod.STAMP_CONTINUOUS = mod.TMP / "cont"
        mod.CACHE = mod.TMP / "cache.json"
        mod.PROJECTS_DIR = tmp / "projects"
        mod.CODEX_SESSIONS_DIR = tmp / "codex"
        path = mod.PROJECTS_DIR / "p" / "s.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("".join(json.dumps(e) + "\n" for e in entries))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                mod.main()
            except SystemExit as e:
                assert e.code in (0, None), e.code
        cached = json.loads(mod.CACHE.read_text())
        return cached["total_hours"], cached["continuous_hours"], out.getvalue()


def test_overnight_machine_only_session_does_not_trigger():
    """Background agents report every few minutes from midnight to 08:40,
    with a compaction in between; nobody typed anything."""
    entries = (_every(_at(0, 0), _at(8, 40), 4, _notification)
               + [_compact_summary(_at(3, 2))])
    total, continuous, out = _run(entries, now=_at(8, 43))
    assert total == 0.0, total
    assert continuous == 0.0, continuous
    assert out == "", out


def test_real_eight_hour_human_day_triggers():
    """The user typed every 4 minutes from 09:00 to 17:04: 8h09m."""
    entries = _every(_at(9, 0), _at(17, 4), 4, _human)
    total, _, out = _run(entries, now=_at(17, 6))
    assert total >= 8, total
    assert "of active session time today (cap is 8h)" in out, out
    assert "wim hof breathwork" in out, out


def test_mixed_day_counts_only_the_human_part():
    """Machine turns all night, the user at the keyboard 08:00-10:00, then the
    user leaves and background agents keep reporting until noon. Only the two
    human hours count, and the noon block is not 'unbroken work'."""
    entries = (_every(_at(0, 0), _at(7, 56), 4, _notification)
               + _every(_at(8, 0), _at(10, 0), 4, _human)
               + _every(_at(10, 3), _at(12, 0), 3, _notification))
    total, continuous, out = _run(entries, now=_at(12, 1))
    assert abs(total - (2 + 5 / 60)) < 1e-6, total
    assert continuous == 0.0, continuous
    assert out == "", out


def test_human_block_with_notifications_interleaved_is_still_unbroken():
    """Notifications landing during a 2h human block neither extend it nor
    break it: the unbroken-block trigger still fires on the human's 2h."""
    entries = (_every(_at(13, 0), _at(15, 0), 4, _human)
               + _every(_at(13, 2), _at(15, 2), 10, _notification))
    total, continuous, out = _run(entries, now=_at(15, 2))
    assert abs(continuous - (2 + 5 / 60)) < 1e-6, continuous
    assert abs(total - continuous) < 1e-6, total
    assert "straight without a 5-minute break" in out, out


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
