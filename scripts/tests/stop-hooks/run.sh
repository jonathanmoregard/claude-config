#!/usr/bin/env bash
# Stop-hook interaction tests.
#
# These exist because of a live incident on 2026-08-27 (session ebcdd93d). A
# user said "write the handoff and then stop, do no other work". The mission was
# genuinely incomplete, so the ralph loop's only exits — the completion sigil,
# max_iterations, or the user typing /cancel-ralph — were all unavailable. The
# agent's real choices were to LIE or to DISOBEY. It held for six turns, then
# disobeyed: roughly two hours and 61k tokens. The definition-of-done gate was
# firing on every one of those turns too, pushing the same direction.
#
# Two fixes, one test file:
#   * ralph gained <halt>reason</halt> — an exit that asserts nothing about
#     completion, so "the user told me to stop" has an honest answer.
#   * the dod gate yields when a ralph loop owns the session, so one turn never
#     has two hooks driving it.
#
# The handover-marker protocol that used to sit alongside this was DELETED after
# the review loop stopped with self-inflicted-churn: a lease with no fencing
# token, whose orphan failure was worse than the duplicate block it prevented.
# Duplicate identical blocks are tolerated by the runtime (measured 2026-08-29:
# 22 blocking responses across 11 firings, clean exit), so nothing replaced it.
#
# CLAUDE_RALPH_STATE_DIR keeps all of this out of the live loop directory.
set -uo pipefail

SD=$(mktemp -d)
# finishing-up-arm.sh refuses to arm anything under /tmp — scratch dirs are not
# deliverables — so the arm-hook cases need a repo OUTSIDE $SD, which mktemp
# puts there. Same lifetime, same trap.
ARMWORK=$(mktemp -d -p "$HOME" .dod-armtest.XXXXXX)
trap 'rm -rf "$SD" "$ARMWORK"' EXIT  # arftl-allow: destructive-shell — both are this run's own mktemp -d
export CLAUDE_RALPH_STATE_DIR="$SD/ralph"
mkdir -p "$CLAUDE_RALPH_STATE_DIR"
GATE="${GATE_UNDER_TEST:-$HOME/.claude/hooks/finishing-up-gate.py}"
UNDER_TEST_ROOT=$(cd "$(dirname "$GATE")/.." && pwd -P)
RALPH_HOOK="$UNDER_TEST_ROOT/hooks/ralph-loop-stop.sh"
RALPH_TEST_HOME="$SD/ralph-home"
mkdir -p "$RALPH_TEST_HOME/.claude/hooks" "$RALPH_TEST_HOME/.claude/scripts"
ln -s "$(realpath "$GATE")" \
  "$RALPH_TEST_HOME/.claude/hooks/finishing-up-gate.py"
ln -s "$UNDER_TEST_ROOT/scripts/ralph-loop-state.sh" \
  "$RALPH_TEST_HOME/.claude/scripts/ralph-loop-state.sh"
ln -s "$UNDER_TEST_ROOT/scripts/dod-check.py" \
  "$RALPH_TEST_HOME/.claude/scripts/dod-check.py"
SID="guardtest-$$"

pass=0; fail=0
ck() {
  if [ "$2" = "$3" ]; then printf '  PASS  %s\n' "$1"; pass=$((pass+1))
  else printf '  FAIL  %s\n     want: %s\n     got : %s\n' "$1" "$2" "$3"; fail=$((fail+1)); fi
}
# Absent key and unparseable both render as empty. Printing python's None here
# made "the hook allowed the turn" and "the hook emitted no decision" look like
# two different results when they are the same one.
decision() { printf '%s' "$1" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("decision") or "")
except Exception: print("")' 2>/dev/null; }
reason() { printf '%s' "$1" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("reason") or "")
except Exception: print("")' 2>/dev/null; }
has_text() { grep -Fq -- "$2" <<<"$1" && echo yes || echo no; }

# `decision` alone CANNOT distinguish "allowed the turn" from "crashed": both
# render as empty. Every allow-assertion in this file was therefore satisfied by
# the guarded path throwing — the suite would have stayed green over a hook that
# never ran. `gate_run` captures the exit code too, so an allow must be a
# deliberate exit 0 and not a traceback.
# THE EXIT CODE TRAVELS THROUGH THE FILESYSTEM, NOT THROUGH A VARIABLE.
#
# `GATE_RC=$?` inside this helper was correct-looking and useless: every call
# site is `out=$(gate_run ...)`, a command substitution, which runs in a
# SUBSHELL. The assignment died with it and the parent kept its initial 0, so
# every "exiting 0, not by crashing" assertion passed unconditionally. Measured
# 2026-08-30 by pointing the gate at a stub that raises: 26 assertions went red
# and all 7 rc assertions stayed green — including two sitting directly beneath
# a FAILING sibling on the same call.
#
# That is the exact blindness the comment below describes and this helper was
# written to remove; the helper was born with it (round 3), and round 6's fix
# for it reproduced it one helper along. A variable cannot cross a subshell
# boundary, so the rc goes somewhere that can: a file the parent reads back.
RCFILE="$SD/.last-gate-rc"
printf '0' > "$RCFILE"
gate_rc() { cat "$RCFILE" 2>/dev/null || printf 'no-rc-recorded'; }
gate_run() {  # $1 = session id; extra args are env assignments -> prints stdout
  local sid="$1"; shift
  local out rc
  out=$(printf '{"session_id":"%s"}' "$sid" \
        | env CLAUDE_DOD_STATE_DIR="$SD" "$@" python3 "$GATE" 2>/dev/null)
  rc=$?
  printf '%s' "$rc" > "$RCFILE"
  printf '%s' "$out"
}
payload_run() {  # $1 = complete JSON payload; extra args are env assignments
  local payload="$1"; shift
  local out rc
  out=$(printf '%s' "$payload" \
        | env CLAUDE_DOD_STATE_DIR="$SD" "$@" python3 "$GATE" 2>/dev/null)
  rc=$?
  printf '%s' "$rc" > "$RCFILE"
  printf '%s' "$out"
}
ralph_under_test() {
  HOME="$RALPH_TEST_HOME" bash "$RALPH_HOOK"
}

echo "== dod gate: one driver per turn =="
# A purpose-built repo with a known blocker. These cases used to arm the live
# ~/.claude, which passed only for as long as that checkout happened to be
# dirty — the moment this branch went green the suite went red and said the gate
# was broken. A test whose fixture is the machine you are working on measures
# the machine.
BREPO="$SD/blocked-repo"
mkdir -p "$BREPO"; ( cd "$BREPO" || exit 1
  git init -q -b master .; git config user.email t@t; git config user.name t
  echo hi > a.txt; git add -A; git commit -qm init
  git init -q --bare "$SD/blocked-origin.git"; git remote add origin "$SD/blocked-origin.git"
  git push -q -u origin master
  git symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/master
  echo unfinished >> a.txt; git add -A; git commit -qm "never pushed" ) >/dev/null 2>&1
printf '%s\n' "$BREPO" > "$SD/$SID.arm"

