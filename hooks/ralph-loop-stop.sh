#!/usr/bin/env bash

# Ralph Loop Stop Hook (session-bound fork of ralph-loop@claude-plugins-official)
#
# Difference from upstream: state lives at ~/.claude/ralph-loop/<session_id>.md
# instead of a cwd-relative .claude/ralph-loop.local.md. The gate therefore
# binds the SESSION that armed the loop, no matter what directory that session
# stops in, and never binds any other session in the same project.

set -euo pipefail

HOOK_INPUT=$(cat)

STATE_DIR="$HOME/.claude/ralph-loop"

HOOK_SESSION=$(echo "$HOOK_INPUT" | jq -r '.session_id // ""')

# No session id, or a session id that is not a plain uuid-ish token: do not
# guess, do not build a path out of it. Allow exit.
if [[ ! "$HOOK_SESSION" =~ ^[A-Za-z0-9._-]+$ ]]; then
  exit 0
fi

RALPH_STATE_FILE="$STATE_DIR/$HOOK_SESSION.md"

if [[ ! -f "$RALPH_STATE_FILE" ]]; then
  # This session has no active loop - allow exit
  exit 0
fi

# Parse markdown frontmatter (YAML between ---) and extract values
FRONTMATTER=$(sed -n '/^---$/,/^---$/{ /^---$/d; p; }' "$RALPH_STATE_FILE")
ITERATION=$(echo "$FRONTMATTER" | grep '^iteration:' | sed 's/iteration: *//')
MAX_ITERATIONS=$(echo "$FRONTMATTER" | grep '^max_iterations:' | sed 's/max_iterations: *//')
# Extract completion_promise and strip surrounding quotes if present
COMPLETION_PROMISE=$(echo "$FRONTMATTER" | grep '^completion_promise:' | sed 's/completion_promise: *//' | sed 's/^"\(.*\)"$/\1/')

# Validate numeric fields before arithmetic operations
if [[ ! "$ITERATION" =~ ^[0-9]+$ ]]; then
  echo "⚠️  Ralph loop: State file corrupted" >&2
  echo "   File: $RALPH_STATE_FILE" >&2
  echo "   Problem: 'iteration' field is not a valid number (got: '$ITERATION')" >&2
  echo "" >&2
  echo "   Ralph loop is stopping. Run /ralph-loop again to start fresh." >&2
  rm "$RALPH_STATE_FILE"
  exit 0
fi

if [[ ! "$MAX_ITERATIONS" =~ ^[0-9]+$ ]]; then
  echo "⚠️  Ralph loop: State file corrupted" >&2
  echo "   File: $RALPH_STATE_FILE" >&2
  echo "   Problem: 'max_iterations' field is not a valid number (got: '$MAX_ITERATIONS')" >&2
  echo "" >&2
  echo "   Ralph loop is stopping. Run /ralph-loop again to start fresh." >&2
  rm "$RALPH_STATE_FILE"
  exit 0
fi

# Check if max iterations reached
if [[ $MAX_ITERATIONS -gt 0 ]] && [[ $ITERATION -ge $MAX_ITERATIONS ]]; then
  echo "🛑 Ralph loop: Max iterations ($MAX_ITERATIONS) reached."
  rm "$RALPH_STATE_FILE"
  exit 0
fi

TRANSCRIPT_PATH=$(echo "$HOOK_INPUT" | jq -r '.transcript_path')

# Read the assistant lines from the transcript (JSONL - one JSON per line).
#
# The Stop hook can fire before Claude Code has flushed the turn's assistant
# message, so poll briefly instead of reading once. Measured on headless runs:
# reading once killed the loop on iteration 1 roughly a third of the time,
# because an unflushed transcript looked identical to "session produced no
# output". Absence of assistant text is NOT evidence the task is finished.
LAST_LINES=""
for _ in $(seq 1 10); do
  if [[ -f "$TRANSCRIPT_PATH" ]]; then
    LAST_LINES=$(grep '"role":"assistant"' "$TRANSCRIPT_PATH" 2>/dev/null | tail -n 100 || true)
    [[ -n "$LAST_LINES" ]] && break
  fi
  sleep 0.3
done

