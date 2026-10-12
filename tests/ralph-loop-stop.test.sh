#!/bin/bash
# Test harness for hooks/ralph-loop-stop.sh
set -uo pipefail

# Test the CHECKOUT this file lives in, not the live ~/.claude: run from a
# worktree, the hook under test is the worktree's copy, or a PR would only ever
# re-verify master. (The hook itself still sources the LIVE resolver by
# absolute path — that is its own guarded design — but the resolver honours
# CLAUDE_RALPH_STATE_DIR, so the state isolation below holds either way.)
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
HOOK="$ROOT/hooks/ralph-loop-stop.sh"
TMP=$(mktemp -d)
# Isolate loop state: without this the suite writes its fake session into the
# LIVE state dir, where a crash mid-run leaves a state file behind.
export CLAUDE_RALPH_STATE_DIR="$TMP/state"
source "$ROOT/scripts/ralph-loop-state.sh"
STATE_DIR="$RALPH_STATE_DIR"
SID="test-session-0000"
STATE="$STATE_DIR/$SID.md"
TRANSCRIPT="$TMP/transcript.jsonl"
FAILED=0

mk_state() {  # $1=iteration $2=max $3=promise-yaml
  mkdir -p "$STATE_DIR"
  cat > "$STATE" <<EOF
---
active: true
iteration: $1
session_id: $SID
armed_in: "/tmp"
max_iterations: $2
completion_promise: $3
started_at: "2026-08-16T00:00:00Z"
---

Do the thing and keep doing it.
EOF
}

mk_transcript() {  # $1 = last assistant text
  printf '%s\n' "$(jq -cn --arg t "$1" '{role:"assistant",message:{content:[{type:"text",text:$t}]}}')" > "$TRANSCRIPT"
}