out=$(printf '{"session_id":"%s"}' "$SID" | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE")
ck "blocks by default (active is the default mode)" "block" "$(decision "$out")"

printf -- '---\nactive: true\niteration: 3\nmax_iterations: 25\n---\nprompt\n' \
  > "$CLAUDE_RALPH_STATE_DIR/$SID.md"
out=$(gate_run "$SID")
ck "yields to an armed ralph loop" "" "$out"
ck "  ...by exiting 0, not by crashing" "0" "$(gate_rc)"
rm -f "$CLAUDE_RALPH_STATE_DIR/$SID.md"

# Proof the assertion above can actually fail: a hook that throws produces the
# same empty stdout an allow does. Without the exit code this suite was blind.
BROKEN="$SD/broken-gate.py"
printf 'import sys\nraise SystemExit(1)\n' > "$BROKEN"
printf '{"session_id":"x"}' | python3 "$BROKEN" >/dev/null 2>&1; rc=$?
ck "  ...and a crash is distinguishable" "1" "$rc"

out=$(printf '{"session_id":"%s"}' "$SID" | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE")
ck "blocks again once the loop is gone" "block" "$(decision "$out")"

echo
echo "== dod gate: bounded explanatory exits =="
REENTRYSID="reentry-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$REENTRYSID.arm"

exit_message() { # $1 = one-line JSON envelope; $2 = aggregate state marker
  printf 'Visible explanation for the user.\nDOD-EXIT: %s\n%s' "$1" "$2"
}
run_reentry() { # $1 = session; $2 = last_assistant_message
  local payload
  payload=$(jq -cn --arg sid "$1" --arg msg "$2" \
    '{session_id:$sid,stop_hook_active:true,last_assistant_message:$msg}')
  payload_run "$payload"
}
reject_reentry() { # $1 = assertion; $2 = last_assistant_message
  local out
  rm -f "$SD/$REENTRYSID.blocks"
  out=$(run_reentry "$REENTRYSID" "$2")
  ck "$1" "block" "$(decision "$out")"
}

qjson=$(jq -cn --arg repo "$BREPO" \
  '{outcomes:[{repo:$repo,outcome:"QUESTION",question:"Should this work be preserved?"}]}')
qmsg=$(exit_message "$qjson" QUESTION)
echo 3 > "$SD/$REENTRYSID.blocks"
out=$(run_reentry "$REENTRYSID" "$qmsg")
ck "QUESTION explanation ends the current nudge chain" "" "$(decision "$out")"
ck "  ...by exiting 0, not by crashing" "0" "$(gate_rc)"
ck "  ...resets the current block budget" "gone" \
   "$([ -e "$SD/$REENTRYSID.blocks" ] && echo kept || echo gone)"
ck "  ...without deleting the armfile" "kept" \
   "$([ -e "$SD/$REENTRYSID.arm" ] && echo kept || echo gone)"

bjson=$(jq -cn --arg repo "$BREPO" \
  '{outcomes:[{repo:$repo,outcome:"BLOCKED",blocker:"GitHub login expired",needed:"Jonathan must authenticate"}]}')
out=$(run_reentry "$REENTRYSID" "$(exit_message "$bjson" BLOCKED)")
ck "BLOCKED explanation ends the current nudge chain" "" "$(decision "$out")"

pjson=$(jq -cn --arg repo "$BREPO" \
  '{outcomes:[{repo:$repo,outcome:"PRESERVE",reason:"User requested a later handoff",owner:"Jonathan",next:"Resume from this exact branch"}]}')
out=$(run_reentry "$REENTRYSID" "$(exit_message "$pjson" DONE)")
ck "PRESERVE explanation ends the current nudge chain" "" "$(decision "$out")"

ajson=$(jq -cn --arg repo "$BREPO" \
  '{outcomes:[{repo:$repo,outcome:"ABANDON",reason:"The motivating assumption was disproven",remaining_work:"Unpublished branch remains untouched"}]}')
out=$(run_reentry "$REENTRYSID" "$(exit_message "$ajson" DONE)")
ck "ABANDON explanation ends the current nudge chain" "" "$(decision "$out")"

reject_reentry "re-entry without an explainer keeps nudging" \
  $'I have nothing more to add.\nDONE'
reject_reentry "malformed JSON keeps nudging" \
  $'Explanation.\nDOD-EXIT: {oops}\nDONE'
badjson=$(jq -cn --arg repo "$BREPO" \
  '{outcomes:[{repo:$repo,outcome:"PAUSE",reason:"Unknown outcome"}]}')
reject_reentry "an unknown outcome keeps nudging" \
  "$(exit_message "$badjson" DONE)"
badjson=$(jq -cn --arg repo "$BREPO" \
  '{outcomes:[{repo:$repo,outcome:"BLOCKED",blocker:"GitHub login expired"}]}')
reject_reentry "missing required outcome fields keep nudging" \
  "$(exit_message "$badjson" BLOCKED)"
badjson=$(jq -cn --arg repo "$BREPO" \
  '{outcomes:[{repo:$repo,outcome:"ABANDON",reason:"Assumption disproven",remaining_work:"None",extra:true}]}')
reject_reentry "extra outcome fields keep nudging" \
  "$(exit_message "$badjson" DONE)"
badjson=$(jq -cn --arg repo "$BREPO" \
  '{outcomes:[{repo:$repo,outcome:"QUESTION",question:42}]}')
reject_reentry "wrong-typed outcome fields keep nudging" \
  "$(exit_message "$badjson" QUESTION)"
badjson=$(jq -cn --arg repo "$BREPO" \
  '{outcomes:[{repo:$repo,outcome:"QUESTION",question:"Choose an outcome"}]}')
reject_reentry "QUESTION must contain an actual question" \
  "$(exit_message "$badjson" QUESTION)"
reject_reentry "aggregate state must match outcomes" \
  "$(exit_message "$qjson" DONE)"
reject_reentry "a quoted marker is not an exit" \
  "$(printf 'Explanation.\n> DOD-EXIT: %s\nQUESTION' "$qjson")"
reject_reentry "a non-final envelope is not an exit" \
  "$(printf 'DOD-EXIT: %s\nextra line\nQUESTION' "$qjson")"

rm -f "$SD/$REENTRYSID.blocks"
payload=$(jq -cn --arg sid "$REENTRYSID" \
  '{session_id:$sid,stop_hook_active:true,last_assistant_message:{text:"not a string"}}')
out=$(payload_run "$payload")
ck "a non-string final message keeps nudging" "block" "$(decision "$out")"

# Exit coverage is per measured blocker, not per session. A clean armed repo
# needs no record; adding one is rejected because stale paths cannot widen the
# envelope.
BREPO2="$SD/blocked-repo-2"
git clone -q "$SD/blocked-origin.git" "$BREPO2" >/dev/null 2>&1
( cd "$BREPO2" || exit 1
  git config user.email t@t; git config user.name t
  echo unfinished >> a.txt; git add -A; git commit -qm "second never pushed" )
CREPO="$SD/clean-repo"
git clone -q "$SD/blocked-origin.git" "$CREPO" >/dev/null 2>&1
MULTISID="multi-exit-$RANDOM"
printf '%s\n%s\n%s\n' "$BREPO" "$BREPO2" "$CREPO" > "$SD/$MULTISID.arm"
multi_run() { # $1 = envelope JSON; $2 = aggregate marker
  rm -f "$SD/$MULTISID.blocks"
  run_reentry "$MULTISID" "$(exit_message "$1" "$2")"
}
completejson=$(jq -cn --arg a "$BREPO" --arg b "$BREPO2" \
  '{outcomes:[
    {repo:$a,outcome:"PRESERVE",reason:"User requested a later handoff",owner:"Jonathan",next:"Resume from this exact branch"},
    {repo:$b,outcome:"BLOCKED",blocker:"Remote access is unavailable",needed:"Jonathan must restore credentials"}
  ]}')
out=$(multi_run "$completejson" BLOCKED)
ck "complete multi-repo coverage exits with aggregate BLOCKED" "" "$(decision "$out")"

missingjson=$(jq -cn --arg a "$BREPO" \
  '{outcomes:[{repo:$a,outcome:"PRESERVE",reason:"User requested a later handoff",owner:"Jonathan",next:"Resume from this exact branch"}]}')
out=$(multi_run "$missingjson" DONE)
ck "one explained repo cannot hide another blocker" "block" "$(decision "$out")"
duplicatejson=$(jq -cn --arg a "$BREPO" --arg b "$BREPO2" \
  '{outcomes:[
    {repo:$a,outcome:"PRESERVE",reason:"User requested a later handoff",owner:"Jonathan",next:"Resume from this exact branch"},
    {repo:$a,outcome:"ABANDON",reason:"Assumption was disproven",remaining_work:"Unpublished branch remains untouched"},
    {repo:$b,outcome:"BLOCKED",blocker:"Remote access is unavailable",needed:"Jonathan must restore credentials"}
  ]}')
out=$(multi_run "$duplicatejson" BLOCKED)
ck "duplicate repo records reject the whole exit" "block" "$(decision "$out")"
unknownjson=$(jq -cn --arg a "$BREPO" --arg b "$BREPO2" \
  '{outcomes:[
    {repo:$a,outcome:"PRESERVE",reason:"User requested a later handoff",owner:"Jonathan",next:"Resume from this exact branch"},
    {repo:$b,outcome:"BLOCKED",blocker:"Remote access is unavailable",needed:"Jonathan must restore credentials"},
    {repo:"/unknown/repository",outcome:"ABANDON",reason:"Assumption was disproven",remaining_work:"No local work exists"}
  ]}')
out=$(multi_run "$unknownjson" BLOCKED)
ck "unknown repo records reject the whole exit" "block" "$(decision "$out")"
cleanjson=$(jq -cn --arg a "$BREPO" --arg b "$BREPO2" --arg c "$CREPO" \
  '{outcomes:[
    {repo:$a,outcome:"PRESERVE",reason:"User requested a later handoff",owner:"Jonathan",next:"Resume from this exact branch"},
    {repo:$b,outcome:"BLOCKED",blocker:"Remote access is unavailable",needed:"Jonathan must restore credentials"},
    {repo:$c,outcome:"PRESERVE",reason:"Clean repo needs no exit",owner:"Jonathan",next:"No action is required"}
  ]}')
out=$(multi_run "$cleanjson" BLOCKED)
ck "already-clean repo records reject the whole exit" "block" "$(decision "$out")"

# The envelope is scoped to this runtime continuation. Replaying the same final
# text on a later independent Stop must measure again.
rm -f "$SD/$REENTRYSID.blocks"
payload=$(jq -cn --arg sid "$REENTRYSID" --arg msg "$qmsg" \
  '{session_id:$sid,stop_hook_active:false,last_assistant_message:$msg}')
out=$(payload_run "$payload")
ck "a later Stop cannot replay the prior exit" "block" "$(decision "$out")"

out=$(payload_run "$(printf '{"session_id":"%s","stop_hook_active":false}' "$REENTRYSID")")
ck "boolean false still measures and blocks" "block" "$(decision "$out")"
message=$(reason "$out")
out=$(payload_run "$(printf '{"session_id":"%s","stop_hook_active":"true"}' "$REENTRYSID")")
ck "a truthy string cannot spoof Stop re-entry" "block" "$(decision "$out")"
out=$(payload_run "$(printf '{"session_id":"%s"}' "$REENTRYSID")")
ck "an absent re-entry field keeps compatibility" "block" "$(decision "$out")"
out=$(payload_run \
  "$(jq -cn --arg sid "$REENTRYSID" --arg msg "$qmsg" \
      '{session_id:$sid,stop_hook_active:true,last_assistant_message:$msg}')" \
  CLAUDE_DOD_HANDOVER=1)
ck "an explicit ralph handover still measures" "block" "$(decision "$out")"

# An accepted exit is not allowed to strand the next turn at a spent budget.
# Make the state directory non-writable so unlinking the countfile fails.
RESETSD="$SD/reset-failure"
mkdir -p "$RESETSD"
RESETSID="reset-failure-$RANDOM"
printf '%s\n' "$BREPO" > "$RESETSD/$RESETSID.arm"
echo 6 > "$RESETSD/$RESETSID.blocks"
resetmsg=$(exit_message \
  "$(jq -cn --arg repo "$BREPO" \
    '{outcomes:[{repo:$repo,outcome:"PRESERVE",reason:"User requested a later handoff",owner:"Jonathan",next:"Resume from this exact branch"}]}')" \
  DONE)
payload=$(jq -cn --arg sid "$RESETSID" --arg msg "$resetmsg" \
  '{session_id:$sid,stop_hook_active:true,last_assistant_message:$msg}')
chmod 500 "$RESETSD"
out=$(payload_run "$payload" CLAUDE_DOD_STATE_DIR="$RESETSD")
chmod 700 "$RESETSD"
ck "a failed budget reset rejects the exit" "block" "$(decision "$out")"
ck "  ...and names the reset failure" "yes" \
  "$(has_text "$(reason "$out")" "block budget could not be reset")"

