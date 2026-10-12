#!/usr/bin/env bash
# Ralph loop exits: what ends a loop, what must not, and how it ends when its
# own environment is broken. Complements tests/ralph-loop-stop.test.sh (core
# loop mechanics); these cases moved here from the removed DoD stop-hook suite.
#
# Origin: 2026-08-27 (session ebcdd93d). A user said "write the handoff and then
# stop". The mission was incomplete, so the loop's only exits — the completion
# sigil, max_iterations, /cancel-ralph — were all unavailable, and the agent's
# real choices were to LIE or to DISOBEY. <halt>reason</halt> is the honest
# exit; own-line anchoring keeps quoting a tag from counting as claiming it.
#
# Tests the CHECKOUT this file lives in. CLAUDE_RALPH_STATE_DIR keeps all of
# this out of the live loop directory.
set -uo pipefail

SD=$(mktemp -d)
trap 'rm -rf "$SD"' EXIT  # arftl-allow: destructive-shell — this run's own mktemp -d
export CLAUDE_RALPH_STATE_DIR="$SD/ralph"
mkdir -p "$CLAUDE_RALPH_STATE_DIR"
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
RALPH_HOOK="$ROOT/hooks/ralph-loop-stop.sh"
# The hook sources $HOME/.claude/scripts/ralph-loop-state.sh; point HOME at a
# fake one holding THIS checkout's resolver.
RALPH_TEST_HOME="$SD/ralph-home"
mkdir -p "$RALPH_TEST_HOME/.claude/scripts"
ln -s "$ROOT/scripts/ralph-loop-state.sh" "$RALPH_TEST_HOME/.claude/scripts/ralph-loop-state.sh"

pass=0; fail=0
ck() {
  if [ "$2" = "$3" ]; then printf '  PASS  %s\n' "$1"; pass=$((pass+1))
  else printf '  FAIL  %s\n     want: %s\n     got : %s\n' "$1" "$2" "$3"; fail=$((fail+1)); fi
}
decision() { printf '%s' "$1" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("decision") or "")
except Exception: print("")' 2>/dev/null; }
ralph_under_test() {
  HOME="$RALPH_TEST_HOME" bash "$RALPH_HOOK"
}
# A transcript whose last assistant text is a genuine completion claim.
MT="$SD/promise.jsonl"
python3 - "$MT" <<'PY'
import json, sys
rec = {"role": "assistant", "message": {"content": [
    {"type": "text", "text": "<promise>ALL DONE</promise>"}]}}
open(sys.argv[1], "w").write(json.dumps(rec, separators=(",", ":")) + "\n")
PY

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
  printf '{"session_id":"%s","transcript_path":"%s"}' "$S" "$T" \
    | ralph_under_test >/dev/null 2>&1
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
echo "== a loop whose own environment is broken still ends =="
# A frontmatter line missing entirely used to kill the hook under
# `set -euo pipefail` (grep exits 1) BEFORE its own corruption handler ran, so
# the state file survived and the loop could never end.
CSID="corrupt-$RANDOM"
printf -- '---\nactive: true\nmax_iterations: 25\n---\np\n' \
  > "$CLAUDE_RALPH_STATE_DIR/$CSID.md"
printf '{"session_id":"%s","transcript_path":"%s"}' "$CSID" "$MT" \
  | ralph_under_test >/dev/null 2>&1
ck "a missing frontmatter line ends the loop" "gone" \
   "$([ -e "$CLAUDE_RALPH_STATE_DIR/$CSID.md" ] && echo kept || echo gone)"

# `rm -f` is not safe under set -e: unlink needs write permission on the
# DIRECTORY, so a state dir at mode 555 returns EACCES and used to kill the hook
# before any JSON, leaving the state file behind with no word said.
RSID="rmfail-$RANDOM"
RODIR="$SD/ro-ralph"; mkdir -p "$RODIR"
printf -- '---\nactive: true\niteration: 25\nmax_iterations: 25\n---\np\n' \
  > "$RODIR/$RSID.md"
chmod 555 "$RODIR"
out=$(printf '{"session_id":"%s","transcript_path":"%s"}' "$RSID" "$MT" \
      | CLAUDE_RALPH_STATE_DIR="$RODIR" ralph_under_test 2>/dev/null)
chmod 755 "$RODIR"
case "$out" in *"could not delete the loop state file"*"$RSID.md"*) r=named ;; *) r="$out" ;; esac
ck "an undeletable loop state file is named, not silent" "named" "$r"
rm -f "$RODIR/$RSID.md"

# The state resolver is sourced. Unguarded, a checkout that lost the file killed
# ralph before any handler, every turn, and the loop could never end.
NORES_HOME="$SD/nores"
mkdir -p "$NORES_HOME/.claude/scripts"
NSID="nores-$RANDOM"
printf -- '---\nactive: true\niteration: 25\nmax_iterations: 25\n---\np\n' \
  > "$CLAUDE_RALPH_STATE_DIR/$NSID.md"
out=$(printf '{"session_id":"%s","transcript_path":"%s"}' "$NSID" "$MT" \
      | HOME="$NORES_HOME" bash "$RALPH_HOOK" 2>/dev/null)
case "$out" in *"max iterations"*) r=ended ;; *) r="$out" ;; esac
ck "a missing state resolver does not kill the hook" "ended" "$r"
ck "  ...and the loop state is gone" "gone" \
   "$([ -e "$CLAUDE_RALPH_STATE_DIR/$NSID.md" ] && echo kept || echo gone)"

# jq is this hook's parser AND its output builder. Off PATH, the hook used to
# die on the next line, silently, every turn.
NOJQ="$SD/nojq-bin"; mkdir -p "$NOJQ"
for b in bash env python3 sed awk grep cat date mktemp rm head tr perl seq sleep; do
  src=$(command -v "$b" 2>/dev/null) && ln -sf "$src" "$NOJQ/$b"
done
JSID="nojq-$RANDOM"
printf -- '---\nactive: true\niteration: 4\nmax_iterations: 25\n---\np\n' \
  > "$CLAUDE_RALPH_STATE_DIR/$JSID.md"
out=$(printf '{"session_id":"%s","transcript_path":"%s"}' "$JSID" "$MT" \
      | PATH="$NOJQ" ralph_under_test 2>/dev/null)
ck "a missing jq BLOCKS while loop state exists" "block" "$(decision "$out")"
rm -f "$CLAUDE_RALPH_STATE_DIR/$JSID.md"
out=$(printf '{"session_id":"%s","transcript_path":"%s"}' "$JSID" "$MT" \
      | PATH="$NOJQ" ralph_under_test 2>/dev/null)
ck "  ...and is quiet when no loop is armed anywhere" "" "$(decision "$out")"

echo
echo "== a loop is found however its directory is spelled =="
# `armed_in:` was compared to the caller's cwd by exact string equality, so a
# trailing slash or a symlinked spelling of the same directory missed — and a
# miss means compact-reinject and idle-handoff conclude no loop is armed here.
# shellcheck source=/dev/null
. "$ROOT/scripts/ralph-loop-state.sh"
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
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
