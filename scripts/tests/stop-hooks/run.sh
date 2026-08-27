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
# CLAUDE_RALPH_STATE_DIR keeps all of this out of the live loop directory.
set -uo pipefail

SD=$(mktemp -d)
trap 'rm -rf "$SD"' EXIT  # arftl-allow: destructive-shell — $SD is this run's own mktemp -d
export CLAUDE_RALPH_STATE_DIR="$SD/ralph"
mkdir -p "$CLAUDE_RALPH_STATE_DIR"
GATE="$HOME/.claude/hooks/finishing-up-gate.py"
RALPH_HOOK="$HOME/.claude/hooks/ralph-loop-stop.sh"
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

# `decision` alone CANNOT distinguish "allowed the turn" from "crashed": both
# render as empty. Every allow-assertion in this file was therefore satisfied by
# the guarded path throwing — the suite would have stayed green over a hook that
# never ran. `gate_run` captures the exit code too, so an allow must be a
# deliberate exit 0 and not a traceback.
GATE_RC=0
gate_run() {  # $1 = session id; extra args are env assignments -> prints stdout
  local sid="$1"; shift
  local out
  out=$(printf '{"session_id":"%s"}' "$sid" \
        | env CLAUDE_DOD_STATE_DIR="$SD" "$@" python3 "$GATE" 2>/dev/null)
  GATE_RC=$?
  printf '%s' "$out"
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
ck "  ...by exiting 0, not by crashing" "0" "$GATE_RC"
rm -f "$CLAUDE_RALPH_STATE_DIR/$SID.md"

# Proof the assertion above can actually fail: a hook that throws produces the
# same empty stdout an allow does. Without the exit code this suite was blind.
BROKEN="$SD/broken-gate.py"
printf 'import sys\nraise SystemExit(1)\n' > "$BROKEN"
printf '{"session_id":"x"}' | python3 "$BROKEN" >/dev/null 2>&1; rc=$?
ck "  ...and a crash is distinguishable" "1" "$rc"

out=$(printf '{"session_id":"%s"}' "$SID" | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE")
ck "blocks again once the loop is gone" "block" "$(decision "$out")"

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
    | CLAUDE_DOD_STATE_DIR="$SD" bash "$RALPH_HOOK" >/dev/null 2>&1
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
ck "a QUOTED halt does not exit"        kept \
   "$(probe 'I could emit <halt>give up</halt> here but I will not.')"
# Discussing the rule and THEN genuinely claiming it must still work.
ck "discuss-then-claim still exits"     gone \
   "$(probe 'The rule says emit <promise>ALL DONE</promise> only when true. It is.
<promise>ALL DONE</promise>')"

if [ -s "$CLAUDE_RALPH_STATE_DIR/halts.log" ]; then
  ck "the halt reason is recorded" "yes" \
     "$(grep -qF 'the user told me to stop' "$CLAUDE_RALPH_STATE_DIR/halts.log" && echo yes || echo no)"
else
  ck "the halt reason is recorded" "yes" "no halts.log"
fi

echo
echo "== a REAL loop end leaves no marker behind =="
# The orphan, measured against a real end_loop rather than a planted marker.
# The first attempt at this fix consumed the marker gate-side, which does not
# work on the actual timing: the standalone gate finishes in milliseconds while
# ralph polls the transcript for up to ~3s, so the gate stands down BEFORE the
# marker exists and has nothing to consume. Bounding the marker's life to
# end_loop is what closes it.
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
printf '{"session_id":"%s","transcript_path":"%s"}' "$RSID" "$RT" \
  | CLAUDE_DOD_STATE_DIR="$SD" bash "$RALPH_HOOK" >/dev/null 2>&1
ck "end_loop leaves no marker"          "gone" \
   "$([ -e "$CLAUDE_RALPH_STATE_DIR/$RSID.handover" ] && echo kept || echo gone)"
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
  | CLAUDE_DOD_STATE_DIR="$SD" bash "$RALPH_HOOK" >/dev/null 2>&1
ck "a missing frontmatter line ends the loop" "gone" \
   "$([ -e "$CLAUDE_RALPH_STATE_DIR/$CSID.md" ] && echo kept || echo gone)"

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
ck "gate.log was appended, not rewritten" "yes" \
   "$([ "$(wc -c < "$SD/gate.log")" -ge "$LOGSIZE_BEFORE" ] && echo yes || echo no)"

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
  printf '{"session_id":"%s","transcript_path":"%s"}' "$S" "$T" \
    | CLAUDE_DOD_STATE_DIR="$SD" bash "$RALPH_HOOK" 2>/dev/null
  rm -f "$CLAUDE_RALPH_STATE_DIR/$S.md"
}

out=$(end_via '<promise>ALL DONE</promise>')
# Bound to its own name: the marker cases below reuse $out, and the two
# assertions further down are about THIS handover's output, not whatever ran
# last. (They silently were, which is how a passing suite hid it.)
HOUT="$out"
ck "promise handover blocks on the gate's verdict" "block" "$(decision "$HOUT")"

# The race the marker closes. Stop hooks run concurrently, so on the turn a loop
# ends, the standalone gate is running alongside ralph's own call. Simulate the
# losing interleaving directly: state file already gone, marker present. Without
# the marker this invocation would evaluate and the turn would carry two
# identical blocks.
MSID="marker-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$MSID.arm"
: > "$CLAUDE_RALPH_STATE_DIR/$MSID.handover"
out=$(gate_run "$MSID")
ck "a fresh handover marker silences the standalone gate" "" "$(decision "$out")"
ck "  ...by exiting 0, not by crashing" "0" "$GATE_RC"

# The orphaned-marker hole, found independently by two reviewers and
# reproduced. ralph writes the marker only inside end_loop, AFTER a transcript
# poll that can take ~3s — so the standalone gate almost always reads the state
# file first and stands down. If it leaves the marker behind, the NEXT Stop
# inside the staleness window eats it and allows a turn with unfinished work.
# A live loop makes any marker meaningless, so standing down must consume it.
OSID="orphan-$RANDOM"
printf '%s\n' "$BREPO" > "$SD/$OSID.arm"
printf -- '---\nactive: true\niteration: 2\nmax_iterations: 25\n---\np\n' \
  > "$CLAUDE_RALPH_STATE_DIR/$OSID.md"
: > "$CLAUDE_RALPH_STATE_DIR/$OSID.handover"
out=$(gate_run "$OSID")
ck "  ...standing down exits 0" "0" "$GATE_RC"
ck "standing down for a live loop consumes the marker" "gone" \
   "$([ -e "$CLAUDE_RALPH_STATE_DIR/$OSID.handover" ] && echo kept || echo gone)"
rm -f "$CLAUDE_RALPH_STATE_DIR/$OSID.md"
out=$(printf '{"session_id":"%s"}' "$OSID" | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE")
ck "the turn after the loop ends is still gated" "block" "$(decision "$out")"
rm -f "$SD/$OSID.arm" "$SD/$OSID.blocks"
ck "the marker is consumed on read" "gone" \
   "$([ -e "$CLAUDE_RALPH_STATE_DIR/$MSID.handover" ] && echo kept || echo gone)"
# Consumed means the NEXT turn is gated normally again.
out=$(printf '{"session_id":"%s"}' "$MSID" | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE")
ck "the turn after the handover is gated again" "block" "$(decision "$out")"
# A marker left behind by a gate that never ran must not gag the session forever.
touch -d "@$(( $(date +%s) - 3600 ))" "$CLAUDE_RALPH_STATE_DIR/$MSID.handover"
out=$(printf '{"session_id":"%s"}' "$MSID" | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE")
ck "a stale marker is ignored, not obeyed" "block" "$(decision "$out")"
# And ralph's own call must NOT stand down on the marker it just wrote.
: > "$CLAUDE_RALPH_STATE_DIR/$MSID.handover"
out=$(printf '{"session_id":"%s"}' "$MSID" \
      | CLAUDE_DOD_STATE_DIR="$SD" CLAUDE_DOD_HANDOVER=1 python3 "$GATE")
ck "HANDOVER=1 bypasses the marker" "block" "$(decision "$out")"
rm -f "$SD/$MSID.arm" "$SD/$MSID.blocks" "$CLAUDE_RALPH_STATE_DIR/$MSID.handover"
case "$HOUT" in *UNPUSHED_COMMITS*) r=named ;; *) r="$HOUT" ;; esac
ck "the gate's blockers reach the agent" "named" "$r"
case "$HOUT" in *"promise met"*) r=kept ;; *) r="no ralph message" ;; esac
ck "ralph's own message survives the merge" "kept" "$r"

out=$(end_via 'stopping <halt>user said stop</halt>')
ck "halt handover also runs the gate" "block" "$(decision "$out")"

# And when there is nothing to block on, the handover is quiet.
( cd "$HREPO" && git push -q origin master ) >/dev/null 2>&1
out=$(end_via '<promise>ALL DONE</promise>')
ck "clean repo ends the turn" "" "$(decision "$out")"
case "$out" in *systemMessage*) r=reported ;; *) r="$out" ;; esac
ck "and still reports why the loop ended" "reported" "$r"

echo
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