ck "block guidance names Ship" "yes" "$(has_text "$message" "Ship")"
ck "block guidance names Preserve" "yes" "$(has_text "$message" "Preserve")"
ck "block guidance names Abandon" "yes" "$(has_text "$message" "Abandon")"
ck "block guidance names Question" "yes" "$(has_text "$message" "Question")"
ck "block guidance names Blocked" "yes" "$(has_text "$message" "Blocked")"
ck "block guidance gives exact explanatory-exit syntax" "yes" \
   "$(has_text "$message" "DOD-EXIT:")"
ck "Abandon never grants deletion authority" "yes" \
   "$(has_text "$message" "Abandon does not authorize deletion")"
ck "an exit is scoped to the current Stop chain" "yes" \
   "$(has_text "$message" "current Stop chain")"
ck "a PR is never opened only to clear the gate" "yes" \
   "$(has_text "$message" "Never push or open a PR solely to clear this gate.")"
ck "deletion requires explicit user approval" "yes" \
   "$(has_text "$message" "Never delete work without explicit user approval.")"
ck "PR URLs are conditional on an existing PR" "yes" \
   "$(has_text "$message" "Include a PR URL only when one exists.")"
ck "in-flight checks get one reporting follow-up" "yes" \
   "$(has_text "$message" "one durable reporting follow-up")"
ck "old single-outcome instruction is gone" "no" \
   "$(has_text "$message" "drive to green and mergeable")"
ck "old unconditional PR-link demand is gone" "no" \
   "$(has_text "$message" "with the clickable PR URL")"
rm -f "$SD/$REENTRYSID.arm" "$SD/$REENTRYSID.blocks" \
      "$SD/$MULTISID.arm" "$SD/$MULTISID.blocks"

out=$(printf '{"session_id":"%s"}' "$SID" | CLAUDE_DOD_STATE_DIR="$SD" CLAUDE_DOD_MODE=shadow python3 "$GATE")
case "$out" in *'"systemMessage"'*) r=shadow ;; *) r="$out" ;; esac
ck "shadow is still available as an opt-out" "shadow" "$r"

echo
echo "== config that cannot be read FAILS CLOSED =="
# Every one of these used to end the turn UNGUARDED. The numeric vars are parsed
# at import, so a ValueError there exited non-zero with no output and the
# harness simply carried on; and MODE compared only against "active", so one
# typo degraded to shadow permanently and said nothing. A guard disabled by a
# stray character in a shell profile is not a guard.
out=$(printf '{"session_id":"%s"}' "$SID" \
      | CLAUDE_DOD_STATE_DIR="$SD" CLAUDE_DOD_MAX_BLOCKS=x python3 "$GATE")
ck "garbage MAX_BLOCKS still blocks"     "block" "$(decision "$out")"
out=$(printf '{"session_id":"%s"}' "$SID" \
      | CLAUDE_DOD_STATE_DIR="$SD" CLAUDE_DOD_ARM_MAX_AGE_DAYS=abc python3 "$GATE")
ck "garbage ARM_MAX_AGE_DAYS still blocks" "block" "$(decision "$out")"
# The one that mattered most: shadow must be asked for BY NAME.
out=$(printf '{"session_id":"%s"}' "$SID" \
      | CLAUDE_DOD_STATE_DIR="$SD" CLAUDE_DOD_MODE=activ python3 "$GATE")
ck "a MODE typo fails closed to active"  "block" "$(decision "$out")"
ck "and says which value it rejected"    "yes" \
   "$(grep -q "CLAUDE_DOD_MODE='activ' unrecognised" "$SD/gate.log" && echo yes || echo no)"
# A tiny positive age would reap every other live session's state.
touch -d "@$(( $(date +%s) - 7200 ))" "$SD/twohours.arm"
printf '{"session_id":"%s"}' "$SID" \
  | CLAUDE_DOD_STATE_DIR="$SD" CLAUDE_DOD_ARM_MAX_AGE_DAYS=0.001 python3 "$GATE" >/dev/null 2>&1
ck "a sub-day reap age is floored"       "kept" \
   "$([ -e "$SD/twohours.arm" ] && echo kept || echo gone)"
rm -f "$SD/twohours.arm"
# dod-check that emits a non-object must not crash the hook. Its OWN session id:
# $SID has been blocking all through this section and its 6-block budget is
# spent, so reusing it here would read "stood down on budget" as "did not block"
# and quietly assert nothing.
printf 'print("[]")\n' > "$SD/fake-dod.py"
NSID="nonobj-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$NSID.arm"
out=$(printf '{"session_id":"%s"}' "$NSID" \
      | CLAUDE_DOD_STATE_DIR="$SD" CLAUDE_DOD_CHECK="$SD/fake-dod.py" python3 "$GATE")
ck "non-object dod-check output blocks"  "block" "$(decision "$out")"
ck "and names the shape it got"          "yes" \
   "$(grep -q 'emitted list, expected an object' "$SD/gate.log" && echo yes || echo no)"
rm -f "$SD/$NSID.arm" "$SD/$NSID.blocks"

echo
echo "== ralph: an exit that does not claim completion =="
probe() {  # $1 = assistant text -> "gone" (loop ended) | "kept" (still looping)
  local T="$SD/t-$RANDOM.jsonl" S="halt-$RANDOM"
  # Compact separators are load-bearing: the hook greps for the literal
  # '"role":"assistant"', which json.dumps' default '"role": "assistant"' misses.
  python3 - "$T" "$1" <<'PY'
import json, sys
rec = {"role": "assistant", "message": {"content": [{"type": "text", "text": sys.argv[2]}]}}
open(sys.argv[1], "w").write(json.dumps(rec, separators=(",", ":")) + "\n")
PY
  printf -- '---\nactive: true\niteration: 4\nmax_iterations: 25\ncompletion_promise: "ALL DONE"\n---\nkeep going\n' \
    > "$CLAUDE_RALPH_STATE_DIR/$S.md"
  # CLAUDE_DOD_STATE_DIR is load-bearing here: end_loop calls the gate, and
  # without it that inner call reads the LIVE ~/.local/state/claude-dod — an
  # isolation gap in a file whose own header claims isolation.
  printf '{"session_id":"%s","transcript_path":"%s"}' "$S" "$T" \
    | CLAUDE_DOD_STATE_DIR="$SD" ralph_under_test >/dev/null 2>&1
  if [ -f "$CLAUDE_RALPH_STATE_DIR/$S.md" ]; then rm -f "$CLAUDE_RALPH_STATE_DIR/$S.md"; echo kept
  else echo gone; fi
}

ck "<halt> on its own line ends the loop" gone \
   "$(probe 'Stopping.
<halt>the user told me to stop and the mission is incomplete</halt>')"
# A bare tag is not an escape hatch: an exit has to say why, or the audit trail
# is worthless and "halt" becomes a cheaper sigil than the sigil.
ck "bare <halt> does NOT exit"          kept "$(probe 'Stopping.
<halt></halt>')"
ck "no tag keeps looping"               kept "$(probe 'Holding.')"
ck "a true promise still exits"         gone "$(probe '<promise>ALL DONE</promise>')"
ck "a false promise does not exit"      kept "$(probe '<promise>something else</promise>')"

# The systemMessage injects the literal promise string EVERY iteration, so the
# model is reading it every turn. Quoting it back while explaining itself is
# ordinary behaviour, not an edge case. All four of these used to EXIT the loop
# with a false completion claim — including a model explicitly REFUSING to make
# one, and one that never used a tag at all.
ck "a QUOTED promise does not exit"     kept \
   "$(probe 'I am not done, so I will not output <promise>ALL DONE</promise> yet.')"
ck "explaining the rule does not exit"  kept \
   "$(probe 'To stop the loop I must emit <promise>ALL DONE</promise> only when true.')"
ck "bare promise text does not exit"    kept "$(probe 'ALL DONE')"
# A fenced block is how a model DISPLAYS a literal tag, and the systemMessage
# invites discussing these freely. Own-line anchoring alone matched inside the
# fence, so demonstrating the tag exited the loop with a false completion claim.
ck "a FENCED promise does not exit"     kept "$(probe 'To stop you write:

```
<promise>ALL DONE</promise>
```

I am not doing that.')"
ck "a fenced halt does not exit"        kept "$(probe 'Example:

~~~
<halt>giving up</halt>
~~~

Not yet.')"
ck "an inline-code tag does not exit"   kept "$(probe 'The tag is
`<promise>ALL DONE</promise>`
which I will not emit.')"
ck "a blockquoted tag does not exit"    kept "$(probe '> <promise>ALL DONE</promise>

That is what it would look like.')"
# Four+ leading spaces is a markdown code block: a demonstration, not a claim.
ck "an indented tag does not exit"      kept "$(probe 'Like so:

    <promise>ALL DONE</promise>')"
