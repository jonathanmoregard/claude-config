#!/usr/bin/env python3
"""
Cron-friendly TickTick habit tracker.

Fetches all habits + today's checkins, decides which are due today and which
are still unchecked, and writes ~/.claude/tmp/.habit-cache.json for the
habit-nudge UserPromptSubmit hook to read.

Cache shape:
    {
        "ts": <unix>,
        "date": "YYYY-MM-DD",
        "habits": [
            {
                "id": "...",
                "name": "Meditate",
                "due_today": true,
                "checked_today": false,
                "last_checkin_date": "2026-04-17",
                "preferred_time_hhmm": "12:20",
                "preferred_hour": 12
            },
            ...
        ]
    }
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

try:
    import tomllib  # py 3.11+
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

from dateutil.rrule import rrulestr  # type: ignore[import-untyped]

sys.path.insert(0, str(Path(__file__).parent.parent))
from todo.backends.ticktick import TickTickBackend  # noqa: E402
from todo.backends.base import BackendError  # noqa: E402

CACHE = Path.home() / ".claude" / "tmp" / ".habit-cache.json"
CONFIG = Path(__file__).parent / "habits.toml"
CHECKIN_STATUS_DONE = 2
DEFAULT_NUDGE_HOUR_FALLBACK = 18


def _is_due(rrule_str: str, target_start_date_int: int, on_date: date) -> bool:
    if not rrule_str:
        return False
    try:
        anchor = datetime.strptime(str(target_start_date_int), "%Y%m%d")
    except (ValueError, TypeError):
        return False
    try:
        rule = rrulestr(rrule_str, dtstart=anchor)
    except (ValueError, TypeError):
        return False
    day_start = datetime(on_date.year, on_date.month, on_date.day)
    day_end = day_start + timedelta(days=1)
    return any(day_start <= o < day_end for o in rule.between(day_start - timedelta(seconds=1), day_end, inc=True))


def _last_done_stamp(checkins_for_habit: list[dict]) -> int | None:
    stamps = [c.get("stamp") for c in checkins_for_habit if c.get("status") == CHECKIN_STATUS_DONE]
    stamps = [s for s in stamps if isinstance(s, int)]
    return max(stamps) if stamps else None


def _stamp_to_iso(stamp: int) -> str:
    s = str(stamp)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def _load_config() -> tuple[dict[str, int], int]:
    """Return (deadlines_by_name, default_nudge_hour)."""
    if not CONFIG.exists() or tomllib is None:
        return {}, DEFAULT_NUDGE_HOUR_FALLBACK
    try:
        with open(CONFIG, "rb") as f:
            cfg = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}, DEFAULT_NUDGE_HOUR_FALLBACK
    deadlines = cfg.get("deadlines", {}) or {}
    deadlines = {k: int(v) for k, v in deadlines.items() if isinstance(v, int)}
    default_hour = cfg.get("default_nudge_hour", DEFAULT_NUDGE_HOUR_FALLBACK)
    if not isinstance(default_hour, int):
        default_hour = DEFAULT_NUDGE_HOUR_FALLBACK
    return deadlines, default_hour


def _preferred_time(habit: dict) -> tuple[str | None, int | None]:
    reminders = habit.get("reminders") or []
    for r in reminders:
        if isinstance(r, str) and len(r) >= 4 and r[2] == ":":
            try:
                hour = int(r[:2])
                return r, hour
            except ValueError:
                continue
    return None, None


def build_state(today: date, habits: list[dict], checkin_docs: list[dict]) -> dict:
    today_stamp = int(today.strftime("%Y%m%d"))
    by_habit: dict[str, list[dict]] = {}
    for doc in checkin_docs:
        hid = doc.get("habitId")
        if not hid:
            continue
        by_habit.setdefault(hid, []).extend(doc.get("checkins") or [])

    deadlines, default_nudge_hour = _load_config()

    out_habits = []
    for h in habits:
        if h.get("status") != 0:  # 0 = active; non-zero = archived/disabled
            continue
        hid = h["id"]
        name = h.get("name", "")
        checkins = by_habit.get(hid, [])
        last_stamp = _last_done_stamp(checkins)
        checked_today = any(
            c.get("stamp") == today_stamp and c.get("status") == CHECKIN_STATUS_DONE
            for c in checkins
        )
        due = _is_due(h.get("repeatRule", ""), h.get("targetStartDate", 0), today)
        pref_str, pref_hour = _preferred_time(h)
        deadline_hour = deadlines.get(name)
        out_habits.append({
            "id": hid,
            "name": name,
            "due_today": due,
            "checked_today": checked_today,
            "last_checkin_date": _stamp_to_iso(last_stamp) if last_stamp else None,
            "preferred_time_hhmm": pref_str,
            "preferred_hour": pref_hour,
            "deadline_hour": deadline_hour,
        })

    return {
        "ts": time.time(),
        "date": today.isoformat(),
        "default_nudge_hour": default_nudge_hour,
        "habits": out_habits,
    }


def main() -> int:
    today = date.today()
    backend = TickTickBackend.from_env()

    try:
        habits = backend.list_habits()
    except BackendError as e:
        print(f"[habit-tracker] list_habits failed: {e}", file=sys.stderr)
        return 1

    ids = [h["id"] for h in habits if h.get("status") == 0]
    # Pull a 2-day window so we can show "last_checkin_date" if it was yesterday.
    yesterday = today - timedelta(days=1)
    try:
        checkins = backend.get_checkins(
            ids,
            yesterday.strftime("%Y%m%d"),
            today.strftime("%Y%m%d"),
        )
    except BackendError as e:
        print(f"[habit-tracker] get_checkins failed: {e}", file=sys.stderr)
        return 1

    state = build_state(today, habits, checkins)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    print(json.dumps(state, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
