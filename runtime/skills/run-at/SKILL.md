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

Run `bunx ccusage blocks --active --timezone Europe/Stockholm` and read the
"Time Remaining:" line. Add that duration to the current time to get the
reset time, then format as a cron expression.

If there is no active block or `bunx` is not available, **ask the user** for
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
