#!/usr/bin/env bash
# Weekly date-checker.
# Agent access: Read + Write + Google Calendar MCP only (--strict-mcp-config).
# Email fallback handled here in the shell, not by the agent.
# Cron: 0 7 * * 1 (Mondays at 7am UTC = 9am Europe/Stockholm)

set -euo pipefail

# Load credentials (kept outside the repo, chmod 600)
# shellcheck source=/dev/null
[ -f "$HOME/.config/date-check/env" ] && source "$HOME/.config/date-check/env"

DATES_DIR="$HOME/Dropbox/1. Exocortex/_Engine/automation/dates"
CONFIG_FILE="$DATES_DIR/config.json"
STATE_FILE="$DATES_DIR/state.json"
IDEAS_FILE="$DATES_DIR/ideas.md"
MCP_CONFIG="$HOME/.claude/date-check-mcp.json"
LOG_FILE="$HOME/.claude/tmp/date-check.log"
CLAUDE="$HOME/.local/bin/claude"

mkdir -p "$(dirname "$LOG_FILE")"
exec >> "$LOG_FILE" 2>&1
echo "--- $(date '+%Y-%m-%d %H:%M:%S') ---"

# Load config
PARTNER=$(python3 -c "import json; d=json.load(open('$CONFIG_FILE')); print(d['partner_name'])")
CITY=$(python3 -c "import json; d=json.load(open('$CONFIG_FILE')); print(d['city'])")
LAT=$(python3 -c "import json; d=json.load(open('$CONFIG_FILE')); print(d['city_lat'])")
LON=$(python3 -c "import json; d=json.load(open('$CONFIG_FILE')); print(d['city_lon'])")
DATES_PER_MONTH=$(python3 -c "import json; d=json.load(open('$CONFIG_FILE')); print(d['dates_per_month'])")

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

# ── Run the agent (Read + Write + Google Calendar MCP only) ──────────────────
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

### Step 1 — Check Jonathan's Google Calendar
Fetch events for the next 14 days. Identify any that look like a planned date or social
outing with $PARTNER: events mentioning '$PARTNER', 'date', 'dinner', 'restaurant',
'bio', 'concert', 'museum', 'weekend trip', or any deliberate shared activity.

### Step 2 — Check $PARTNER's freebusy
Query ONLY the freebusy (busy time blocks) for $PARTNER's calendar for the next 14 days.
Do NOT read event titles or details — freebusy only. This is a hard privacy requirement.

### Step 3 — Read state and ideas files

### Step 4 — Decide and write state

**Case A — A date IS already planned in the next 14 days:**
Write: { \"last_checked\": \"$TODAY\", \"pending_suggestion\": null }

**Case B — No date found, no pending suggestion:**
1. Find 2–3 time windows where both are free: evenings (after 17:00) or weekends.
   Prefer windows with good weather for outdoor ideas (avoid rain/thunderstorm).
2. Pick ONE idea from ideas.md that fits the current season ($SEASON) and those windows.
   Prefer outdoor when weather is good; indoor otherwise. Vary picks over time.
3. Write:
{
  \"last_checked\": \"$TODAY\",
  \"pending_suggestion\": {
    \"idea\": \"<chosen idea>\",
    \"suggested_windows\": [\"<e.g. Sat Apr 19 evening — Clear sky, 15°C>\", \"<window 2>\"],
    \"created_at\": \"$TODAY\",
    \"shown_in_session\": false,
    \"email_sent\": false
  }
}

**Case C — No date found, pending suggestion exists, created_at is 7+ days ago, email_sent is false:**
Update state: set email_sent to true, update last_checked. Keep idea and suggested_windows.
(The shell script will send the actual email — you just flip the flag.)

**Case D — No date found, pending suggestion < 7 days old:**
Only update last_checked. Leave pending_suggestion unchanged.

## Rules
- Write state as valid JSON with 2-space indentation.
- If the Calendar MCP is unavailable: update last_checked only; do not clear a pending suggestion.
- If $PARTNER's freebusy is unavailable: proceed without suggested_windows.
- End with ONE summary line of what action you took.
"

"$CLAUDE" --print --dangerously-skip-permissions \
  --allowedTools "Read,Write" \
  --mcp-config "$MCP_CONFIG" \
  --strict-mcp-config \
  -p "$PROMPT"

# ── Email fallback (handled here, not by the agent) ──────────────────────────
STATE=$(python3 -c "import json; print(json.dumps(json.load(open('$STATE_FILE'))))" 2>/dev/null || echo "{}")

SHOULD_EMAIL=$(python3 << PYEOF
import json, sys
from datetime import date, datetime
state = json.loads('''$STATE''')
s = state.get("pending_suggestion")
if not s or s.get("email_sent"):
    print("no"); sys.exit(0)
try:
    created = datetime.strptime(s["created_at"], "%Y-%m-%d").date()
except Exception:
    print("no"); sys.exit(0)
days_old = (date.today() - created).days
print("yes" if days_old >= 7 else "no")
PYEOF
)

if [ "$SHOULD_EMAIL" = "yes" ]; then
    IDEA=$(python3 -c "
import json
s = json.load(open('$STATE_FILE')).get('pending_suggestion', {})
print(s.get('idea','a date'))
")
    WINDOWS=$(python3 -c "
import json
s = json.load(open('$STATE_FILE')).get('pending_suggestion', {})
ws = s.get('suggested_windows', [])
print(', '.join(ws) if ws else '')
")

    BODY="Hey — no date with $PARTNER is planned in the next 2 weeks.

Suggestion: $IDEA"
    if [ -n "$WINDOWS" ]; then
        BODY="$BODY

Good windows: $WINDOWS"
    fi

    python3 "$HOME/.claude/date-send-email.py" \
      "Date night reminder 💛" \
      "$BODY" \
      && echo "Email sent." \
      || echo "Email failed (check ~/.config/date-check/email.json)"
fi

echo "--- done ---"
