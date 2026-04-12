#!/usr/bin/env python3
"""
UserPromptSubmit hook: warns if it's after 20:00 and the user has 5+ hours
of actual Claude session time today. Counts real active time by summing
per-session durations (first message to last message in each jsonl).
60-minute cooldown between warnings.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

STAMP = Path.home() / ".claude" / "tmp" / ".late-night-warn-stamp"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
COOLDOWN_SECONDS = 3600
MIN_ACTIVE_HOURS = 5
MIN_HOUR = 20


def main():
    now = datetime.now()
    if now.hour < MIN_HOUR:
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

    # Find all jsonl files modified today
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_ts = today_start.timestamp()

    # Collect (start, end) intervals per session, then merge overlaps
    # so parallel sessions count as wall-clock time, not summed time.
    intervals = []

    for jsonl in PROJECTS_DIR.rglob("*.jsonl"):
        if "subagents" in jsonl.parts:
            continue
        try:
            if jsonl.stat().st_mtime < today_ts:
                continue
        except OSError:
            continue

        first_ts = None
        last_ts = None
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
                    if first_ts is None or ts_local < first_ts:
                        first_ts = ts_local
                    if last_ts is None or ts_local > last_ts:
                        last_ts = ts_local
        except OSError:
            continue

        if first_ts and last_ts and last_ts > first_ts:
            intervals.append((first_ts, last_ts))

    # Merge overlapping intervals
    intervals.sort()
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    total_seconds = sum((e - s).total_seconds() for s, e in merged)

    total_hours = total_seconds / 3600

    if total_hours < MIN_ACTIVE_HOURS:
        sys.exit(0)

    STAMP.write_text(str(time.time()))

    hours = int(total_hours)
    minutes = int((total_hours - hours) * 60)
    print(
        f"It's after {MIN_HOUR}:00 and the user has {hours}h{minutes:02d}m of active session time today. "
        f"Gently remind the user — their wellbeing goals include mindful breaks and not marathoning. "
        f"This is a nudge, not an order."
    )


if __name__ == "__main__":
    main()