# Fence chars are tracked separately: a ``` fence DISPLAYED inside a ~~~ fence
# used to flip the single boolean closed, so the demo tag read as a real claim.
ck "a nested fence of the other char does not exit" kept "$(probe '~~~
```
<promise>ALL DONE</promise>
```
~~~')"
# CRLF: perl -n keeps the trailing CR, so a genuine claim never matched the
# end-anchor and the loop could only ever end at max_iterations.
# (This case previously spilled its second half onto its own line, where the
#  shell ran "gone" as a command and the assertion passed regardless.)
CRLF_OUT=$(probe "$(printf '<promise>ALL DONE</promise>
')")
ck "CRLF still lets a real promise exit" gone "$CRLF_OUT"
ck "a QUOTED halt does not exit"        kept \
   "$(probe 'I could emit <halt>give up</halt> here but I will not.')"
# Discussing the rule and THEN genuinely claiming it must still work.
ck "discuss-then-claim still exits"     gone \
   "$(probe 'The rule says emit <promise>ALL DONE</promise> only when true. It is.
<promise>ALL DONE</promise>')"
# Own-line anchoring is about the tag not being INSIDE A SENTENCE. A promise
# whose text wraps is still a tag that opens a line and closes one — and the
# promise string is injected into the systemMessage every iteration, so a long
# one comes back wrapped as a matter of course. Round 3 of this run replaced a
# `perl -0777` slurp with a line-at-a-time matcher and took multi-line claims
# out with the fenced ones; the only test that covered it lives outside these
# three suites, so nothing here noticed for three rounds.
ck "a promise wrapped over two lines still exits" gone "$(probe '<promise>ALL
DONE</promise>')"
# ...and the sentence case still must not. The closing tag does not end its
# line, so this is prose about the tag, not a claim.
ck "a wrapped tag inside prose does not exit" kept "$(probe 'I could write
<promise>ALL
DONE</promise> here but it would not be true.')"

if [ -s "$CLAUDE_RALPH_STATE_DIR/halts.log" ]; then
  ck "the halt reason is recorded" "yes" \
     "$(grep -qF 'the user told me to stop' "$CLAUDE_RALPH_STATE_DIR/halts.log" && echo yes || echo no)"
else
  ck "the halt reason is recorded" "yes" "no halts.log"
fi

echo
echo "== a REAL loop end hands over and leaves nothing behind =="
# Measured against a real end_loop, not a planted fixture. The marker that used
# to live here is gone; what must still hold is that ending the loop hands the
# turn to the gate, removes the loop state, and leaves the next turn gated.
RSID="realend-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$RSID.arm"
printf -- '---\nactive: true\niteration: 4\nmax_iterations: 25\ncompletion_promise: "ALL DONE"\n---\np\n' \
  > "$CLAUDE_RALPH_STATE_DIR/$RSID.md"
RT="$SD/realend.jsonl"
python3 - "$RT" <<'PY'
import json, sys
rec = {"role": "assistant", "message": {"content": [
    {"type": "text", "text": "<promise>ALL DONE</promise>"}]}}
open(sys.argv[1], "w").write(json.dumps(rec, separators=(",", ":")) + "\n")
PY
# The concurrent gate wins the race, as it does in practice.
out=$(gate_run "$RSID")
ck "concurrent gate stands down for the live loop" "" "$(decision "$out")"
ck "  ...by exiting 0, not by crashing" "0" "$(gate_rc)"
printf '{"session_id":"%s","transcript_path":"%s"}' "$RSID" "$RT" \
  | CLAUDE_DOD_STATE_DIR="$SD" ralph_under_test >/dev/null 2>&1
ck "end_loop leaves no loop state behind" "gone" \
   "$([ -e "$CLAUDE_RALPH_STATE_DIR/$RSID.md" ] && echo kept || echo gone)"
out=$(gate_run "$RSID")
ck "the turn after a real loop end is gated" "block" "$(decision "$out")"
rm -f "$SD/$RSID.arm" "$SD/$RSID.blocks"

# A frontmatter line missing entirely used to kill the hook under
# `set -euo pipefail` (grep exits 1) BEFORE its own corruption handler ran, so
# the state file survived and the gate yielded to a dead loop every turn.
CSID="corrupt-$RANDOM"
printf -- '---\nactive: true\nmax_iterations: 25\n---\np\n' \
  > "$CLAUDE_RALPH_STATE_DIR/$CSID.md"
printf '{"session_id":"%s","transcript_path":"%s"}' "$CSID" "$RT" \
  | CLAUDE_DOD_STATE_DIR="$SD" ralph_under_test >/dev/null 2>&1
ck "a missing frontmatter line ends the loop" "gone" \
   "$([ -e "$CLAUDE_RALPH_STATE_DIR/$CSID.md" ] && echo kept || echo gone)"

echo
echo "== a checker that could not run has not reported done =="
# The handover turn is the one the whole marker protocol exists to cover, and
# the loop state is already deleted by the time the gate is called — so nothing
# revisits it. The first version of this merely WARNED in a systemMessage and
# let the turn end. Unmeasurable is not done here for the same reason it is not
# done anywhere else.
MSID2="gatefail-$RANDOM"
MT="$SD/gatefail.jsonl"
python3 - "$MT" <<'PY'
import json, sys
rec = {"role": "assistant", "message": {"content": [
    {"type": "text", "text": "<promise>ALL DONE</promise>"}]}}
open(sys.argv[1], "w").write(json.dumps(rec, separators=(",", ":")) + "\n")
PY
printf -- '---\nactive: true\niteration: 4\nmax_iterations: 25\ncompletion_promise: "ALL DONE"\n---\np\n' \
  > "$CLAUDE_RALPH_STATE_DIR/$MSID2.md"
printf '%s\n' "$BREPO" > "$SD/$MSID2.arm"
# A gate that is not there at all. The fake HOME still needs ralph's own state
# resolver: without it the hook dies at `source` under set -e, and this case
# would measure that instead of the missing gate.
MISS_HOME="$SD/misshome"; mkdir -p "$MISS_HOME/.claude/hooks" "$MISS_HOME/.claude/scripts"
cp "$UNDER_TEST_ROOT/scripts/ralph-loop-state.sh" "$MISS_HOME/.claude/scripts/"
out=$(printf '{"session_id":"%s","transcript_path":"%s"}' "$MSID2" "$MT" \
      | CLAUDE_DOD_STATE_DIR="$SD" HOME="$MISS_HOME" bash "$RALPH_HOOK" 2>/dev/null)
ck "a missing gate BLOCKS the handover turn" "block" "$(decision "$out")"
ck "the loop still ended"                    "gone" \
   "$([ -e "$CLAUDE_RALPH_STATE_DIR/$MSID2.md" ] && echo kept || echo gone)"

# A gate that crashes.
MSID3="gatecrash-$RANDOM"
printf -- '---\nactive: true\niteration: 4\nmax_iterations: 25\ncompletion_promise: "ALL DONE"\n---\np\n' \
  > "$CLAUDE_RALPH_STATE_DIR/$MSID3.md"
CRASH_HOME="$SD/crashhome"; mkdir -p "$CRASH_HOME/.claude/hooks" "$CRASH_HOME/.claude/scripts"
printf 'raise RuntimeError("boom")\n' > "$CRASH_HOME/.claude/hooks/finishing-up-gate.py"
cp "$UNDER_TEST_ROOT/scripts/ralph-loop-state.sh" "$CRASH_HOME/.claude/scripts/"
out=$(printf '{"session_id":"%s","transcript_path":"%s"}' "$MSID3" "$MT" \
      | CLAUDE_DOD_STATE_DIR="$SD" HOME="$CRASH_HOME" bash "$RALPH_HOOK" 2>/dev/null)
ck "a crashing gate BLOCKS the handover turn" "block" "$(decision "$out")"

# And the same principle for the gate's own corrupt state.
CSID2="badarm-$RANDOM"
printf '\xff\xfe not utf8\n' > "$SD/$CSID2.arm"
out=$(gate_run "$CSID2")
ck "an unreadable armfile BLOCKS, never allows" "block" "$(decision "$out")"

# The last silent allows: payloads this gate cannot identify. It still has to
# end the turn — no session id means no armfile means nothing to measure — but
# "we could not read what the harness sent" must not look like "this session had
# no work". A sweep of all eleven turn-ending paths found these four were the
# only ones that did not log.
printf 'not json at all' | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE" >/dev/null 2>&1
ck "unparseable stdin is logged, not silent" "yes" \
   "$(grep -q 'stdin was not valid JSON' "$SD/gate.log" && echo yes || echo no)"
printf '[1,2,3]' | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE" >/dev/null 2>&1
ck "a non-object payload is logged"          "yes" \
   "$(grep -q 'not an object' "$SD/gate.log" && echo yes || echo no)"
printf '{"session_id": 42}' | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE" >/dev/null 2>&1
ck "a non-string session_id is logged"       "yes" \
   "$(grep -q 'not a string' "$SD/gate.log" && echo yes || echo no)"
printf '{"session_id":"../../etc/passwd"}' | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE" >/dev/null 2>&1
ck "a path-shaped session_id is refused and logged" "yes" \
   "$(grep -q 'not a plain token' "$SD/gate.log" && echo yes || echo no)"
ck "and says nothing was checked"              "yes" \
   "$(grep -q 'NOTHING WAS CHECKED' "$SD/gate.log" && echo yes || echo no)"
rm -f "$SD/$CSID2.arm" "$SD/$CSID2.blocks" "$SD/$MSID2.arm" "$SD/$MSID2.blocks"

echo
echo "== the budget does not apply to the handover =="
# The block budget stops the gate arguing with a session turn after turn. A
# handover is not another turn in that argument: it happens once, when a loop
# ends, and it is the LAST chance anything measures the work. Honouring the
# budget there let a loop that had already spent it end its final turn
# unmeasured, with the loop state already deleted so nothing would revisit it.
BSID="budget-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$BSID.arm"
echo 99 > "$SD/$BSID.blocks"
out=$(gate_run "$BSID")
ck "a spent budget stands the ordinary turn down" "" "$(decision "$out")"
out=$(printf '{"session_id":"%s"}' "$BSID" \
      | CLAUDE_DOD_STATE_DIR="$SD" CLAUDE_DOD_HANDOVER=1 python3 "$GATE" 2>/dev/null)
ck "but the HANDOVER still checks"               "block" "$(decision "$out")"
ck "and says why it overrode the budget"         "yes" \
   "$(grep -q "this is the loop's handover" "$SD/gate.log" && echo yes || echo no)"
rm -f "$SD/$BSID.arm" "$SD/$BSID.blocks"

# The reaper must not disarm a session whose loop is still running: a long loop
# can outlive the reap age while still working, and losing its armfile means its
# eventual handover finds nothing to check.
LSID="liveloop-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$LSID.arm"
touch -d "@$(( $(date +%s) - 40*86400 ))" "$SD/$LSID.arm"
printf -- '---\nactive: true\niteration: 9\nmax_iterations: 500\n---\np\n' \
  > "$CLAUDE_RALPH_STATE_DIR/$LSID.md"
printf '%s\n' "$BREPO" > "$SD/$SID.arm"   # re-arm the reaping session
printf '{"session_id":"%s"}' "$SID" | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE" >/dev/null 2>&1
ck "a live loop's aged armfile is NOT reaped" "kept" \
   "$([ -e "$SD/$LSID.arm" ] && echo kept || echo gone)"
rm -f "$CLAUDE_RALPH_STATE_DIR/$LSID.md" "$SD/$LSID.arm"
# ...but with no live loop, the same aged armfile goes.
DSID="deadloop-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$DSID.arm"
touch -d "@$(( $(date +%s) - 40*86400 ))" "$SD/$DSID.arm"
printf '{"session_id":"%s"}' "$SID" | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE" >/dev/null 2>&1
ck "an aged armfile with no loop IS reaped"   "gone" \
   "$([ -e "$SD/$DSID.arm" ] && echo kept || echo gone)"

echo
echo "== stand-down fields must be the SHAPE they claim =="
# Both fields are undocumented — read out of the installed binary — so their
# shape is precisely the thing not to assume. Trusted by truthiness, ANY truthy
# value stood the gate down and logged a clean `allow`: a dict, a string,
# anything at all. Found by the cross-vendor reviewer after four Opus rounds.
# Its own session id: a spent block budget would read as "did not block".
SHSID="shape-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$SHSID.arm"
# Captures the EXIT CODE, like gate_run. Piping the gate straight into the
# decision reader threw that away, and an allow and a traceback both render as
# empty — so the allow-assertion below was satisfiable by a crash. That is the
# exact blindness this suite fixed in decision() a round earlier and then
# reintroduced one helper along.
shape_dec() {
  local out rc
  out=$(printf '%s' "$1" | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE" 2>/dev/null)
  rc=$?
  printf '%s' "$rc" > "$RCFILE"   # a variable would not survive $(shape_dec ...)
  decision "$out"
}
ck "a dict session_crons does NOT stand it down"   "block" \
   "$(shape_dec "$(printf '{"session_id":"%s","session_crons":{"oops":1}}' "$SHSID")")"
ck "a string background_tasks does NOT either"     "block" \
   "$(shape_dec "$(printf '{"session_id":"%s","background_tasks":"x"}' "$SHSID")")"
ck "an empty list does NOT stand it down"          "block" \
   "$(shape_dec "$(printf '{"session_id":"%s","session_crons":[]}' "$SHSID")")"
ck "a real non-empty list DOES"                    "" \
   "$(shape_dec "$(printf '{"session_id":"%s","background_tasks":[{"id":"t"}]}' "$SHSID")")"
ck "  ...by exiting 0, not by crashing"            "0" "$(gate_rc)"
ck "and the bad shape is named in the log"         "yes" \
   "$(grep -q 'unexpected shape' "$SD/gate.log" && echo yes || echo no)"
rm -f "$SD/$SHSID.arm" "$SD/$SHSID.blocks"

echo
echo "== reaping abandoned arm state =="
# A session that ends while still blocked never reaches the clean pass that
# deletes its armfile, so the dir grows forever. Reaping is age-based and
# self-healing: the arm hook rewrites the file on the next edit.
OLD=$(( $(date +%s) - 30*86400 ))
touch -d "@$OLD" "$SD/deadsession.arm" "$SD/deadsession.blocks"
printf '%s\n' "$BREPO" > "$SD/fresh-other.arm"
LOGSIZE_BEFORE=$(wc -c < "$SD/gate.log" 2>/dev/null || echo 0)

printf '{"session_id":"%s"}' "$SID" | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE" >/dev/null 2>&1

ck "a 30-day-old armfile is reaped"      "gone" "$([ -e "$SD/deadsession.arm" ] && echo kept || echo gone)"
ck "its orphaned .blocks goes too"       "gone" "$([ -e "$SD/deadsession.blocks" ] && echo kept || echo gone)"
ck "a fresh armfile survives"            "kept" "$([ -e "$SD/fresh-other.arm" ] && echo kept || echo gone)"
ck "the reaping session keeps its own"   "kept" "$([ -e "$SD/$SID.arm" ] && echo kept || echo gone)"
ck "gate.log is never reaped"            "kept" "$([ -e "$SD/gate.log" ] && echo kept || echo gone)"
ck "the reap is recorded"                "yes"  "$(grep -q '"action": "reap"' "$SD/gate.log" && echo yes || echo no)"
# The audit trail must only ever grow.
# -gt, not -ge: the preceding gate run always logs, so an unchanged file means
# the log was NOT appended to. `-ge` was satisfied by a no-op and could only
# ever have caught an outright truncation.
ck "gate.log was appended, not rewritten" "yes" \
   "$([ "$(wc -c < "$SD/gate.log")" -gt "$LOGSIZE_BEFORE" ] && echo yes || echo no)"

# Disabling it is one env var, and an unarmed session must never scan at all.
touch -d "@$OLD" "$SD/deadsession2.arm"
printf '{"session_id":"%s"}' "$SID" \
  | CLAUDE_DOD_STATE_DIR="$SD" CLAUDE_DOD_ARM_MAX_AGE_DAYS=0 python3 "$GATE" >/dev/null 2>&1
ck "age 0 disables reaping" "kept" "$([ -e "$SD/deadsession2.arm" ] && echo kept || echo gone)"

printf '{"session_id":"unarmed-nobody"}' | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE" >/dev/null 2>&1
ck "an unarmed session reaps nothing" "kept" "$([ -e "$SD/deadsession2.arm" ] && echo kept || echo gone)"
rm -f "$SD/deadsession2.arm" "$SD/fresh-other.arm"

echo
echo "== ralph hands the finished turn to the gate =="
# The point of the handover. Stop hooks run CONCURRENTLY (measured on 2.1.247:
# the hook declared second started 111ms before the first and ran while it
# slept), so "ralph deletes its state file, the gate notices" is a race whose
# losing side is silence on the one turn that matters. Ralph therefore calls the
# gate itself, and the gate's verdict decides whether the turn may end.
HREPO="$SD/handoff-repo"
mkdir -p "$HREPO"; ( cd "$HREPO" || exit 1
  git init -q -b master .; git config user.email t@t; git config user.name t
  echo hi > a.txt; git add -A; git commit -qm init
  git init -q --bare "$SD/handoff-origin.git"; git remote add origin "$SD/handoff-origin.git"
  git push -q -u origin master
  git symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/master
  echo unfinished >> a.txt; git add -A; git commit -qm "never pushed" ) >/dev/null 2>&1

end_via() {  # $1 = assistant text that ends the loop -> the hook's stdout
  local T="$SD/h-$RANDOM.jsonl" S="handoff-$RANDOM"
  python3 - "$T" "$1" <<'PY'
import json, sys
rec = {"role": "assistant", "message": {"content": [{"type": "text", "text": sys.argv[2]}]}}
open(sys.argv[1], "w").write(json.dumps(rec, separators=(",", ":")) + "\n")
PY
  printf -- '---\nactive: true\niteration: 4\nmax_iterations: 25\ncompletion_promise: "ALL DONE"\n---\nkeep going\n' \
    > "$CLAUDE_RALPH_STATE_DIR/$S.md"
  printf '%s\n' "$HREPO" > "$SD/$S.arm"
  # Exercise branch gate's HANDOVER exception through ralph, not only through
  # a direct gate call: ordinary re-entry stands down, handover must not.
  printf '{"session_id":"%s","transcript_path":"%s","stop_hook_active":true}' "$S" "$T" \
    | CLAUDE_DOD_STATE_DIR="$SD" ralph_under_test 2>/dev/null
  rm -f "$CLAUDE_RALPH_STATE_DIR/$S.md"
}

out=$(end_via '<promise>ALL DONE</promise>')
# Bound to its own name: the marker cases below reuse $out, and the two
# assertions further down are about THIS handover's output, not whatever ran
# last. (They silently were, which is how a passing suite hid it.)
HOUT="$out"
ck "promise handover still blocks after a Stop continuation" "block" "$(decision "$HOUT")"

# The marker protocol these cases used to exercise is GONE — it was a lease with
# no fencing token, and the review loop stopped with self-inflicted-churn after
# four rounds of ordering bugs in it. What survives is the requirement it was
# dressed around, which never needed a marker: once the loop's state file is
# gone, the next turn is gated normally again.
OSID="afterloop-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$OSID.arm"
printf -- '---\nactive: true\niteration: 2\nmax_iterations: 25\n---\np\n' \
  > "$CLAUDE_RALPH_STATE_DIR/$OSID.md"
out=$(gate_run "$OSID")
ck "stands down while the loop owns the session" "" "$(decision "$out")"
ck "  ...by exiting 0, not by crashing" "0" "$(gate_rc)"
rm -f "$CLAUDE_RALPH_STATE_DIR/$OSID.md"
out=$(gate_run "$OSID")
ck "the turn after the loop ends is still gated" "block" "$(decision "$out")"
ck "  ...and that is a real block, not a crash" "0" "$(gate_rc)"
rm -f "$SD/$OSID.arm" "$SD/$OSID.blocks"

# No marker file is created by anything, ever. This is the regression guard for
# the whole deleted mechanism: if a marker reappears, this fails.
# BOTH state directories. Searching only the ralph one left the dod state dir —
# where the gate itself would write — outside the regression guard, so a marker
# reappearing on the gate's side would have passed it.
ck "no .handover file exists anywhere" "none" \
   "$([ -z "$(find "$CLAUDE_RALPH_STATE_DIR" "$SD" -name '*.handover' -print -quit 2>/dev/null)" ] && echo none || echo FOUND)"

case "$HOUT" in *UNPUSHED_COMMITS*) r=named ;; *) r="$HOUT" ;; esac
ck "the gate's blockers reach the agent" "named" "$r"
case "$HOUT" in *"promise met"*) r=kept ;; *) r="no ralph message" ;; esac
ck "ralph's own message survives the merge" "kept" "$r"

