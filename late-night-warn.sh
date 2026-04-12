#!/usr/bin/env bash
# Warns if it's after 20:00 and the user has 5+ hours of Claude activity today.
# Fires at most once per session (60 min cooldown).

STAMP="$HOME/.claude/tmp/.late-night-warn-stamp"
PROJECTS_DIR="$HOME/.claude/projects"

hour=$(date +%H)
if (( hour < 20 )); then
  exit 0
fi

# Cooldown: warn at most once per 60 minutes
if [[ -f "$STAMP" ]]; then
  last=$(cat "$STAMP")
  now=$(date +%s)
  if (( now - last < 3600 )); then
    exit 0
  fi
fi

# Count hours of activity today by checking jsonl files modified today
today=$(date +%Y-%m-%d)
active_files=$(find "$PROJECTS_DIR" -name "*.jsonl" -newer "/tmp/.late-night-sentinel-$today" 2>/dev/null | wc -l)

# Create today's sentinel if it doesn't exist (set to midnight)
sentinel="/tmp/.late-night-sentinel-$today"
if [[ ! -f "$sentinel" ]]; then
  touch -t "$(date +%Y%m%d)0000" "$sentinel"
fi

# Estimate hours: count jsonl files modified today, each session ~1 hour average
# Better heuristic: sum up hours from first to last modified jsonl today
first_mod=""
last_mod=""
while IFS= read -r f; do
  mod=$(stat -c %Y "$f" 2>/dev/null)
  if [[ -z "$first_mod" || "$mod" -lt "$first_mod" ]]; then
    first_mod="$mod"
  fi
  if [[ -z "$last_mod" || "$mod" -gt "$last_mod" ]]; then
    last_mod="$mod"
  fi
done < <(find "$PROJECTS_DIR" -name "*.jsonl" -newermt "$today" 2>/dev/null)

if [[ -z "$first_mod" || -z "$last_mod" ]]; then
  exit 0
fi

span_hours=$(( (last_mod - first_mod) / 3600 ))

if (( span_hours < 5 )); then
  exit 0
fi

mkdir -p "$(dirname "$STAMP")"
date +%s > "$STAMP"

echo "It's after 20:00 and you've been active for ~${span_hours} hours today. Gently remind the user — their wellbeing goals include mindful breaks and not marathoning. This is a nudge, not an order."
