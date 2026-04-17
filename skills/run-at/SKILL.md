---
name: run-at
description: >
  Use when the user wants to defer execution of a task to a future time or
  event. Trigger phrases: "after next limit reset", "after next reset window",
  "run this at [time]", "wait until [time] then do X", "after my limits reset",
  "schedule this for later". Uses CronCreate (one-shot, durable) for deferred
  execution — no /loop, no chaining, no repeats.
---

# Run-At: Deferred One-Shot Execution

## How to execute

### Step 1: Resolve target datetime

**For "next limit reset" / "next reset window":**

Run ccusage to get the active block's remaining time:

```bash
output=$(bunx ccusage blocks --active --timezone Europe/Stockholm 2>/dev/null)
remaining_line=$(echo "$output" | grep "Time Remaining:")
if [ -z "$remaining_line" ]; then
  echo "UNAVAILABLE — no active block"
else
  remaining=$(echo "$remaining_line" | sed 's/.*Time Remaining: //')
  hours=$(echo "$remaining" | grep -oP '\d+(?=h)' | head -1)
  mins=$(echo "$remaining" | grep -oP '\d+(?=m)' | head -1)
  [ -z "$hours" ] && hours=0
  [ -z "$mins" ] && mins=0
  total_mins=$(( hours * 60 + mins ))
  reset_epoch=$(( $(date +%s) + total_mins * 60 ))
  echo "cron:  $(date -d @$reset_epoch '+%M %H %d %m') *"
  echo "human: $(date -d @$reset_epoch '+%H:%M on %Y-%m-%d')"
fi
```

If UNAVAILABLE (no active block) or `bunx` not found, **ask the user** for
the reset time — they can see it in the Claude Code UI or error message.

**For an explicit clock time (e.g. "01:15"):**

```bash
python3 -c "
from datetime import datetime, timedelta
now = datetime.now()
target = now.replace(hour=1, minute=15, second=0, microsecond=0)
if target <= now:
    target += timedelta(days=1)
print(f'{target.minute} {target.hour} {target.day} {target.month} *')
"
```

### Step 2: Call CronCreate

```
CronCreate(
  cron: "<minute> <hour> <dom> <month> *",   # pinned to target datetime
  prompt: "<task>",
  recurring: false,                           # fire once, auto-delete
  durable: true                               # survive restarts
)
```

Confirm to the user: "Scheduled for HH:MM. Job fires once then deletes itself."

## What NOT to do

- **Don't use `recurring: true`** — task will repeat every year on same date
- **Don't omit `durable: true`** — job lost if session restarts before fire time
- **Don't omit `recurring: false`** — defaults to true, will repeat
- **Don't use `at`** — not installed on this system
- **Don't use ScheduleWakeup** — 3600s ceiling breaks for long waits
- **Don't hardcode reset times** — always run ccusage or ask
