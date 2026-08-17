#!/usr/bin/env bash

# Cancel a session-bound Ralph loop.
# Default target: this session. Optional arg: another session id, or --all.

set -euo pipefail

STATE_DIR="$HOME/.claude/ralph-loop"
TARGET="${1:-${CLAUDE_CODE_SESSION_ID:-}}"

shopt -s nullglob
ALL_STATES=("$STATE_DIR"/*.md)
shopt -u nullglob

report_others() {
  if [[ ${#ALL_STATES[@]} -eq 0 ]]; then
    return
  fi
  echo ""
  echo "Loops still armed (other sessions):"
  for f in "${ALL_STATES[@]}"; do
    [[ -f "$f" ]] || continue
    sid=$(basename "$f" .md)
    it=$(grep '^iteration:' "$f" | sed 's/iteration: *//' || echo "?")
    dir=$(grep '^armed_in:' "$f" | sed 's/armed_in: *//' | tr -d '"' || echo "?")
    echo "  $sid  (iteration $it, armed in $dir)"
  done
  echo ""
  echo "Cancel one with: ~/.claude/scripts/ralph-loop-cancel.sh <session_id>"
}

if [[ "$TARGET" == "--all" ]]; then
  if [[ ${#ALL_STATES[@]} -eq 0 ]]; then
    echo "No active Ralph loops."
    exit 0
  fi
  for f in "${ALL_STATES[@]}"; do
    echo "Cancelled Ralph loop for session $(basename "$f" .md)"
    rm "$f"
  done
  exit 0
fi

if [[ ! "$TARGET" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "No session id available (CLAUDE_CODE_SESSION_ID unset, no argument given)."
  report_others
  exit 0
fi

STATE_FILE="$STATE_DIR/$TARGET.md"

if [[ ! -f "$STATE_FILE" ]]; then
  echo "No active Ralph loop for session $TARGET."
  report_others
  exit 0
fi

ITERATION=$(grep '^iteration:' "$STATE_FILE" | sed 's/iteration: *//' || echo "?")
rm "$STATE_FILE"
echo "Cancelled Ralph loop for session $TARGET (was at iteration $ITERATION)."
