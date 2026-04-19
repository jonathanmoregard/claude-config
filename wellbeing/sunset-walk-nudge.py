#!/usr/bin/env python3
"""
UserPromptSubmit hook: nudge user to take a sunset walk when weather is good
and the sunset window is approaching or active.

Reads ~/.claude/tmp/.sunset-walk-cache.json (written by sunset-walk-tracker
cron) and emits a single context block when:
  * good == true
  * now is within [window_start - PRE_NUDGE_MIN, window_end]
  * cooldown for this sunset_date not yet fired

State file: ~/.claude/tmp/.sunset-walk-fired.json
    {"date": "YYYY-MM-DD"}
Refreshed once per qualifying day so the hook fires at most once per sunset.
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

CACHE = Path.home() / ".claude" / "tmp" / ".sunset-walk-cache.json"
FIRED = Path.home() / ".claude" / "tmp" / ".sunset-walk-fired.json"

PRE_NUDGE_MIN = 30  # nudge starts 30min before walk window opens
CACHE_MAX_AGE_SECONDS = 6 * 3600


def _load_cache() -> dict | None:
    if not CACHE.exists():
        return None
    try:
        return json.loads(CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _already_fired(today: str) -> bool:
    if not FIRED.exists():
        return False
    try:
        return json.loads(FIRED.read_text()).get("date") == today
    except (OSError, json.JSONDecodeError):
        return False


def _mark_fired(today: str) -> None:
    FIRED.parent.mkdir(parents=True, exist_ok=True)
    FIRED.write_text(json.dumps({"date": today}))


def main() -> int:
    now = datetime.now()
    cache = _load_cache()

    if cache is None:
        if now.hour >= 10:
            print(
                "[wellbeing] sunset-walk-tracker cache missing — sunset nudges "
                "are silent. Check ~/.claude/logs/sunset-walk-tracker.log."
            )
        return 0

    if now.timestamp() - cache.get("ts", 0) > CACHE_MAX_AGE_SECONDS:
        if now.hour >= 10:
            print(
                "[wellbeing] sunset-walk-tracker cache is stale. Nudges silent. "
                "Check ~/.claude/logs/sunset-walk-tracker.log."
            )
        return 0

    if not cache.get("good"):
        return 0

    sunset_date = cache.get("sunset_date")
    if not sunset_date:
        return 0

    try:
        window_start = datetime.fromisoformat(cache["window_start_iso"])
        window_end = datetime.fromisoformat(cache["window_end_iso"])
    except (KeyError, ValueError):
        return 0

    pre_start = window_start - timedelta(minutes=PRE_NUDGE_MIN)
    if not (pre_start <= now <= window_end):
        return 0

    if _already_fired(sunset_date):
        return 0

    cloud = cache.get("cloud_cover_pct")
    sunset_hhmm = cache.get("sunset_hhmm", "")
    cloud_str = f"{cloud}%" if cloud is not None else "low"

    print(
        f"Sunset is at {sunset_hhmm} in the configured place — cloud cover {cloud_str}, "
        f"clear visibility, walk window {window_start.strftime('%H:%M')}-"
        f"{window_end.strftime('%H:%M')}. Gently nudge the user to step outside "
        "for the sunset light. This is a wellbeing nudge, not an order."
    )
    _mark_fired(sunset_date)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
