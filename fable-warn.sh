#!/usr/bin/env bash
# Warns if the current Claude model is Fable, at most once per 10 minutes.
#
# Restored 2026-08-02 from opus-warn.sh. The old script read the "model" key from
# settings.json; that key no longer exists (model is picked per-session via /model),
# so detection now reads the most recent assistant model from the session transcript
# handed to the hook on stdin. settings.json remains a fallback for an explicit pin.

set -uo pipefail

STAMP="$HOME/.claude/tmp/.fable-warn-stamp"
SETTINGS="$HOME/.claude/settings.json"

input=$(cat)

model=$(printf '%s' "$input" | python3 -c '
import json, os, sys

try:
    hook = json.load(sys.stdin)
except Exception:
    print(""); sys.exit(0)

path = hook.get("transcript_path") or ""
model = ""
try:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        # Only the tail matters — the current model is on the newest assistant turn.
        fh.seek(max(0, size - 262144))
        chunk = fh.read().decode("utf-8", "ignore")
    for line in reversed(chunk.splitlines()):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        msg = entry.get("message")
        if isinstance(msg, dict) and msg.get("model"):
            model = msg["model"]
            break
except Exception:
    pass
print(model)
' 2>/dev/null)

# Fallback: explicit model pin in settings.json
if [[ -z "$model" ]]; then
  model=$(grep -o '"model"[[:space:]]*:[[:space:]]*"[^"]*"' "$SETTINGS" 2>/dev/null | head -1 | sed 's/.*"model"[[:space:]]*:[[:space:]]*"//;s/"//')
fi

# Only warn for Fable models
if [[ "$model" != *fable* ]]; then
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

mkdir -p "$(dirname "$STAMP")"
date +%s > "$STAMP"

echo "Fable is selected, if working on hard task, advise the user to switch to Opus"