# Own-line, or this asserts nothing: an inline tag no longer halts, so the
# loop would take the ordinary block path and the case would stay green
# even if halt handover stopped working altogether.
out=$(end_via 'stopping
<halt>user said stop</halt>')
ck "halt handover also runs the gate" "block" "$(decision "$out")"

# And when there is nothing to block on, the handover is quiet.
( cd "$HREPO" && git push -q origin master ) >/dev/null 2>&1
out=$(end_via '<promise>ALL DONE</promise>')
ck "clean repo ends the turn" "" "$(decision "$out")"
case "$out" in *systemMessage*) r=reported ;; *) r="$out" ;; esac
ck "and still reports why the loop ended" "reported" "$r"

echo
echo "== a hook that cannot finish in its slot has not passed =="
# The harness kills a Stop hook at its registered timeout. The gate's budget was
# spent PER ARMED REPO, so its own fail-closed TimeoutExpired branch sat ABOVE
# the point the kill lands. Measured 2026-08-29: two armed repos with a 45s
# dod-check ran 90.08s, and a SIGTERM at 60s left empty stdout, no gate.log line
# and no .blocks file. The budget is now total, and spending it is a BLOCKER —
# an unreached repository is an unmeasured one.
SLOWDOD="$SD/slow-dod.py"
printf 'import time, json\ntime.sleep(30)\nprint(json.dumps({"done": False, "applicable": True, "blockers": []}))\n' \
  > "$SLOWDOD"
