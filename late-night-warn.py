#!/usr/bin/env python3
"""
UserPromptSubmit hook: warns if it's after 20:00 and the user has 5+ hours
of actual Claude session time today. Active time is computed from real user
prompts only: each prompt claims a 4-minute forward window; overlapping
windows merge, and gaps >4 minutes between prompts count as idle.
60-minute cooldown between warnings.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

STAMP = Path.home() / ".claude" / "tmp" / ".late-night-warn-stamp"
CACHE = Path.home() / ".claude" / "tmp" / ".late-night-cache.json"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
COOLDOWN_SECONDS = 3600
CACHE_SECONDS = 300  # reparse at most every 5 minutes
MAX_HOURS_ANY_TIME = 9
MIN_HOUR = 20
IDLE_SECONDS = 4 * 60  # gaps longer than this between user prompts = idle


def _get_active_hours(now):
    """Return wall-clock active hours today, cached for CACHE_SECONDS."""
    if CACHE.exists():
        try:
            cached = json.loads(CACHE.read_text())
            if time.time() - cached["ts"] < CACHE_SECONDS:
                return cached["hours"]
        except (json.JSONDecodeError, KeyError, OSError):
            pass

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_ts = today_start.timestamp()
    prompt_times = []

    for jsonl in PROJECTS_DIR.rglob("*.jsonl"):
        if "subagents" in jsonl.parts:
            continue
        try:
            if jsonl.stat().st_mtime < today_ts:
                continue
        except OSError:
            continue

        try:
            with open(jsonl) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") != "user":
                        continue
                    if entry.get("isSidechain") or entry.get("isMeta"):
                        continue
                    content = entry.get("message", {}).get("content")
                    if isinstance(content, list) and any(
                        isinstance(c, dict) and c.get("type") == "tool_result"
                        for c in content
                    ):
                        continue
                    ts_str = entry.get("timestamp")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        ts_local = ts.astimezone().replace(tzinfo=None)
                    except (ValueError, TypeError):
                        continue
                    if ts_local < today_start:
                        continue
                    prompt_times.append(ts_local)
        except OSError:
            continue

    # Each user prompt claims a forward IDLE_SECONDS window. Merge overlaps
    # across all sessions so concurrent terminals don't double-count.
    idle = timedelta(seconds=IDLE_SECONDS)
    intervals = sorted((t, t + idle) for t in prompt_times)
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    total_hours = sum((e - s).total_seconds() for s, e in merged) / 3600

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"ts": time.time(), "hours": total_hours}))

    return total_hours


def main():
    now = datetime.now()

    # Cheap early exit: if it's before 20:00 and fewer than MAX_HOURS_ANY_TIME
    # have elapsed since midnight, 9h is impossible — skip the expensive parse.
    hours_since_midnight = now.hour + now.minute / 60
    if now.hour < MIN_HOUR and hours_since_midnight < MAX_HOURS_ANY_TIME:
        sys.exit(0)

    # Cooldown check
    STAMP.parent.mkdir(parents=True, exist_ok=True)
    if STAMP.exists():
        try:
            last = float(STAMP.read_text().strip())
            if time.time() - last < COOLDOWN_SECONDS:
                sys.exit(0)
        except (ValueError, OSError):
            pass

    total_hours = _get_active_hours(now)

    is_late = now.hour >= MIN_HOUR
    is_marathon = total_hours >= MAX_HOURS_ANY_TIME

    if not is_late and not is_marathon:
        sys.exit(0)

    STAMP.write_text(str(time.time()))

    hours = int(total_hours)
    minutes = int((total_hours - hours) * 60)

    if is_late and is_marathon:
        reason = f"It's after {MIN_HOUR}:00 and the user has {hours}h{minutes:02d}m of active session time today."
    elif is_late:
        reason = f"It's after {MIN_HOUR}:00 ({hours}h{minutes:02d}m active today)."
    else:
        reason = f"The user has been active for {hours}h{minutes:02d}m today."

    print(
        f"{reason} "
        f"Gently remind the user — their wellbeing goals include mindful breaks and not marathoning. "
        f"This is a nudge, not an order."
    )


if __name__ == "__main__":
    main()
