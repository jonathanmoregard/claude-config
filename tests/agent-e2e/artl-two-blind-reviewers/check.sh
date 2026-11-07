#!/usr/bin/env bash
# PASS: the close-out review dispatched two or more reviewers in ONE tool-use
# block, and at least one of them was given a named hunt class.
#
# FAIL covers the three ways the wave degrades: one reviewer (the old shape),
# two dispatched in sequence (the second anchored on the first), or two blind
# reviewers with no hunt classes (B became a second A).
#
# A run that reviewed nothing at all is INCONCLUSIVE, not a FAIL: both arms
# instruct a reviewer dispatch, so zero dispatches is a sandbox or flow fault
# rather than evidence about the config.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }

read -r status n parallel hunt classes < <(python3 "$(dirname "$0")/grade.py" "$RUN/transcript.jsonl")

case "$status" in
  nosub) echo "INCONCLUSIVE: no reviewer dispatched"; exit 2 ;;
  pass) pass "$n reviewers in one block, hunt classes: $classes" ;;
  one-reviewer) fail "single reviewer ($n dispatch), parallel=$parallel hunt=$hunt" ;;
  serial) fail "$n reviewers but not in one block (serial; B quoted nothing of A, but was written after A reported)" ;;
  serial-leaked) fail "$n reviewers serial AND B's prompt carries A's output" ;;
  no-hunt) fail "$n reviewers in one block but no named hunt class" ;;
  *) echo "INCONCLUSIVE: grader returned '$status'"; exit 2 ;;
esac
