#!/usr/bin/env bash
# Weekly date-checker.
# Agent access: Read + Write only (no MCP, no calendar).
# Escalation (creating a TickTick reminder) is handled here in the shell,
# not by the agent — the agent never touches the todo_created flag.
# Cron: CRON_TZ=Europe/Stockholm 0 9 * * 1  (Mondays at 9am local time)

set -euo pipefail

DATES_DIR="$HOME/.claude/dates"
CONFIG_FILE="$DATES_DIR/config.json"
STATE_FILE="$DATES_DIR/state.json"
IDEAS_FILE="$DATES_DIR/ideas.md"
LOG_FILE="$HOME/.claude/tmp/date-check.log"
CLAUDE="$HOME/.local/bin/claude"
TODO_ADD="$HOME/.claude/todo-add"

mkdir -p "$(dirname "$LOG_FILE")"
exec >> "$LOG_FILE" 2>&1
echo "--- $(date '+%Y-%m-%d %H:%M:%S') ---"

# Load config in one python invocation.
eval "$(CONFIG_FILE="$CONFIG_FILE" python3 <<'PYEOF'
import json, os, shlex
d = json.load(open(os.environ["CONFIG_FILE"]))
for k in ("partner_name", "city", "city_lat", "city_lon", "dates_per_month"):
    print(f"{k.upper()}={shlex.quote(str(d[k]))}")
PYEOF
)"
PARTNER="$PARTNER_NAME"
CITY="$CITY"
LAT="$CITY_LAT"
LON="$CITY_LON"

# Fetch 14-day weather forecast from Open-Meteo (no API key required)
WEATHER_RAW=$(curl -sf \
  "https://api.open-meteo.com/v1/forecast?latitude=${LAT}&longitude=${LON}&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum&timezone=Europe%2FStockholm&forecast_days=14" \
  || echo "{}")

WEATHER_SUMMARY=$(WEATHER_RAW="$WEATHER_RAW" python3 << 'PYEOF'
import json, os

try:
    data = json.loads(os.environ["WEATHER_RAW"])
except Exception:
    print("(weather unavailable)")
    raise SystemExit(0)

daily  = data.get("daily", {})
dates  = daily.get("time", [])
codes  = daily.get("weather_code", [])
tmax   = daily.get("temperature_2m_max", [])
tmin   = daily.get("temperature_2m_min", [])
precip = daily.get("precipitation_sum", [])

def describe(code):
    if code == 0:                               return "Clear sky"
    if code in (1, 2, 3):                      return "Partly cloudy"
    if code in (45, 48):                       return "Fog"
    if code in (51,53,55,61,63,65,66,67,80,81,82): return "Rain"
    if code in (71,73,75,77,85,86):            return "Snow"
    if code in (95,96,99):                     return "Thunderstorm"
    return "Mixed"

lines = []
for i in range(len(dates)):
    p = precip[i] if precip[i] is not None else 0
    lines.append(f"{dates[i]}: {describe(codes[i])}, {tmin[i]:.0f}–{tmax[i]:.0f}°C, {p:.1f}mm")
print("\n".join(lines))
PYEOF
)

TODAY=$(date '+%Y-%m-%d')
MONTH=$(date '+%B')
SEASON=$(python3 -c "
from datetime import date
m = date.today().month
print('Winter' if m in (12,1,2) else 'Spring' if m in (3,4,5) else 'Summer' if m in (6,7,8) else 'Fall')
")

# ── Run the agent (Read + Write only) ────────────────────────────────────────
PROMPT="You are a weekly date-night planner helping Jonathan stay on a regular date rhythm with $PARTNER in $CITY.

## Context
- Today: $TODAY ($MONTH, $SEASON)
- Target: ~$DATES_PER_MONTH dates per month
- Config: $CONFIG_FILE
- State file: $STATE_FILE
- Ideas file: $IDEAS_FILE

## 14-Day Weather Forecast ($CITY)
$WEATHER_SUMMARY

## Your Task

### Step 1 — Read state and ideas files

### Step 2 — Decide and write state

**Case A — No pending suggestion:**
1. Pick ONE idea from ideas.md that fits the current season ($SEASON).
   Prefer outdoor ideas when the next few days look clear; indoor otherwise. Vary picks over time.
2. Write:
{
  \"last_checked\": \"$TODAY\",
  \"pending_suggestion\": {
    \"idea\": \"<chosen idea>\",
    \"created_at\": \"$TODAY\",
    \"shown\": false,
    \"todo_created\": false
  }
}

**Case B — Pending suggestion already exists:**
Only update last_checked. Leave pending_suggestion unchanged.
Do NOT touch the todo_created flag — the shell script owns escalation.

## Rules
- Write state as valid JSON with 2-space indentation.
- End with ONE summary line of what action you took.
"

"$CLAUDE" --print --dangerously-skip-permissions \
  --allowedTools "Read,Write" \
  -p "$PROMPT"

# ── Escalation: create a TickTick reminder if suggestion has sat for 7+ days ─
# The shell owns the todo_created flag. The agent never sets it.
STATE_FILE="$STATE_FILE" TODO_ADD="$TODO_ADD" PARTNER="$PARTNER" python3 <<'PYEOF'
import json, os, subprocess, sys
from datetime import date, datetime, timedelta

state_file = os.environ["STATE_FILE"]
todo_add   = os.environ["TODO_ADD"]
partner    = os.environ["PARTNER"]

try:
    with open(state_file) as f:
        state = json.load(f)
except Exception as e:
    print(f"Escalation: cannot read state ({e}), skipping.")
    sys.exit(0)

s = state.get("pending_suggestion") or {}
if not s:
    print("Escalation: no pending suggestion.")
    sys.exit(0)
if s.get("todo_created"):
    print("Escalation: todo already created for this suggestion.")
    sys.exit(0)

try:
    created = datetime.strptime(s["created_at"], "%Y-%m-%d").date()
except Exception:
    print("Escalation: bad created_at, skipping.")
    sys.exit(0)

days_old = (date.today() - created).days
if days_old < 7:
    print(f"Escalation: suggestion is {days_old}d old, not yet due.")
    sys.exit(0)

idea = s.get("idea", "Plan a date")
title = f"Plan date with {partner}: {idea}"
notes = f"No date with {partner} planned recently. Suggestion: {idea}"
due = (date.today() + timedelta(days=3)).isoformat()

try:
    result = subprocess.run(
        [todo_add, title, "--due", due, "--notes", notes],
        check=True, capture_output=True, text=True,
    )
    task_id = result.stdout.strip()
    print(f"Escalation: TickTick task created ({task_id}).")
except subprocess.CalledProcessError as e:
    print(f"Escalation: todo-add failed ({e.returncode}): {e.stderr.strip()}")
    sys.exit(0)  # do not flip flag; retry next week

# Flip the flag so we don't recreate the task next week.
s["todo_created"] = True
state["pending_suggestion"] = s
with open(state_file, "w") as f:
    json.dump(state, f, indent=2)
print("Escalation: state updated (todo_created=true).")
PYEOF

echo "--- done ---"