# Extract the most recent assistant text block. Claude Code writes each content
# block (text/tool_use/thinking) as its own JSONL line, all with
# role=assistant, so flatten the last 100 to text blocks and take the last one.
# The 100-line cap keeps jq's input bounded for long-running sessions.
#
# `last // ""` yields empty string when no text blocks exist (e.g. a turn that
# is all tool calls). Empty text means no <promise> tag, so the loop continues.
LAST_OUTPUT=""
TRANSCRIPT_NOTE=""

if [[ -z "$LAST_LINES" ]]; then
  # Still nothing after ~3s. Continue the loop rather than ending it: ending
  # here would let a slow flush masquerade as completion.
  TRANSCRIPT_NOTE=" | ⚠️ transcript unreadable this turn ($TRANSCRIPT_PATH) - promise not checked"
  echo "⚠️  Ralph loop: no assistant output readable in transcript; continuing loop" >&2
  echo "   Transcript: $TRANSCRIPT_PATH" >&2
else
  set +e
  LAST_OUTPUT=$(echo "$LAST_LINES" | jq -rs '
    map(.message.content[]? | select(.type == "text") | .text) | last // ""
  ' 2>&1)
  JQ_EXIT=$?
  set -e

  # A parse failure is a real corruption signal, not a timing artefact: stop.
  if [[ $JQ_EXIT -ne 0 ]]; then
    echo "⚠️  Ralph loop: Failed to parse assistant message JSON" >&2
    echo "   Error: $LAST_OUTPUT" >&2
    echo "   Ralph loop is stopping." >&2
    rm "$RALPH_STATE_FILE"
    exit 0
  fi
fi

# Check for completion promise (only if set)
if [[ "$COMPLETION_PROMISE" != "null" ]] && [[ -n "$COMPLETION_PROMISE" ]]; then
  # Extract text from <promise> tags using Perl for multiline support
  # -0777 slurps entire input, s flag makes . match newlines
  # .*? is non-greedy (takes FIRST tag), whitespace normalized
  PROMISE_TEXT=$(echo "$LAST_OUTPUT" | perl -0777 -pe 's/.*?<promise>(.*?)<\/promise>.*/$1/s; s/^\s+|\s+$//g; s/\s+/ /g' 2>/dev/null || echo "")

  # Use = for literal string comparison ( == in [[ ]] would glob-match )
  if [[ -n "$PROMISE_TEXT" ]] && [[ "$PROMISE_TEXT" = "$COMPLETION_PROMISE" ]]; then
    echo "✅ Ralph loop: Detected <promise>$COMPLETION_PROMISE</promise>"
    rm "$RALPH_STATE_FILE"
    exit 0
  fi
fi

# Not complete - continue loop with SAME PROMPT
NEXT_ITERATION=$((ITERATION + 1))

# Extract prompt (everything after the closing ---)
PROMPT_TEXT=$(awk '/^---$/{i++; next} i>=2' "$RALPH_STATE_FILE")

if [[ -z "$PROMPT_TEXT" ]]; then
  echo "⚠️  Ralph loop: State file corrupted or incomplete" >&2
  echo "   File: $RALPH_STATE_FILE" >&2
  echo "   Problem: No prompt text found" >&2
  echo "   Ralph loop is stopping. Run /ralph-loop again to start fresh." >&2
  rm "$RALPH_STATE_FILE"
  exit 0
fi

# Update iteration in frontmatter (portable across macOS and Linux)
TEMP_FILE="${RALPH_STATE_FILE}.tmp.$$"
sed "s/^iteration: .*/iteration: $NEXT_ITERATION/" "$RALPH_STATE_FILE" > "$TEMP_FILE"
mv "$TEMP_FILE" "$RALPH_STATE_FILE"

if [[ "$COMPLETION_PROMISE" != "null" ]] && [[ -n "$COMPLETION_PROMISE" ]]; then
  SYSTEM_MSG="🔄 Ralph iteration $NEXT_ITERATION | To stop: output <promise>$COMPLETION_PROMISE</promise> (ONLY when statement is TRUE - do not lie to exit!)"
else
  SYSTEM_MSG="🔄 Ralph iteration $NEXT_ITERATION | No completion promise set - loop runs infinitely"
fi
SYSTEM_MSG="${SYSTEM_MSG}${TRANSCRIPT_NOTE}"

# Block the stop and feed the prompt back
jq -n \
  --arg prompt "$PROMPT_TEXT" \
  --arg msg "$SYSTEM_MSG" \
  '{
    "decision": "block",
    "reason": $prompt,
    "systemMessage": $msg
  }'

exit 0