run_hook() {  # $1 = session id; $2 (optional) = last_assistant_message payload field
  # stdout only: the hook's warnings go to stderr, and mixing them into the
  # capture would corrupt the JSON the assertions parse.
  #
  # Called with one argument the payload OMITS last_assistant_message, which is
  # how a pre-2.1.2xx harness (or a schema regression) looks to the hook — so
  # every one-argument case below doubles as coverage of the transcript
  # fallback path.
  if [[ $# -ge 2 ]]; then
    jq -cn --arg s "$1" --arg t "$TRANSCRIPT" --arg m "$2" \
      '{session_id:$s,transcript_path:$t,stop_hook_active:false,cwd:"/tmp",last_assistant_message:$m}' \
      | bash "$HOOK" 2>/dev/null
  else
    jq -cn --arg s "$1" --arg t "$TRANSCRIPT" \
      '{session_id:$s,transcript_path:$t,stop_hook_active:false,cwd:"/tmp"}' \
      | bash "$HOOK" 2>/dev/null
  fi
}

check() {  # $1=name $2=condition-result
  if [[ "$2" == "pass" ]]; then echo "PASS  $1"; else echo "FAIL  $1"; FAILED=1; fi
}

# 1. no state file -> silent allow
rm -f "$STATE"
mk_transcript "nothing to see"
OUT=$(run_hook "$SID")
check "no state file -> empty output" "$([[ -z "$OUT" ]] && echo pass || echo fail)"

# 2. active loop, no promise -> block + iteration bump
mk_state 1 25 '"ALL DONE"'
mk_transcript "still working on it"
OUT=$(run_hook "$SID")
DEC=$(echo "$OUT" | jq -r '.decision' 2>/dev/null)
REASON=$(echo "$OUT" | jq -r '.reason' 2>/dev/null | sed '/^$/d')
ITER=$(grep '^iteration:' "$STATE" | sed 's/iteration: *//')
check "active loop -> decision=block" "$([[ "$DEC" == "block" ]] && echo pass || echo fail)"
check "active loop -> prompt fed back" "$([[ "$REASON" == "Do the thing and keep doing it." ]] && echo pass || echo fail)"
check "active loop -> iteration bumped to 2" "$([[ "$ITER" == "2" ]] && echo pass || echo fail)"

# 3. other session, same state present -> not bound
OUT=$(run_hook "some-other-session")
ITER=$(grep '^iteration:' "$STATE" | sed 's/iteration: *//')
check "other session -> empty output" "$([[ -z "$OUT" ]] && echo pass || echo fail)"
check "other session -> iteration untouched" "$([[ "$ITER" == "2" ]] && echo pass || echo fail)"

# 4. promise emitted -> loop ends, state removed
#
# ON ITS OWN LINE. A tag inside a sentence is deliberately ignored now: the
# systemMessage injects the promise string every iteration, so quoting it back
# while explaining oneself is ordinary behaviour, and three measured transcripts
# exited the loop with a false completion claim before own-line anchoring landed.
# This case asserted the OLD contract, and the message it looked for
# ("Detected <promise>") stopped being emitted before that.
mk_transcript "Work is finished.
<promise>ALL DONE</promise>"
OUT=$(run_hook "$SID")
check "promise -> state file removed" "$([[ ! -f "$STATE" ]] && echo pass || echo fail)"
check "promise -> success message" "$(echo "$OUT" | grep -q 'completion promise met' && echo pass || echo fail)"

# 5. max iterations reached -> stop
mk_state 25 25 '"ALL DONE"'
mk_transcript "grinding"
OUT=$(run_hook "$SID")
check "max iterations -> state removed" "$([[ ! -f "$STATE" ]] && echo pass || echo fail)"
check "max iterations -> message" "$(echo "$OUT" | grep -q 'max iterations' && echo pass || echo fail)"

# 6. path traversal session id -> refuse, touch nothing
mk_state 1 0 'null'
EVIL="../../../tmp/evil"
OUT=$(run_hook "$EVIL")
check "traversal session id -> empty output" "$([[ -z "$OUT" ]] && echo pass || echo fail)"

# 7. no promise set (null) -> infinite block
mk_transcript "<promise>ALL DONE</promise>"
OUT=$(run_hook "$SID")
DEC=$(echo "$OUT" | jq -r '.decision' 2>/dev/null)
check "promise=null -> still blocks" "$([[ "$DEC" == "block" ]] && echo pass || echo fail)"

# 8. missing / unflushed transcript -> keep looping, do NOT treat as completion.
#    (Reading the transcript once killed loops on iteration 1 about a third of
#    the time in live headless runs, before the flush had landed.)
mk_state 1 0 'null'
TRANSCRIPT="$TMP/nope.jsonl"
OUT=$(run_hook "$SID")
DEC=$(echo "$OUT" | jq -r '.decision' 2>/dev/null)
ITER=$(grep '^iteration:' "$STATE" 2>/dev/null | sed 's/iteration: *//')
check "unreadable transcript -> state kept" "$([[ -f "$STATE" ]] && echo pass || echo fail)"
check "unreadable transcript -> still blocks" "$([[ "$DEC" == "block" ]] && echo pass || echo fail)"
check "unreadable transcript -> iteration bumped" "$([[ "$ITER" == "2" ]] && echo pass || echo fail)"
check "unreadable transcript -> flagged in systemMessage" "$(echo "$OUT" | jq -r '.systemMessage' | grep -q 'transcript unreadable' && echo pass || echo fail)"

# 8b. unreadable transcript still respects max_iterations
mk_state 4 4 'null'
OUT=$(run_hook "$SID")
check "unreadable transcript + max reached -> stops" "$([[ ! -f "$STATE" ]] && echo pass || echo fail)"
rm -f "$STATE"

# 9. multi-line promise across tool-call turns
TRANSCRIPT="$TMP/transcript.jsonl"
mk_state 3 0 '"All tests pass and the reviewer confirmed it."'
{
  jq -cn '{role:"assistant",message:{content:[{type:"text",text:"earlier text"}]}}'
  jq -cn '{role:"assistant",message:{content:[{type:"tool_use",name:"Bash",input:{}}]}}'
  jq -cn '{role:"assistant",message:{content:[{type:"text",text:"Summary line.\n\n<promise>All tests pass\nand the reviewer confirmed it.</promise>"}]}}'
} > "$TRANSCRIPT"
OUT=$(run_hook "$SID")
check "multiline promise normalized -> loop ends" "$([[ ! -f "$STATE" ]] && echo pass || echo fail)"

# 10. FLUSH RACE (session 6f11dbf0, 2026-09-04). The Stop hook can fire before
#     Claude Code flushes the final message, so the transcript's newest text
#     block is the PREVIOUS message and a byte-perfect own-line sigil is
#     rejected — measured live: two valid emissions rejected, iterations 2→4
#     burned, loop ended only by /cancel-ralph. The payload's
#     last_assistant_message arrives synchronously on stdin and cannot be
#     stale; when present it must win over the file.
mk_state 1 25 '"ALL DONE"'
mk_transcript "previous message, no sigil anywhere"
OUT=$(run_hook "$SID" "Handover summary above the tag.
<promise>ALL DONE</promise>")
check "flush race: payload promise + stale transcript -> state removed" "$([[ ! -f "$STATE" ]] && echo pass || echo fail)"
check "flush race: payload promise -> success message" "$(echo "$OUT" | grep -q 'completion promise met' && echo pass || echo fail)"

# 10b. payload promise standing completely alone (the incident's second
#      emission was a bare tag with no surrounding text).
mk_state 1 25 '"ALL DONE"'
mk_transcript "previous message, no sigil anywhere"
OUT=$(run_hook "$SID" "<promise>ALL DONE</promise>")
check "flush race: bare payload promise -> state removed" "$([[ ! -f "$STATE" ]] && echo pass || echo fail)"

# 11. payload is authoritative when present: a sigil visible only in the
#     transcript must NOT end the loop — the payload IS the final message, so
#     anything else in the file is an older message by definition.
mk_state 1 25 '"ALL DONE"'
mk_transcript "<promise>ALL DONE</promise>"
OUT=$(run_hook "$SID" "still working on it")
DEC=$(echo "$OUT" | jq -r '.decision' 2>/dev/null)
check "payload without sigil -> transcript sigil ignored, still blocks" "$([[ "$DEC" == "block" ]] && echo pass || echo fail)"
check "payload without sigil -> state kept" "$([[ -f "$STATE" ]] && echo pass || echo fail)"

# 12. <halt> travels the payload path too.
mk_state 1 0 'null'
mk_transcript "previous message, no halt here"
OUT=$(run_hook "$SID" "<halt>blocked on credentials only a human holds</halt>")
check "payload halt + stale transcript -> state removed" "$([[ ! -f "$STATE" ]] && echo pass || echo fail)"
check "payload halt -> halted message" "$(echo "$OUT" | grep -q 'halted at iteration' && echo pass || echo fail)"

# 13. payload field present but EMPTY (e.g. a turn whose final message is all
#     tool calls) -> fall back to the transcript, exactly as if absent.
mk_state 1 25 '"ALL DONE"'
mk_transcript "Work is finished.
<promise>ALL DONE</promise>"
OUT=$(run_hook "$SID" "")
check "empty payload field -> transcript fallback still ends loop" "$([[ ! -f "$STATE" ]] && echo pass || echo fail)"

rm -f "$STATE"; rm -rf "$TMP"
exit $FAILED
