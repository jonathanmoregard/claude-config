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
# GUARDED. Bare, under `set -euo pipefail`, a missing resolver kills this hook
# before any of its handlers — every turn, silently — and the loop's state file
# then survives, which stands the standalone gate down on it ("a ralph loop owns
# this session") forever. The fallback is the resolver's own documented default
# and reads the same env var finishing-up-gate.py reads, so the two readers
# still agree about where state lives even when one of them cannot load it.
if ! source "$HOME/.claude/scripts/ralph-loop-state.sh" 2>/dev/null; then
  RALPH_STATE_DIR="${CLAUDE_RALPH_STATE_DIR:-$HOME/.local/state/claude-ralph-loop}"
  echo "⚠️  Ralph loop: state resolver missing at $HOME/.claude/scripts/ralph-loop-state.sh; falling back to $RALPH_STATE_DIR" >&2
fi
STATE_DIR="$RALPH_STATE_DIR"

# Ceiling for the inline definition-of-done call in end_loop. It must stay under
# the harness timeout for THIS hook — settings.json gives the ralph entry no
# `timeout` key, so it runs on the 60s default — minus the transcript poll
# further down, which can spend ~3s of that on its own.
GATE_CALL_TIMEOUT="${CLAUDE_RALPH_GATE_TIMEOUT:-45}"

HOOK_SESSION=$(echo "$HOOK_INPUT" | jq -r '.session_id // ""')

# No session id, or a session id that is not a plain uuid-ish token: do not
# guess, do not build a path out of it. Allow exit.
if [[ ! "$HOOK_SESSION" =~ ^[A-Za-z0-9._-]+$ ]]; then
  exit 0
fi

RALPH_STATE_FILE="$STATE_DIR/$HOOK_SESSION.md"

