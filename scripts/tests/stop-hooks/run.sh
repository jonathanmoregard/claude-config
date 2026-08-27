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
decision() { printf '%s' "$1" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("decision"))
except Exception: print("")' 2>/dev/null; }

echo "== dod gate: one driver per turn =="
printf '%s\n' "$HOME/.claude" > "$SD/$SID.arm"

out=$(printf '{"session_id":"%s"}' "$SID" | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE")
ck "blocks by default (active is the default mode)" "block" "$(decision "$out")"

printf -- '---\nactive: true\niteration: 3\nmax_iterations: 25\n---\nprompt\n' \
  > "$CLAUDE_RALPH_STATE_DIR/$SID.md"
out=$(printf '{"session_id":"%s"}' "$SID" | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE")
ck "yields to an armed ralph loop" "" "$out"
rm -f "$CLAUDE_RALPH_STATE_DIR/$SID.md"

out=$(printf '{"session_id":"%s"}' "$SID" | CLAUDE_DOD_STATE_DIR="$SD" python3 "$GATE")
ck "blocks again once the loop is gone" "block" "$(decision "$out")"

out=$(printf '{"session_id":"%s"}' "$SID" | CLAUDE_DOD_STATE_DIR="$SD" CLAUDE_DOD_MODE=shadow python3 "$GATE")
case "$out" in *'"systemMessage"'*) r=shadow ;; *) r="$out" ;; esac
ck "shadow is still available as an opt-out" "shadow" "$r"

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
  printf '{"session_id":"%s","transcript_path":"%s"}' "$S" "$T" \
    | bash "$RALPH_HOOK" >/dev/null 2>&1
  if [ -f "$CLAUDE_RALPH_STATE_DIR/$S.md" ]; then rm -f "$CLAUDE_RALPH_STATE_DIR/$S.md"; echo kept
  else echo gone; fi
}

ck "<halt> with a reason ends the loop" gone \
   "$(probe 'Stopping. <halt>the user told me to stop and the mission is incomplete</halt>')"
# A bare tag is not an escape hatch: an exit has to say why, or the audit trail
# is worthless and "halt" becomes a cheaper sigil than the sigil.
ck "bare <halt> does NOT exit"          kept "$(probe 'Stopping. <halt></halt>')"
ck "no tag keeps looping"               kept "$(probe 'Holding.')"
ck "a true promise still exits"         gone "$(probe '<promise>ALL DONE</promise>')"
ck "a false promise does not exit"      kept "$(probe '<promise>something else</promise>')"

if [ -s "$CLAUDE_RALPH_STATE_DIR/halts.log" ]; then
  ck "the halt reason is recorded" "yes" \
     "$(grep -qF 'the user told me to stop' "$CLAUDE_RALPH_STATE_DIR/halts.log" && echo yes || echo no)"
else
  ck "the halt reason is recorded" "yes" "no halts.log"
fi

echo
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
