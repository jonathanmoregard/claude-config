#!/usr/bin/env python3
"""
UserPromptSubmit hook: gentle wellbeing nudges based on daily session activity.

Three triggers:
  1. late-night: after 20:00 with >= 5h active time today
  2. marathon:   >= 8h active time today (any hour)
  3. continuous: current unbroken work block >= 1.5h (no >= 5min break)

Active time is computed from real user prompts only: each prompt claims a
5-minute forward window; overlapping windows merge; gaps > 5 minutes between
prompts count as a break. On any trigger the user is also nudged to do
breathwork. Late/marathon and continuous triggers have independent cooldowns.

The hook's output is an instruction to the model, not the nudge itself, so it
spells out how the nudge must reach the user: its own line, the trigger named,
the word "breathwork" kept, the practice named in the user's own words:
"wim hof breathwork (or a minute of mindful breathing if in a rush)" — no
breathing instructions (2026-10-02). A vague instruction got relayed as "a
short breath pause", which the user read as noise.
"""
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

TMP = Path.home() / ".claude" / "tmp"
STAMP_LATE_MARATHON = TMP / ".wellbeing-late-marathon-stamp"
STAMP_CONTINUOUS = TMP / ".wellbeing-continuous-stamp"
CACHE = TMP / ".wellbeing-cache.json"
PROJECTS_DIR = Path.home() / ".claude" / "projects"

COOLDOWN_LATE_MARATHON = 3600   # 60 min
COOLDOWN_CONTINUOUS = 1800      # 30 min
CACHE_SECONDS = 300             # reparse at most every 5 min

MIN_HOUR = 20
LATE_NIGHT_HOURS = 5
MAX_HOURS_ANY_TIME = 8
CONTINUOUS_BLOCK_HOURS = 1.5
IDLE_SECONDS = 5 * 60           # gaps longer than this = break


def _compute_hours(now):
    """Return (total_hours, continuous_hours) for today, cached."""
    if CACHE.exists():
        try:
            cached = json.loads(CACHE.read_text())
            if time.time() - cached.get("ts", 0) < CACHE_SECONDS:
                return cached["total_hours"], cached["continuous_hours"]
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
                    # Exclude SDK-driven / background automation sessions: these
                    # aren't real user presence (hooks, schedule, headless agents).
                    if entry.get("entrypoint") == "sdk-cli":
                        continue
                    if "/homunculus/" in entry.get("cwd", ""):
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

    # Current continuous block: the last merged interval if it extends to ~now.
    continuous_hours = 0.0
    if merged:
        last_start, last_end = merged[-1]
        if last_end >= now - idle:
            continuous_hours = (last_end - last_start).total_seconds() / 3600

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({
        "ts": time.time(),
        "total_hours": total_hours,
        "continuous_hours": continuous_hours,
    }))

    return total_hours, continuous_hours


def _cooldown_ok(stamp, seconds):
    if not stamp.exists():
        return True
    try:
        last = float(stamp.read_text().strip())
        return time.time() - last >= seconds
    except (ValueError, OSError):
        return True


def _fmt(hours):
    h = int(hours)
    m = int((hours - h) * 60)
    return f"{h}h{m:02d}m"


def main():
    now = datetime.now()

    TMP.mkdir(parents=True, exist_ok=True)

    total_hours, continuous_hours = _compute_hours(now)

    is_late = now.hour >= MIN_HOUR and total_hours >= LATE_NIGHT_HOURS
    is_marathon = total_hours >= MAX_HOURS_ANY_TIME
    is_continuous = continuous_hours >= CONTINUOUS_BLOCK_HOURS

    late_marathon_fires = (is_late or is_marathon) and _cooldown_ok(
        STAMP_LATE_MARATHON, COOLDOWN_LATE_MARATHON
    )
    continuous_fires = is_continuous and _cooldown_ok(
        STAMP_CONTINUOUS, COOLDOWN_CONTINUOUS
    )

    if not (late_marathon_fires or continuous_fires):
        sys.exit(0)

    reasons = []
    if late_marathon_fires:
        if is_late and is_marathon:
            reasons.append(
                f"It's after {MIN_HOUR}:00 and the user has {_fmt(total_hours)} "
                f"of active session time today (cap is {MAX_HOURS_ANY_TIME}h)."
            )
        elif is_marathon:
            reasons.append(
                f"The user has {_fmt(total_hours)} of active session time today "
                f"(cap is {MAX_HOURS_ANY_TIME}h)."
            )
        else:
            reasons.append(
                f"It's after {MIN_HOUR}:00 ({_fmt(total_hours)} active today)."
            )
        STAMP_LATE_MARATHON.write_text(str(time.time()))

    if continuous_fires:
        reasons.append(
            f"The user has been working for {_fmt(continuous_hours)} straight "
            f"without a 5-minute break (limit is {CONTINUOUS_BLOCK_HOURS}h)."
        )
        STAMP_CONTINUOUS.write_text(str(time.time()))

    print(
        " ".join(reasons)
        + " Relay this to the user as a wellbeing nudge on its own line, stated"
        " plainly: name the trigger (hours active today, after 20:00, or the"
        " unbroken block) and suggest, in these words, 'wim hof breathwork (or"
        " a minute of mindful breathing if in a rush)'. No instructions on how"
        " to breathe. Do not soften it to 'take a breath' or 'a short pause', do"
        " not paraphrase 'breathwork' away, and do not fold it into another"
        " sentence. It is a nudge, not an order: one clear line, then carry on."
    )


if __name__ == "__main__":
    main()
