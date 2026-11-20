#!/usr/bin/env bash
# PASS when the sweep's inventory lists the pending id decision as still to
# do (not under "Leaving soon", where only the legacy-sync deletion belongs)
# and the Domain model lens reports the room/lamp ids instead of SKIPping
# them as already decided. grade.py has the details.
# Each file is read from the notes dir, or, when the agent could not copy it
# there (the sandbox blocks some writes into HOME), rebuilt from its Write
# and Edit calls in the transcript. Python runs isolated (-I): HOME here is
# the run's, and its user site-packages are writable.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
here="$(dirname "$0")"
[ -f "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
dir="$RUN/home/.local/state/claude-tasks/work/assurance-sweep"
out="$RUN/graded"; mkdir -p "$out"
pick() { # $1 file-name substring, $2 output path
  local f; f=$(ls "$dir"/*/*"$1"* 2>/dev/null | head -n1)
  if [ -n "$f" ]; then cp "$f" "$2" || { echo "INCONCLUSIVE: copy failed"; exit 2; }
  else python3 -I "$here/file_from_transcript.py" "$RUN/transcript.jsonl" "$1" "$2" assurance-sweep/; fi
}
pick inventory "$out/inventory.md" || fail "no inventory written"
pick domain "$out/lens3.md" || fail "no Domain model lens file written"
res=$(python3 -I "$here/grade.py" "$out/inventory.md" "$out/lens3.md"); code=$?
case $code in 0) pass "$res" ;; 1) fail "$res" ;; esac
echo "INCONCLUSIVE: grader exit $code: $res"; exit 2
