#!/usr/bin/env python3
"""
UserPromptSubmit hook: nudge user about TickTick habits forgotten today.

Reads ~/.claude/tmp/.habit-cache.json (written by habit-tracker.py cron) and
emits a single context block when:
  * habit is due_today AND not checked_today
  * time-of-day gate passed:
      - if habit has a preferred_hour, fire 1h after that hour (grace window)
      - otherwise fire after DEFAULT_NUDGE_HOUR (evening fallback)
  * per-habit-per-day cooldown not yet fired

State file: ~/.claude/tmp/.habit-nudge-fired.json
    {"YYYY-MM-DD": ["habit_id_1", "habit_id_2"]}
Old dates are pruned.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

CACHE = Path.home() / ".claude" / "tmp" / ".habit-cache.json"
FIRED = Path.home() / ".claude" / "tmp" / ".habit-nudge-fired.json"

DEADLINE_PRE_HOURS = 1   # nudge starts 1h before deadline_hour
DEADLINE_POST_HOURS = 1  # second escalated nudge fires 1h after deadline_hour
GRACE_HOURS_AFTER_PREFERRED = 1
DEFAULT_NUDGE_HOUR_FALLBACK = 18
CACHE_MAX_AGE_SECONDS = 6 * 3600


def _load_cache() -> dict | None:
    if not CACHE.exists():
        return None
    try:
        data = json.loads(CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data


def _load_fired() -> dict[str, list[str]]:
    if not FIRED.exists():
        return {}
    try:
        return json.loads(FIRED.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_fired(state: dict[str, list[str]], today: str) -> None:
    pruned = {today: state.get(today, [])}
    FIRED.parent.mkdir(parents=True, exist_ok=True)
    FIRED.write_text(json.dumps(pruned))


def _gate_state(habit: dict, default_nudge_hour: int, now: datetime) -> tuple[bool, str]:
    """Return (should_fire, urgency).

    Resolution order:
      1. deadline_hour from local config — fires at deadline-1h ("warn"),
         then again at deadline+1h ("overdue") if still unchecked.
      2. preferred_hour from TickTick reminders[] — fires at preferred+1h.
      3. default_nudge_hour from config (or 18) — fires at that hour.
    """
    deadline = habit.get("deadline_hour")
    if isinstance(deadline, int):
        if now.hour >= deadline + DEADLINE_POST_HOURS:
            return True, "overdue"
        if now.hour >= deadline - DEADLINE_PRE_HOURS:
            return True, "warn"
        return False, ""
    pref = habit.get("preferred_hour")
    if isinstance(pref, int):
        return now.hour >= pref + GRACE_HOURS_AFTER_PREFERRED, "warn"
    return now.hour >= default_nudge_hour, "warn"


def main() -> int:
    cache = _load_cache()
    now = datetime.now()
    today = now.date().isoformat()

    if not cache:
        if now.hour >= 10:
            print(
                "[wellbeing] habit-tracker cache missing — TickTick habit nudges "
                "are silent. If this persists, check ~/.claude/logs/habit-tracker.log "
                "(token may need re-auth via `~/.claude/ticktick-login`)."
            )
        return 0

    if cache.get("date") != today:
        if now.hour >= 10:
            stale_date = cache.get("date", "unknown")
            print(
                f"[wellbeing] habit-tracker cache is stale (date={stale_date}, "
                f"today={today}). Nudges silent. Check "
                "~/.claude/logs/habit-tracker.log — TickTick token may need "
                "re-auth via `~/.claude/ticktick-login`."
            )
        return 0

    cache_ts = cache.get("ts", 0)
    if now.timestamp() - cache_ts > CACHE_MAX_AGE_SECONDS:
        return 0

    fired = _load_fired()
    # fired_today maps habit_id -> last urgency stage fired ("warn"|"overdue").
    raw = fired.get(today, [])
    if isinstance(raw, list):  # legacy format: list of ids = warn-only
        fired_today: dict[str, str] = {hid: "warn" for hid in raw}
    elif isinstance(raw, dict):
        fired_today = dict(raw)
    else:
        fired_today = {}

    default_nudge_hour = cache.get("default_nudge_hour", DEFAULT_NUDGE_HOUR_FALLBACK)

    forgotten = []
    for h in cache.get("habits", []):
        if not h.get("due_today") or h.get("checked_today"):
            continue
        should_fire, urgency = _gate_state(h, default_nudge_hour, now)
        if not should_fire:
            continue
        already = fired_today.get(h["id"])
        # Warn fires once. Overdue fires once after warn (not duplicated).
        if already == urgency or already == "overdue":
            continue
        forgotten.append((h, urgency))
        fired_today[h["id"]] = urgency

    if not forgotten:
        return 0

    warn_parts = []
    overdue_parts = []
    for h, urgency in forgotten:
        pref = h.get("preferred_time_hhmm")
        deadline = h.get("deadline_hour")
        last = h.get("last_checkin_date")
        suffix_bits = []
        if isinstance(deadline, int):
            suffix_bits.append(f"deadline {deadline:02d}:00")
        elif pref:
            suffix_bits.append(f"usually {pref}")
        if last:
            suffix_bits.append(f"last done {last}")
        suffix = f" ({', '.join(suffix_bits)})" if suffix_bits else ""
        line = f"{h['name']}{suffix}"
        if urgency == "overdue":
            overdue_parts.append(line)
        else:
            warn_parts.append(line)

    fired[today] = fired_today
    _save_fired(fired, today)

    msgs = []
    if warn_parts:
        msgs.append(
            "Habits due today and not yet checked in TickTick: "
            + "; ".join(warn_parts) + "."
        )
    if overdue_parts:
        msgs.append(
            "Habits past their deadline today and still unchecked: "
            + "; ".join(overdue_parts) + "."
        )
    msgs.append(
        "Gently remind the user — these are the user's daily habits and skipping "
        "them often correlates with overwork. Tone: warm nudge, not a list."
    )
    print(" ".join(msgs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
