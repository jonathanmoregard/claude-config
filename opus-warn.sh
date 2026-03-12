#!/usr/bin/env bash
# Warns if the current Claude model is Opus, at most once per 10 minutes.

SETTINGS="$HOME/.claude/settings.json"
STAMP="$HOME/.claude/tmp/.opus-warn-stamp"

model=$(grep -o '"model"[[:space:]]*:[[:space:]]*"[^"]*"' "$SETTINGS" 2>/dev/null | head -1 | sed 's/.*"model"[[:space:]]*:[[:space:]]*"//;s/"//')

# Only warn for Opus models
if [[ "$model" != *opus* ]]; then
  exit 0
fi

# Check if we warned in the last 10 minutes
if [[ -f "$STAMP" ]]; then
  last=$(cat "$STAMP")
  now=$(date +%s)
  if (( now - last < 600 )); then
    exit 0
  fi
fi

date +%s > "$STAMP"

echo "Opus is selected, if working on an easy task, advise the user to switch to Sonnet"