extract_tag() {  # $1 = tag name; reads $LAST_OUTPUT, prints the claimed text
  printf '%s' "$LAST_OUTPUT" | TAG="$1" perl -ne '
    BEGIN { $tag = $ENV{TAG}; $fence = ""; @hit = (); }
    # Track WHICH fence character opened the block: only the same character
    # closes it. Treating ``` and ~~~ as one state meant a ``` fence displayed
    # INSIDE a ~~~ fence flipped the state to closed, and the demo tag on the
    # next line was then read as a genuine completion claim.
    if (/^[ \t]*(```|~~~)/) {
      my $mark = $1;
      if ($fence eq "")       { $fence = $mark; }
      elsif ($fence eq $mark) { $fence = "";    }
      next;
    }
    next if $fence ne "";
    # Up to three leading spaces: four or more is a markdown code block, which
    # is a demonstration of the tag rather than an assertion of it. \r is
    # allowed at the end so CRLF transcripts can still exit the loop at all.
    if (/^[ ]{0,3}<\Q$tag\E>(.*?)<\/\Q$tag\E>[ \t\r]*$/) { push @hit, $1; }
    END { if (@hit) { $t = $hit[-1]; $t =~ s/^\s+|\s+$//g; $t =~ s/\s+/ /g; print $t; } }
  ' 2>/dev/null || true
}

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
  local gate_fail="" gate_err="" gate_rc=0
  local -a runner=()
  # No marker. The standalone gate is racing us right now and may reach the
  # same verdict independently; that costs a duplicated block message, which
  # the runtime tolerates (measured: 22 blocking responses across 11 firings,
  # clean exit). A marker used to suppress that duplicate, and its own failure
  # mode — orphaned, then silently allowing a later turn's unfinished work —
  # was strictly worse than the duplicate it prevented. Four review rounds of
  # ordering bugs later, it is gone. The call below is sequential composition,
  # not coordination: it converts the race into a function call.
  #
  # The unlink is GUARDED. Bare, under `set -euo pipefail`, it exits the hook
  # before every JSON emitter below it — and it fails for real: a state
  # directory at mode 555 gives EACCES, because unlink needs write permission on
  # the DIRECTORY, not the file. Reproduced 2026-08-29, identically at HEAD and
  # at master: rc=1, zero bytes on stdout, state file surviving, and the
  # standalone gate then logging "a ralph loop owns this session" over a repo
  # with real unpushed work — on that turn and on every later one. A loop whose
  # state cannot be cleaned up must still end LOUDLY.
  if ! rm -f "$RALPH_STATE_FILE" 2>/dev/null; then
    msg="$msg | ⚠️ could not delete the loop state file ($RALPH_STATE_FILE); until it is removed by hand both Stop hooks will read this session as a live loop"
  fi
  if [[ -f "$gate" ]]; then
    # HANDOVER=1 so the gate skips its ownership check and its block budget:
    # this call IS the handover, and standing down here would mean nobody
    # checked at all. The ownership skip is load-bearing exactly when the unlink
    # above failed — the state file is still there, and without the skip the
    # gate would yield to it and end the turn unmeasured.
    #
    # stderr goes to a TEMP file, never into the state dir: the unlink above may
    # have just proved that directory unwritable, and a redirect that fails
    # there under `set -e` kills the hook on the one turn this whole function
    # exists to cover.
    gate_err=$(mktemp 2>/dev/null || true)
    [[ -n "$gate_err" ]] || gate_err=/dev/null
    # BOUNDED. The gate can legally spend DOD_TIMEOUT (60s) per armed repo while
    # the harness kills this hook at 60s. MEASURED 2026-08-29: three armed repos
    # put 120.4s between the unlink above and the first byte on stdout, and a
    # kill at 60s left EMPTY stdout with the loop state already deleted — the
    # final turn measured zero times, nothing left to revisit it. In the same
    # run the gate reached a `block` verdict 0.11s AFTER its reader died: the
    # measurement happened and was thrown away. The gate now bounds its own
    # total as well; this is the outer belt, and an expiry here is a gate
    # failure like any other — it BLOCKS.
    if command -v timeout >/dev/null 2>&1; then
      runner=(timeout "$GATE_CALL_TIMEOUT")
    fi
    out=$(printf '%s' "$HOOK_INPUT" \
          | CLAUDE_DOD_HANDOVER=1 "${runner[@]}" python3 "$gate" 2>"$gate_err") \
      || gate_rc=$?
    if [[ $gate_rc -ne 0 ]]; then
      if [[ $gate_rc -eq 124 ]]; then
        gate_fail="the definition-of-done gate did not finish within ${GATE_CALL_TIMEOUT}s"
      else
        gate_fail="the definition-of-done gate FAILED to run ($(head -c 200 "$gate_err" 2>/dev/null | tr '\n' ' '))"
      fi
      out=""
    fi
    [[ "$gate_err" = /dev/null ]] || rm -f "$gate_err" 2>/dev/null || true
  else
    gate_fail="the definition-of-done gate is missing at $gate"
  fi

  # A checker that could not run has NOT reported "done". Saying so in a
  # systemMessage — which is what this did first — leaves the turn ending
  # anyway, on the one turn the whole handover exists to cover, with the loop
  # state already deleted so nothing will revisit it. Unmeasurable is not done
  # here for the same reason it is not done anywhere else in this system.
  if [[ -n "${gate_fail:-}" ]]; then
    jq -n --arg r "The loop ended, but $gate_fail, so this turn's work was never checked. Do not treat that as finished: run \`python3 \$HOME/.claude/scripts/dod-check.py --repo <repo>\` yourself and report what it says, or say plainly that you could not." \
          --arg m "$msg | ⛔ $gate_fail — turn NOT checked" \
          '{decision: "block", reason: $r, systemMessage: $m}'
    exit 0
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
ITERATION=$(echo "$FRONTMATTER" | grep '^iteration:' | sed 's/iteration: *//') || true
MAX_ITERATIONS=$(echo "$FRONTMATTER" | grep '^max_iterations:' | sed 's/max_iterations: *//') || true
# Extract completion_promise and strip surrounding quotes if present
COMPLETION_PROMISE=$(echo "$FRONTMATTER" | grep '^completion_promise:' | sed 's/completion_promise: *//' | sed 's/^"\(.*\)"$/\1/') || true

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
  # THE TAG MUST STAND ALONE ON ITS OWN LINE, AND MUST ACTUALLY BE PRESENT.
  #
  # The old form was `s/.*?<promise>(.*?)<\/promise>.*/$1/`, which took the
  # first tag ANYWHERE and, when there was no tag at all, fell through leaving
  # $_ untouched — so the whole message became the "promise text". Measured,
  # all three exited the loop with a false completion claim:
  #
  #   "I am not done, so I will not output <promise>ALL DONE</promise> yet."
  #   "To stop the loop I must emit <promise>ALL DONE</promise> only when true."
  #   "ALL DONE"                        (no tag whatsoever)
  #
  # The systemMessage injects the literal promise string every iteration, so
  # the model is READING that text every turn — quoting it back while
  # explaining itself is ordinary behaviour, not an edge case. "Do not lie to
  # exit" is not a discipline if merely discussing the rule satisfies it.
  #
  # Own-line anchoring is what separates asserting from quoting: prose wraps a
  # tag in a sentence, a deliberate claim stands alone. Take the LAST such
  # line, so a message that discusses the rule and then genuinely claims
  # completion still exits.
  PROMISE_TEXT=""
  if [[ "$LAST_OUTPUT" == *"<promise>"* ]]; then
    PROMISE_TEXT=$(extract_tag promise)
  fi

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
  # Own-line anchored for the same reason as <promise> above: the systemMessage
  # advertises this tag every iteration, so the model reads it every turn and
  # quoting it back mid-sentence must not end the loop.
  HALT_REASON=$(extract_tag halt)
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
# A failed write (full disk, read-only state dir) used to kill the hook under
# `set -e` BEFORE it emitted its block JSON. The concurrent gate has already
# stood down on the live state file by then, so the turn would end with nobody
# checking at all — the exact fail-open the marker machinery guards elsewhere.
# Losing an iteration increment is survivable; losing the block is not.
TEMP_FILE="${RALPH_STATE_FILE}.tmp.$$"
ITER_WRITE_NOTE=""
if sed "s/^iteration: .*/iteration: $NEXT_ITERATION/" "$RALPH_STATE_FILE" > "$TEMP_FILE" 2>/dev/null \
   && mv "$TEMP_FILE" "$RALPH_STATE_FILE" 2>/dev/null; then
  :
else
  rm -f "$TEMP_FILE"
  ITER_WRITE_NOTE=" | ⚠️ could not record iteration $NEXT_ITERATION (state dir unwritable?); the loop continues but the count is stale"
fi

if [[ "$COMPLETION_PROMISE" != "null" ]] && [[ -n "$COMPLETION_PROMISE" ]]; then
  SYSTEM_MSG="🔄 Ralph iteration $NEXT_ITERATION | To stop, put the tag ON A LINE BY ITSELF (a tag inside a sentence is ignored, so you can discuss these freely): <promise>$COMPLETION_PROMISE</promise> ONLY when that statement is TRUE — do not lie to exit. Or <halt>why</halt> to end the loop WITHOUT claiming completion. If the user told you to stop, halt; do not keep looping and do not fake the promise."
else
  SYSTEM_MSG="🔄 Ralph iteration $NEXT_ITERATION | No completion promise set - loop runs infinitely. <halt>why</halt> ON A LINE BY ITSELF ends it (a tag inside a sentence is ignored). If the user told you to stop, halt."
fi
SYSTEM_MSG="${SYSTEM_MSG}${TRANSCRIPT_NOTE}${ITER_WRITE_NOTE}"

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