TSID="budgetcap-$RANDOM"
# Two DISTINCT repositories. This used to list $BREPO twice, which stopped being
# two entries the moment the gate started reading its armfile as a set — the
# case then measured one repo, timed out on it, and never reached the
# NOT MEASURED path it exists to assert. The stub ignores the path it is handed,
# so the second one only has to be a different string.
TSID_SECOND="$SD/never-reached-repo"
printf '%s\n%s\n' "$BREPO" "$TSID_SECOND" > "$SD/$TSID.arm"
t0=$(date +%s)
out=$(gate_run "$TSID" CLAUDE_DOD_CHECK="$SLOWDOD" CLAUDE_DOD_TOTAL_BUDGET=5)
t1=$(date +%s)
ck "a gate out of budget BLOCKS instead of being killed" "block" "$(decision "$out")"
ck "  ...returning inside its own budget" "yes" \
   "$([ $((t1 - t0)) -le 25 ] && echo yes || echo no)"
case "$out" in *"NOT MEASURED"*) r=named ;; *) r="$out" ;; esac
ck "  ...and naming the repository it never reached" "named" "$r"
rm -f "$SD/$TSID.arm" "$SD/$TSID.blocks"

# The outer belt: ralph's inline call is bounded too, so the loop's final turn
# cannot be killed mid-measurement with its state file already deleted.
SLOWGATE_HOME="$SD/slowgate"
mkdir -p "$SLOWGATE_HOME/.claude/hooks" "$SLOWGATE_HOME/.claude/scripts"
printf 'import time\ntime.sleep(45)\n' > "$SLOWGATE_HOME/.claude/hooks/finishing-up-gate.py"
cp "$UNDER_TEST_ROOT/scripts/ralph-loop-state.sh" "$SLOWGATE_HOME/.claude/scripts/"
GSID="slowgate-$RANDOM"
printf -- '---\nactive: true\niteration: 4\nmax_iterations: 25\ncompletion_promise: "ALL DONE"\n---\np\n' \
  > "$CLAUDE_RALPH_STATE_DIR/$GSID.md"
t0=$(date +%s)
out=$(printf '{"session_id":"%s","transcript_path":"%s"}' "$GSID" "$MT" \
      | CLAUDE_DOD_STATE_DIR="$SD" HOME="$SLOWGATE_HOME" \
        CLAUDE_RALPH_GATE_TIMEOUT=5 bash "$RALPH_HOOK" 2>/dev/null)
t1=$(date +%s)
ck "a gate that cannot finish BLOCKS the handover turn" "block" "$(decision "$out")"
ck "  ...bounded, not waited on" "yes" \
   "$([ $((t1 - t0)) -le 20 ] && echo yes || echo no)"

echo
echo "== the loop ends loudly when its own state cannot be deleted =="
# `rm -f` is not safe under set -e: unlink needs write permission on the
# DIRECTORY, so a state dir at mode 555 returns EACCES and kills the hook before
# any JSON. Reproduced at HEAD and at master alike — rc=1, zero bytes of stdout,
# state file surviving — and the survivor then stands the standalone gate down
# on every later turn.
RSID="rmfail-$RANDOM"
RODIR="$SD/ro-ralph"; mkdir -p "$RODIR"
printf -- '---\nactive: true\niteration: 25\nmax_iterations: 25\n---\np\n' \
  > "$RODIR/$RSID.md"
printf '%s\n' "$BREPO" > "$SD/$RSID.arm"
chmod 555 "$RODIR"
out=$(printf '{"session_id":"%s","transcript_path":"%s"}' "$RSID" "$MT" \
      | CLAUDE_DOD_STATE_DIR="$SD" CLAUDE_RALPH_STATE_DIR="$RODIR" \
        ralph_under_test 2>/dev/null)
chmod 755 "$RODIR"
ck "an undeletable loop state file still BLOCKS" "block" "$(decision "$out")"
case "$out" in *"could not delete the loop state file"*) r=named ;; *) r="$out" ;; esac
ck "  ...and says so, naming the file" "named" "$r"
rm -f "$SD/$RSID.arm" "$SD/$RSID.blocks" "$RODIR/$RSID.md"

# The ownership skip under HANDOVER=1 was exercised by NOTHING: every handover
# case deletes the state file before calling the gate, so deleting
# `not HANDOVER and` from the gate left the whole suite green. It is load-bearing
# exactly when the unlink above fails.
HSID="handover-owned-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$HSID.arm"
printf -- '---\nactive: true\niteration: 2\nmax_iterations: 25\n---\np\n' \
  > "$CLAUDE_RALPH_STATE_DIR/$HSID.md"
out=$(gate_run "$HSID")
ck "the gate yields to a live loop" "" "$(decision "$out")"
out=$(gate_run "$HSID" CLAUDE_DOD_HANDOVER=1)
ck "  ...but the handover checks anyway, state file or not" "block" "$(decision "$out")"
rm -f "$CLAUDE_RALPH_STATE_DIR/$HSID.md" "$SD/$HSID.arm" "$SD/$HSID.blocks"

# The state resolver is sourced. Unguarded, a checkout that lost the file killed
# ralph before any handler, every turn, and the orphaned state file then stood
# the gate down forever.
NORES_HOME="$SD/nores"
mkdir -p "$NORES_HOME/.claude/hooks" "$NORES_HOME/.claude/scripts"
cp "$GATE" "$NORES_HOME/.claude/hooks/"
NSID="nores-$RANDOM"
printf -- '---\nactive: true\niteration: 25\nmax_iterations: 25\n---\np\n' \
  > "$CLAUDE_RALPH_STATE_DIR/$NSID.md"
printf '%s\n' "$BREPO" > "$SD/$NSID.arm"
out=$(printf '{"session_id":"%s","transcript_path":"%s"}' "$NSID" "$MT" \
      | CLAUDE_DOD_STATE_DIR="$SD" HOME="$NORES_HOME" bash "$RALPH_HOOK" 2>/dev/null)
ck "a missing state resolver does not kill the hook" "block" "$(decision "$out")"
ck "  ...and the loop still ended" "gone" \
   "$([ -e "$CLAUDE_RALPH_STATE_DIR/$NSID.md" ] && echo kept || echo gone)"
rm -f "$SD/$NSID.arm" "$SD/$NSID.blocks"

echo
echo "== a budget that outlives its slot is not a budget =="
# CLAUDE_DOD_TOTAL_BUDGET=90 is what an operator plausibly picks once a session
# arms two repos. Measured 2026-08-30: it ran 80.34s inside the 60s slot the
# hook is registered with, and the harness kill left empty stdout, no gate.log
# line and no countfile — the exact fail-open the budget was added to remove.
# Nothing warned, because the only budget message the hook could emit was the
# floor one. The ceiling is now READ from the registration rather than trusted
# to whoever set the variable.
CAPSID="ceiling-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$CAPSID.arm"
out=$(gate_run "$CAPSID" CLAUDE_DOD_TOTAL_BUDGET=900)
ck "an over-slot budget still blocks" "block" "$(decision "$out")"
ck "  ...and the clamp is in the log" "yes" \
   "$(grep -q 'outlives the' "$SD/gate.log" && echo yes || echo no)"
rm -f "$SD/$CAPSID.arm" "$SD/$CAPSID.blocks"

# And the ceiling is genuinely derived, not a second constant to keep in sync:
# register the hook with a smaller timeout and the budget follows it down.
SLOTSID="slot-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$SLOTSID.arm"
printf '%s' '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"python3 $HOME/.claude/hooks/finishing-up-gate.py","timeout":12}]}]}}' \
  > "$SD/settings-tiny.json"
out=$(gate_run "$SLOTSID" CLAUDE_DOD_SETTINGS="$SD/settings-tiny.json" CLAUDE_DOD_TOTAL_BUDGET=40)
ck "the ceiling follows the registered timeout" "yes" \
   "$(grep -q '12s slot' "$SD/gate.log" && echo yes || echo no)"
rm -f "$SD/$SLOTSID.arm" "$SD/$SLOTSID.blocks"

echo
echo "== the outer belt is validated, not trusted =="
# Unvalidated, CLAUDE_RALPH_GATE_TIMEOUT had two silent failures: a non-numeric
# value makes `timeout` exit 125, so EVERY handover blocked with the generic
# "gate FAILED to run" and the real verdict was never computed; and `timeout 0`
# means NO limit to GNU timeout, removing the belt while looking deliberate.
for bad_timeout in abc 0 999; do
  VSID="timeoutval-$RANDOM"
  printf -- '---\nactive: true\niteration: 25\nmax_iterations: 25\n---\np\n' \
    > "$CLAUDE_RALPH_STATE_DIR/$VSID.md"
  printf '%s\n' "$BREPO" > "$SD/$VSID.arm"
  out=$(printf '{"session_id":"%s","transcript_path":"%s"}' "$VSID" "$MT" \
        | CLAUDE_DOD_STATE_DIR="$SD" CLAUDE_RALPH_GATE_TIMEOUT="$bad_timeout" \
          ralph_under_test 2>"$SD/tv-$bad_timeout.err")
  ck "CLAUDE_RALPH_GATE_TIMEOUT=$bad_timeout still reaches a real verdict" \
     "block" "$(decision "$out")"
  case "$(cat "$SD/tv-$bad_timeout.err")" in
    *"using 52"*) r=named ;; *) r="$(cat "$SD/tv-$bad_timeout.err")" ;;
  esac
  ck "  ...and says what it used instead" "named" "$r"
  rm -f "$SD/$VSID.arm" "$SD/$VSID.blocks"
