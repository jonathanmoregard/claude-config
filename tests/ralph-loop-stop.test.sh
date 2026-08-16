#!/bin/bash
# Test harness for ~/.claude/hooks/ralph-loop-stop.sh
set -uo pipefail

HOOK=~/.claude/hooks/ralph-loop-stop.sh
STATE_DIR="$HOME/.claude/ralph-loop"
SID="test-session-0000"
STATE="$STATE_DIR/$SID.md"
TMP=$(mktemp -d)
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

run_hook() {  # $1 = session id
  # stdout only: the hook's warnings go to stderr, and mixing them into the
  # capture would corrupt the JSON the assertions parse.
  jq -cn --arg s "$1" --arg t "$TRANSCRIPT" '{session_id:$s,transcript_path:$t,stop_hook_active:false,cwd:"/tmp"}' | bash "$HOOK" 2>/dev/null
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
mk_transcript "Work is finished. <promise>ALL DONE</promise>"
OUT=$(run_hook "$SID")
check "promise -> state file removed" "$([[ ! -f "$STATE" ]] && echo pass || echo fail)"
check "promise -> success message" "$(echo "$OUT" | grep -q 'Detected <promise>' && echo pass || echo fail)"

# 5. max iterations reached -> stop
mk_state 25 25 '"ALL DONE"'
mk_transcript "grinding"
OUT=$(run_hook "$SID")
check "max iterations -> state removed" "$([[ ! -f "$STATE" ]] && echo pass || echo fail)"
check "max iterations -> message" "$(echo "$OUT" | grep -q 'Max iterations' && echo pass || echo fail)"

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

rm -f "$STATE"; rm -rf "$TMP"
exit $FAILED
