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
ck "  ...by exiting 0, not by crashing" "0" "$GATE_RC"
printf '{"session_id":"%s","transcript_path":"%s"}' "$RSID" "$RT" \
  | CLAUDE_DOD_STATE_DIR="$SD" bash "$RALPH_HOOK" >/dev/null 2>&1
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
  | CLAUDE_DOD_STATE_DIR="$SD" bash "$RALPH_HOOK" >/dev/null 2>&1
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
cp "$HOME/.claude/scripts/ralph-loop-state.sh" "$MISS_HOME/.claude/scripts/"
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
cp "$HOME/.claude/scripts/ralph-loop-state.sh" "$CRASH_HOME/.claude/scripts/"
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
shape_dec() { printf '%s' "$1" \
  | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE" 2>/dev/null \
  | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("decision") or "")
except Exception: print("")'; }
ck "a dict session_crons does NOT stand it down"   "block" \
   "$(shape_dec "$(printf '{"session_id":"%s","session_crons":{"oops":1}}' "$SHSID")")"
ck "a string background_tasks does NOT either"     "block" \
   "$(shape_dec "$(printf '{"session_id":"%s","background_tasks":"x"}' "$SHSID")")"
ck "an empty list does NOT stand it down"          "block" \
   "$(shape_dec "$(printf '{"session_id":"%s","session_crons":[]}' "$SHSID")")"
ck "a real non-empty list DOES"                    "" \
   "$(shape_dec "$(printf '{"session_id":"%s","background_tasks":[{"id":"t"}]}' "$SHSID")")"
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
ck "  ...by exiting 0, not by crashing" "0" "$GATE_RC"
rm -f "$CLAUDE_RALPH_STATE_DIR/$OSID.md"
out=$(gate_run "$OSID")
ck "the turn after the loop ends is still gated" "block" "$(decision "$out")"
ck "  ...and that is a real block, not a crash" "0" "$GATE_RC"
rm -f "$SD/$OSID.arm" "$SD/$OSID.blocks"

# No marker file is created by anything, ever. This is the regression guard for
# the whole deleted mechanism: if a marker reappears, this fails.
ck "no .handover file exists anywhere" "none" \
   "$([ -z "$(find "$CLAUDE_RALPH_STATE_DIR" -name '*.handover' -print -quit 2>/dev/null)" ] && echo none || echo FOUND)"

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
printf '%s\n%s\n' "$BREPO" "$BREPO" > "$SD/$TSID.arm"
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
printf 'import time\ntime.sleep(30)\n' > "$SLOWGATE_HOME/.claude/hooks/finishing-up-gate.py"
cp "$HOME/.claude/scripts/ralph-loop-state.sh" "$SLOWGATE_HOME/.claude/scripts/"
GSID="slowgate-$RANDOM"
printf -- '---\nactive: true\niteration: 4\nmax_iterations: 25\ncompletion_promise: "ALL DONE"\n---\np\n' \
  > "$CLAUDE_RALPH_STATE_DIR/$GSID.md"
t0=$(date +%s)
out=$(printf '{"session_id":"%s","transcript_path":"%s"}' "$GSID" "$MT" \
      | CLAUDE_DOD_STATE_DIR="$SD" HOME="$SLOWGATE_HOME" \
        CLAUDE_RALPH_GATE_TIMEOUT=2 bash "$RALPH_HOOK" 2>/dev/null)
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
        bash "$RALPH_HOOK" 2>/dev/null)
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
cp "$HOME/.claude/hooks/finishing-up-gate.py" "$NORES_HOME/.claude/hooks/"
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
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