done

echo
echo "== a hook without jq must not die quietly =="
# jq is this hook's parser AND its output builder, called bare under set -e: off
# PATH, the hook died on the next line, silently, every turn. The loop state
# file then survives, and the standalone gate yields to it on this turn and
# every later one — so the whole gate is disabled for as long as it exists.
NOJQ="$SD/nojq-bin"; mkdir -p "$NOJQ"
for b in bash env python3 git sed awk grep cat date mktemp rm timeout head tr perl seq sleep; do
  src=$(command -v "$b" 2>/dev/null) && ln -sf "$src" "$NOJQ/$b"
done
JSID="nojq-$RANDOM"
printf -- '---\nactive: true\niteration: 4\nmax_iterations: 25\n---\np\n' \
  > "$CLAUDE_RALPH_STATE_DIR/$JSID.md"
out=$(printf '{"session_id":"%s","transcript_path":"%s"}' "$JSID" "$MT" \
      | PATH="$NOJQ" CLAUDE_DOD_STATE_DIR="$SD" ralph_under_test 2>/dev/null)
ck "a missing jq BLOCKS while loop state exists" "block" "$(decision "$out")"
rm -f "$CLAUDE_RALPH_STATE_DIR/$JSID.md"
out=$(printf '{"session_id":"%s","transcript_path":"%s"}' "$JSID" "$MT" \
      | PATH="$NOJQ" CLAUDE_DOD_STATE_DIR="$SD" ralph_under_test 2>/dev/null)
ck "  ...and is quiet when no loop is armed anywhere" "" "$(decision "$out")"

echo
echo "== the armfile is a SET of repositories, not a tally of edits =="
# Two things conspire here. finishing-up-arm.sh does check-then-append with no
# lock, and SUBAGENTS SHARE THE PARENT'S session_id — so two PostToolUse hooks
# run concurrently for one session, both miss the grep, and both append. The
# gate then spends its one budget measuring the same repository twice, which on
# a slow network is exactly how the last repo in the list goes unmeasured.
DUPSID="dupes-$RANDOM"
printf '%s\n%s\n%s\n' "$BREPO" "$BREPO" "$BREPO" > "$SD/$DUPSID.arm"
DUPCALLS="$SD/dup-calls.txt"; : > "$DUPCALLS"
DUPDOD="$SD/counting-dod.py"
cat > "$DUPDOD" <<PY
import sys, json
open("$DUPCALLS", "a").write(sys.argv[2] + "\n")
print(json.dumps({"done": True, "applicable": True, "blockers": []}))
PY
out=$(gate_run "$DUPSID" CLAUDE_DOD_CHECK="$DUPDOD")
ck "a repo listed three times is measured once" "1" "$(wc -l < "$DUPCALLS")"
ck "  ...and the turn still ends cleanly" "0" "$(gate_rc)"
rm -f "$SD/$DUPSID.arm" "$SD/$DUPSID.blocks"

# ...and the arm hook must not create the duplicates in the first place.
ARMHOOK="$HOME/.claude/hooks/finishing-up-arm.sh"
( cd "$ARMWORK" || exit 1
  git init -q -b master .; git config user.email t@t; git config user.name t
  echo hi > a.txt; git add -A; git commit -qm init
  git init -q --bare "$SD/armtest-origin.git"; git remote add origin "$SD/armtest-origin.git"
  git push -q -u origin master
  git symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/master ) >/dev/null 2>&1
# Firing twenty of them at once does NOT reproduce this — measured, they
# serialise on their own git and jq calls and the file comes out with one line
# either way. A test that green either way asserts nothing, so this drives the
# race directly: hold the armfile's lock, and assert the hook has not appended
# while somebody else holds it. That IS the mutual exclusion the concurrent case
# needs and cannot demonstrate.
ASID="armrace-$RANDOM"
: > "$SD/$ASID.arm"
flock "$SD/$ASID.arm" sleep 4 &
LOCKPID=$!
sleep 0.3
printf '{"session_id":"%s","tool_input":{"file_path":"%s/a.txt"}}' "$ASID" "$ARMWORK" \
  | CLAUDE_DOD_STATE_DIR="$SD" bash "$ARMHOOK" >/dev/null 2>&1 &
ARMPID=$!
sleep 2   # ~10x the hook's own jq+git cost, well inside the 4s the lock is held
ck "arming waits while another writer holds the armfile" "0" \
   "$(wc -l < "$SD/$ASID.arm")"
wait "$LOCKPID" "$ARMPID" 2>/dev/null
ck "  ...and records the repo once the lock is released" "1" \
   "$(wc -l < "$SD/$ASID.arm")"
rm -f "$SD/$ASID.arm"

echo
echo "== the budget does not starve the same repository every turn =="
# The budget is spent in armfile order, so a slow first repository starves the
# same tail on every turn of a session — and the repo that is never reached is
# the one reported NOT MEASURED, turn after turn, with no path to being read.
# The block counter already says how many turns this has happened for, so the
# start index rotates by it.
ROTSID="rotate-$RANDOM"
ROTSEEN="$SD/rot-seen.txt"; : > "$ROTSEEN"
ROTDOD="$SD/slow-record-dod.py"
cat > "$ROTDOD" <<PY
import sys, time, json
open("$ROTSEEN", "a").write(sys.argv[2] + "\n")
time.sleep(4)
print(json.dumps({"done": True, "applicable": True, "blockers": []}))
PY
printf '%s\n%s\n%s\n' "$SD/r1" "$SD/r2" "$SD/r3" > "$SD/$ROTSID.arm"
echo 1 > "$SD/$ROTSID.blocks"
out=$(gate_run "$ROTSID" CLAUDE_DOD_CHECK="$ROTDOD" CLAUDE_DOD_TOTAL_BUDGET=5)
ck "after one spent budget the next turn starts at the second repo" \
   "$SD/r2" "$(head -n1 "$ROTSEEN")"
ck "  ...and the unreached repos still BLOCK" "block" "$(decision "$out")"
rm -f "$SD/$ROTSID.arm" "$SD/$ROTSID.blocks"

echo
echo "== a loop is found however its directory is spelled =="
# `armed_in:` is compared to the caller's cwd by exact string equality, so a
# trailing slash or a symlinked spelling of the same directory misses — and a
# miss means compact-reinject and idle-handoff conclude no loop is armed here.
# ~/worktrees is exactly where symlinked spellings turn up.
# shellcheck source=/dev/null
. "$HOME/.claude/scripts/ralph-loop-state.sh"
WTREAL="$SD/wt-real"; mkdir -p "$WTREAL"
ln -sfn "$WTREAL" "$SD/wt-link"
LSID2="spelling-$RANDOM"
printf -- '---\nactive: true\niteration: 1\nmax_iterations: 25\narmed_in: "%s"\n---\np\n' \
  "$WTREAL" > "$CLAUDE_RALPH_STATE_DIR/$LSID2.md"
ck "the exact spelling finds it" "found" \
   "$(ralph_state_for_dir "$WTREAL" >/dev/null 2>&1 && echo found || echo missed)"
ck "a trailing slash finds it too" "found" \
   "$(ralph_state_for_dir "$WTREAL/" >/dev/null 2>&1 && echo found || echo missed)"
ck "and so does a symlinked spelling" "found" \
   "$(ralph_state_for_dir "$SD/wt-link" >/dev/null 2>&1 && echo found || echo missed)"
ck "a genuinely different directory still misses" "missed" \
   "$(ralph_state_for_dir "$SD/elsewhere" >/dev/null 2>&1 && echo found || echo missed)"
rm -f "$CLAUDE_RALPH_STATE_DIR/$LSID2.md"

echo
echo "== a stand-down field the payload got WRONG must not crash the gate =="
# `len(payload.get(k) or [])` inside the guards= comprehension raises TypeError
# on any truthy scalar, and that comprehension is an ARGUMENT to block() — so it
# fired BEFORE block() logged anything and before it printed a decision.
# Measured 2026-08-30: exit 1, EMPTY stdout, no decision line in gate.log, and
# the block-budget slot spent on a turn that guarded nothing. The two malformed
# shapes this suite already covered — a string and a dict — are the only bad
# shapes that HAVE a len(), which is why it stayed green.
for shape in 'true' '1' '3.5'; do
  SCSID="scalar-$RANDOM"
  printf '%s\n' "$BREPO" > "$SD/$SCSID.arm"
  out=$(printf '{"session_id":"%s","background_tasks":%s}' "$SCSID" "$shape" \
        | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE" 2>/dev/null); rc=$?
  ck "background_tasks=$shape still blocks"   "block" "$(decision "$out")"
  ck "  ...by exiting 0, not by crashing"     "0"     "$rc"
  rm -f "$SD/$SCSID.arm" "$SD/$SCSID.blocks"
done
SCSID="scalarcrons-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$SCSID.arm"
out=$(printf '{"session_id":"%s","session_crons":true}' "$SCSID" \
      | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE" 2>/dev/null)
ck "session_crons=true still blocks" "block" "$(decision "$out")"
# Keyed to session_crons SPECIFICALLY. A bare `not-a-list:bool` grep was already
# satisfied by the background_tasks case in the loop above — block() logs guards
# for BOTH keys every time — so the assertion could not fail.
ck "  ...and the shape it got is in the log" "yes" \
   "$(grep -q '"session_crons": "not-a-list:bool"' "$SD/gate.log" && echo yes || echo no)"
rm -f "$SD/$SCSID.arm" "$SD/$SCSID.blocks"

echo
echo "== a value that clamps to nothing is refused, not used =="
# NaN parses as a float and then defeats every comparison: `nan < floor` is
# False, `nan > ceiling` is False. MEASURED 2026-08-30 — ARM_MAX_AGE_DAYS=nan
# made `st_mtime >= nan` False for EVERY file, and ONE sweep unlinked six other
# sessions' .arm and .blocks. Those sessions are never told: an unarmed
# session's Stop hook is the one turn-ending path that deliberately writes no
# log line, so their gates were disarmed in silence.
NANSID="nanage-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$NANSID.arm"
printf '%s\n' "$BREPO" > "$SD/victim-alpha.arm"
: > "$SD/victim-alpha.blocks"
out=$(gate_run "$NANSID" CLAUDE_DOD_ARM_MAX_AGE_DAYS=nan)
ck "a NaN reap age does not eat a fresh armfile" "kept" \
   "$([ -e "$SD/victim-alpha.arm" ] && echo kept || echo gone)"
ck "  ...nor its countfile"                      "kept" \
   "$([ -e "$SD/victim-alpha.blocks" ] && echo kept || echo gone)"
ck "  ...and the gate still blocks"              "block" "$(decision "$out")"
ck "  ...and says which value it refused"        "yes" \
   "$(grep -q 'is not a finite number' "$SD/gate.log" && echo yes || echo no)"
rm -f "$SD/victim-alpha.arm" "$SD/victim-alpha.blocks" "$SD/$NANSID.arm" "$SD/$NANSID.blocks"

NBSID="nanbudget-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$NBSID.arm"
out=$(gate_run "$NBSID" CLAUDE_DOD_TOTAL_BUDGET=nan)
ck "a NaN budget still blocks" "block" "$(decision "$out")"
ck "  ...having fallen back to a real number" "yes" \
   "$(grep -q "CLAUDE_DOD_TOTAL_BUDGET='nan' is not a finite number" "$SD/gate.log" && echo yes || echo no)"
rm -f "$SD/$NBSID.arm" "$SD/$NBSID.blocks"

echo
echo "== an armfile that lists nothing is not a clean pass =="
# Under a write-size limit the arm hook leaves a 0-BYTE armfile behind. The gate
# read it as zero repositories, took the clean-pass branch, deleted the file and
# ended the turn with no output and no log line — armed, and measuring nothing.
# The UNREADABLE armfile already blocks for exactly this reason.
EMPSID="emptyarm-$RANDOM"
: > "$SD/$EMPSID.arm"
out=$(gate_run "$EMPSID")
ck "an empty armfile BLOCKS" "block" "$(decision "$out")"
case "$out" in *EMPTY*) r=named ;; *) r="$out" ;; esac
ck "  ...and says the record was empty" "named" "$r"
rm -f "$SD/$EMPSID.arm" "$SD/$EMPSID.blocks"

