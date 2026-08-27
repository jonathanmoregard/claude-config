#!/usr/bin/env bash

# Ralph Loop Stop Hook (session-bound fork of ralph-loop@claude-plugins-official)
#
# Difference from upstream: state lives at
# ~/.local/state/claude-ralph-loop/<session_id>.md instead of a cwd-relative
# .claude/ralph-loop.local.md. The gate therefore binds the SESSION that armed
# the loop, no matter what directory that session stops in, never binds any
# other session in the same project, and never writes inside a repo.

set -euo pipefail

HOOK_INPUT=$(cat)

# Path comes from the shared resolver, never re-spelled here: this hook is one
# of the readers a previous half-done migration left pointing at the old home.
source "$HOME/.claude/scripts/ralph-loop-state.sh"
STATE_DIR="$RALPH_STATE_DIR"

HOOK_SESSION=$(echo "$HOOK_INPUT" | jq -r '.session_id // ""')

# No session id, or a session id that is not a plain uuid-ish token: do not
# guess, do not build a path out of it. Allow exit.
if [[ ! "$HOOK_SESSION" =~ ^[A-Za-z0-9._-]+$ ]]; then
  exit 0
fi

RALPH_STATE_FILE="$STATE_DIR/$HOOK_SESSION.md"

# ---------------------------------------------------------------------------
# Ending the loop hands the turn to the definition-of-done gate.
#
# The gate stands down while a loop owns the session, so two Stop hooks never
# block one turn and out-vote the human. That leaves one question: who checks
# the work on the turn the loop ENDS?
#
# It cannot be left to hook ordering. Measured on 2.1.247 with two Stop hooks,
# one sleeping: the hook declared SECOND started 111ms BEFORE the first, and ran
# while it slept. Stop hooks are CONCURRENT. So on the final turn the gate may
# read the state file before this script unlinks it, stand down, and there is no
# next turn — the loop's last turn escapes the check entirely, which is exactly
# the turn where "did any of this get pushed?" matters most.
#
# So the loop hands over explicitly. Every path that ends the loop calls this,
# and the GATE's verdict decides whether the turn may end. Its own ownership
# check passes by itself, because the state file is gone before it runs.
#
# The standalone gate later in the Stop list still races us and may reach the
# same verdict independently, costing a duplicated block message. The opposite
# failure — no check at all — is the one worth engineering against.
end_loop() {  # $1 = human-readable message for the transcript
  local msg="$1" gate="$HOME/.claude/hooks/finishing-up-gate.py" out=""
  rm -f "$RALPH_STATE_FILE"
  if [[ -f "$gate" ]]; then
    out=$(printf '%s' "$HOOK_INPUT" | python3 "$gate" 2>/dev/null) || out=""
  fi
  if [[ -n "$out" ]]; then
    # Exactly one JSON object must reach the harness, so fold our message into
    # the gate's: a block keeps its reason and the human still learns why the
    # loop ended.
    jq -n --argjson g "$out" --arg m "$msg" \
      '$g + {systemMessage: ([($g.systemMessage // ""), $m] | map(select(. != "")) | join(" | "))}' \
      2>/dev/null || printf '%s\n' "$out"
  else
    jq -n --arg m "$msg" '{systemMessage: $m}'
  fi
  exit 0
}

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
  end_loop "Ralph loop: stopped - state file corrupted (iteration)."
fi

if [[ ! "$MAX_ITERATIONS" =~ ^[0-9]+$ ]]; then
  echo "⚠️  Ralph loop: State file corrupted" >&2
  echo "   File: $RALPH_STATE_FILE" >&2
  echo "   Problem: 'max_iterations' field is not a valid number (got: '$MAX_ITERATIONS')" >&2
  echo "" >&2
  echo "   Ralph loop is stopping. Run /ralph-loop again to start fresh." >&2
  end_loop "Ralph loop: stopped - state file corrupted (max_iterations)."
fi

# Check if max iterations reached
if [[ $MAX_ITERATIONS -gt 0 ]] && [[ $ITERATION -ge $MAX_ITERATIONS ]]; then
  end_loop "Ralph loop: max iterations ($MAX_ITERATIONS) reached."
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
    end_loop "Ralph loop: stopped - could not parse the transcript."
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
    end_loop "Ralph loop: completion promise met."
  fi
fi

# Honest exit that does NOT claim completion.
#
# Without this the loop had exactly three exits: the sigil, max_iterations, or
# the user typing /cancel-ralph. So when a human said "write the handoff and
# then stop, do no other work" on a mission that was genuinely incomplete, the
# agent's only options were to LIE (emit a false sigil) or to DISOBEY (keep
# working). Observed 2026-08-27, session ebcdd93d: it held for six turns
# repeating "Holding.", then broke the instruction and resumed — about two
# hours and 61k tokens. Six held turns is the loop out-voting the human, and
# "do not lie to exit" is not a discipline if the only alternative is disobeying.
#
# <halt> ends the loop while asserting nothing about completion. The reason is
# required and recorded, so an exit is auditable rather than silent. It is a
# weaker claim than the sigil, which is the point: nothing here tempts an agent
# to overstate what it finished.
if [[ "$LAST_OUTPUT" == *"<halt>"* ]]; then
  HALT_REASON=$(echo "$LAST_OUTPUT" | perl -0777 -pe 's/.*?<halt>(.*?)<\/halt>.*/$1/s; s/^\s+|\s+$//g; s/\s+/ /g' 2>/dev/null || echo "")
  if [[ -n "$HALT_REASON" ]]; then
    printf '%s\t%s\titeration=%s\t%s\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$HOOK_SESSION" "$ITERATION" "$HALT_REASON" \
      >> "$STATE_DIR/halts.log" 2>/dev/null || true
    end_loop "Ralph loop: halted at iteration $ITERATION without claiming completion - $HALT_REASON"
  fi
  # A bare <halt> with no reason is not an exit. Fall through and keep looping.
  echo "⚠️  Ralph loop: <halt> seen with an empty reason; ignoring. A halt must say why." >&2
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
  end_loop "Ralph loop: stopped - state file has no prompt text."
fi

# Update iteration in frontmatter (portable across macOS and Linux)
TEMP_FILE="${RALPH_STATE_FILE}.tmp.$$"
sed "s/^iteration: .*/iteration: $NEXT_ITERATION/" "$RALPH_STATE_FILE" > "$TEMP_FILE"
mv "$TEMP_FILE" "$RALPH_STATE_FILE"

if [[ "$COMPLETION_PROMISE" != "null" ]] && [[ -n "$COMPLETION_PROMISE" ]]; then
  SYSTEM_MSG="🔄 Ralph iteration $NEXT_ITERATION | To stop: output <promise>$COMPLETION_PROMISE</promise> (ONLY when statement is TRUE - do not lie to exit!) — or <halt>why</halt> to end the loop WITHOUT claiming completion. If the user told you to stop, halt; do not keep looping and do not fake the promise."
else
  SYSTEM_MSG="🔄 Ralph iteration $NEXT_ITERATION | No completion promise set - loop runs infinitely. <halt>why</halt> ends it. If the user told you to stop, halt."
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
