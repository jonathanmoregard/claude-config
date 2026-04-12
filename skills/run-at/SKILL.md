---
name: run-at
description: >
  Use when the user wants to defer execution of a task to a future time or
  event. Trigger phrases: "after next limit reset", "after next reset window",
  "run this at [time]", "wait until [time] then do X", "after my limits reset",
  "schedule this for later". Uses /loop + ScheduleWakeup for true one-shot
  deferral — no cron, no repeats.
---

# Run-At: Deferred One-Shot Execution

## When this applies

- "after next reset window, do X Y Z"
- "after next limit reset, run this"
- "wait until [time] then run it"
- "run this at [time]"
- "after my limits reset, [do task]"

## The mechanism: /loop + ScheduleWakeup

`/loop` with dynamic self-pacing + `ScheduleWakeup` = true one-shot deferral.

- `ScheduleWakeup` suspends the session and resumes at the target time
- The loop only repeats if another `ScheduleWakeup` is called at the end
- Don't call it again → loop ends after one wake → truly one-shot
- No cron (repeats), no `at` (not installed), no systemd-run needed

## How to execute

### Step 1: Capture the task

Extract the full task from the user's message. Write it down explicitly — the
session wakes cold and needs the task self-contained in the prompt.

### Step 2: Resolve target time

**For "next limit reset" / "next reset window":**

Read the cached statusline JSON (updated on every API response):

```bash
cat ~/.claude/last-statusline.json | jq -r '.rate_limits.five_hour.resets_at // empty'
```

This returns a **Unix epoch timestamp** (seconds) for when the 5-hour window
resets. There's also `.rate_limits.seven_day.resets_at` for the 7-day window.

Compute delay:

```bash
python3 -c "
import json, time
with open('$HOME/.claude/last-statusline.json') as f:
    data = json.load(f)
resets_at = data.get('rate_limits', {}).get('five_hour', {}).get('resets_at')
if resets_at:
    delay = max(0, int(resets_at - time.time()))
    print(delay)
else:
    print('UNAVAILABLE')
"
```

If the file doesn't exist or returns UNAVAILABLE (session hasn't made an API
call yet, or statusline capture isn't configured), **ask the user** for the
reset time — they can see it in the Claude Code UI or error message.

**For an explicit clock time (e.g. "01:15"):**

```bash
python3 -c "
from datetime import datetime, timedelta
now = datetime.now()
target = now.replace(hour=1, minute=15, second=0, microsecond=0)
if target <= now:
    target += timedelta(days=1)
print(int((target - now).total_seconds()))
"
```

### Step 3: Call ScheduleWakeup

Call `ScheduleWakeup` with:
- `delaySeconds`: computed delay (clamped to [60, 3600] by runtime)
- `prompt`: the deferred task, fully self-contained (working directory, branch,
  file paths, what to do, what to check)
- `reason`: e.g. "waiting for 5h limit reset" or "deferred until 01:15"

### Step 4: On wake — execute, then STOP

When the session resumes:
1. Run the deferred task
2. Do NOT call `ScheduleWakeup` again — loop ends naturally

## What NOT to do

- **Don't use cron** — repeats on schedule, explicitly rejected
- **Don't use `at`** — not installed on this system
- **Don't call ScheduleWakeup again** after the task runs — that would repeat
- **Don't lose task context** — the prompt must be fully self-contained
- **Don't hardcode reset times** — always read from the statusline JSON or ask