echo
echo "== a repository the gate skips is never skipped SILENTLY =="
# `not result.get("applicable")` read a MISSING key as n/a, so a dod-check
# emitting `{}` was skipped with no log line, the armfile unlinked and the turn
# allowed. The same silence hid a missing `git`, which answers n/a for every
# repository on the machine — measured end to end, the only variable being
# whether git was on PATH.
EMPTYJSON="$SD/empty-json-dod.py"
printf 'print("{}")\n' > "$EMPTYJSON"
BJSID="emptyjson-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$BJSID.arm"
out=$(gate_run "$BJSID" CLAUDE_DOD_CHECK="$EMPTYJSON")
ck "a verdict with no applicable field BLOCKS" "block" "$(decision "$out")"
rm -f "$SD/$BJSID.arm" "$SD/$BJSID.blocks"

# ...while a GENUINE n/a still ends the turn — but leaves a record that it did.
NADOD="$SD/na-dod.py"
printf 'import json\nprint(json.dumps({"applicable": False, "done": False, "blockers": [], "reason": "not a git repository"}))\n' \
  > "$NADOD"
NASID="genuinena-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$NASID.arm"
out=$(gate_run "$NASID" CLAUDE_DOD_CHECK="$NADOD")
ck "a genuine n/a still allows"        ""    "$(decision "$out")"
ck "  ...by exiting 0, not by crashing" "0"  "$(gate_rc)"
ck "  ...but is recorded, not silent"  "yes" \
   "$(grep -q 'does not apply here' "$SD/gate.log" && echo yes || echo no)"
rm -f "$SD/$NASID.arm" "$SD/$NASID.blocks"

echo
echo "== what could not be established reaches the AGENT, not just the log =="
# dod-check demotes several FAILED measurements to warnings and still reports
# done. Blocking on them would hang every offline session — but until now the
# fact reached only gate.log, which no agent reads, so "no unresolved threads"
# and "could not find out" ended the turn identically. A token missing scopes
# makes that failure PERMANENT, not transient.
WARNDOD="$SD/warn-dod.py"
printf 'import json\nprint(json.dumps({"applicable": True, "done": True, "blockers": [], "warnings": ["unresolved review threads not counted: HTTP 403"]}))\n' \
  > "$WARNDOD"
WARNSID="warned-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$WARNSID.arm"
out=$(gate_run "$WARNSID" CLAUDE_DOD_CHECK="$WARNDOD")
ck "a warned-but-done turn is not blocked" "" "$(decision "$out")"
case "$out" in *"could not establish"*) r=told ;; *) r="$out" ;; esac
ck "  ...and the agent is told what was not measured" "told" "$r"
case "$out" in *"HTTP 403"*) r=verbatim ;; *) r="$out" ;; esac
ck "  ...naming it, not just that there was one" "verbatim" "$r"
rm -f "$SD/$WARNSID.arm" "$SD/$WARNSID.blocks"

echo
echo "== the arm hook cannot switch the whole gate off quietly =="
# No armfile means the gate has nothing to check and ends the turn with no
# output and no gate.log line at all — that is enforcement OFF, not a hook that
# failed. So every path that fails to arm exits NON-ZERO, which is what surfaces
# a PostToolUse hook's stderr, and says what it could not do. Never 2: that code
# blocks the tool call itself, and failing to arm must not stop an edit landing.
BADSTATE="$SD/state-is-a-file"
: > "$BADSTATE"
printf '{"session_id":"armfail-1","tool_input":{"file_path":"%s/a.txt"}}' "$ARMWORK" \
  | CLAUDE_DOD_STATE_DIR="$BADSTATE" bash "$ARMHOOK" >/dev/null 2>"$SD/armfail.err"; rc=$?
ck "an unusable state dir exits non-zero" "yes" "$([ "$rc" -ne 0 ] && echo yes || echo no)"
ck "  ...but never 2, which would block the edit" "yes" "$([ "$rc" -ne 2 ] && echo yes || echo no)"
ck "  ...and says the gate will not run" "yes" \
   "$(grep -q 'definition-of-done gate' "$SD/armfail.err" && echo yes || echo no)"

printf '{"session_id":"armfail-2","tool_input":{"file_path":"%s/a.txt"}}' "$ARMWORK" \
  | PATH="$NOJQ" CLAUDE_DOD_STATE_DIR="$SD" bash "$ARMHOOK" >/dev/null 2>"$SD/armjq.err"; rc=$?
ck "a missing jq exits non-zero too" "yes" "$([ "$rc" -ne 0 ] && echo yes || echo no)"
ck "  ...and never 2"                "yes" "$([ "$rc" -ne 2 ] && echo yes || echo no)"
ck "  ...and still names the consequence" "yes" \
   "$(grep -q 'definition-of-done gate' "$SD/armjq.err" && echo yes || echo no)"

echo
echo "== a clean pass must not DISARM the session =="
# The clean branch used to unlink the armfile, and that is how one gate instance
# disarmed a session its sibling was still blocking. Once the armfile is gone the
# gate returns at `if not armfile.exists(): allow()` — before any logging exists
# — so every later turn ends allowed, silent and unrecorded. Nothing in this
# suite asserted the deletion, which is why it survived seven rounds.
CLEANDOD="$SD/clean-dod.py"
printf 'import json\nprint(json.dumps({"applicable": True, "done": True, "blockers": []}))\n' \
  > "$CLEANDOD"
CLEANSID="cleanarm-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$CLEANSID.arm"
echo 3 > "$SD/$CLEANSID.blocks"
out=$(gate_run "$CLEANSID" CLAUDE_DOD_CHECK="$CLEANDOD")
ck "a clean pass ends the turn"              ""      "$(decision "$out")"
ck "  ...by exiting 0, not by crashing"      "0"     "$(gate_rc)"
ck "  ...and LEAVES the armfile in place"    "kept"  \
   "$([ -e "$SD/$CLEANSID.arm" ] && echo kept || echo gone)"
ck "  ...while resetting the block budget"   "gone"  \
   "$([ -e "$SD/$CLEANSID.blocks" ] && echo kept || echo gone)"
rm -f "$SD/$CLEANSID.arm"

# The race itself, driven directly. MEASURED 2026-08-30 with both instances
# running the REAL predicate against one healthy repo and a flaky transport
# between them: 20 of 40 concurrent pairs reached different verdicts, and EVERY
# divergence disarmed the session. Here the divergence is forced rather than
# raced, because a test that only sometimes reproduces is a test that cannot
# fail on purpose.
BLOCKDOD="$SD/blocking-dod.py"
printf 'import json, time\ntime.sleep(1)\nprint(json.dumps({"applicable": True, "done": False, "blockers": [{"code": "STILL_UNFINISHED", "detail": "work is not done"}]}))\n' \
  > "$BLOCKDOD"
RACESID="race-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$RACESID.arm"
( printf '{"session_id":"%s"}' "$RACESID" \
  | env CLAUDE_DOD_STATE_DIR="$SD" CLAUDE_DOD_CHECK="$BLOCKDOD" \
        CLAUDE_DOD_HANDOVER=1 python3 "$GATE" > "$SD/race-handover.out" 2>/dev/null ) &
( printf '{"session_id":"%s"}' "$RACESID" \
  | env CLAUDE_DOD_STATE_DIR="$SD" CLAUDE_DOD_CHECK="$CLEANDOD" \
        python3 "$GATE" > "$SD/race-standalone.out" 2>/dev/null ) &
wait
ck "a clean sibling does not disarm a blocking handover" "kept" \
   "$([ -e "$SD/$RACESID.arm" ] && echo kept || echo gone)"
ck "  ...and the handover's block still reaches the agent" "block" \
   "$(decision "$(cat "$SD/race-handover.out" 2>/dev/null)")"
rm -f "$SD/$RACESID.arm" "$SD/$RACESID.blocks"

echo
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
