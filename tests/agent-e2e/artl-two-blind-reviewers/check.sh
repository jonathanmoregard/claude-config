#!/usr/bin/env bash
# PASS: the close-out review dispatched two or more reviewers, at least one was
# given a named hunt class, and no later reviewer's prompt carries the earlier
# one's output.
#
# FAIL covers the three ways the wave degrades: one reviewer (the old shape),
# a second reviewer anchored on the first's findings, or a second reviewer with
# no hunt classes (B became a second A).
#
# Co-dispatch in one tool-use block is the skill's preferred mechanism and is
# REPORTED, not required: measured across ten runs it never happened, while
# the property it protects -- no content from A in B's prompt -- held every
# time. Grading the mechanism would fail runs that are blind in substance.
#
# A run that reviewed nothing at all is INCONCLUSIVE, not a FAIL: both arms
# instruct a reviewer dispatch, so zero dispatches is a sandbox or flow fault
# rather than evidence about the config.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }

read -r status n block hunt classes < <(python3 "$(dirname "$0")/grade.py" "$RUN/transcript.jsonl")

case "$status" in
  nosub) echo "INCONCLUSIVE: no reviewer dispatched"; exit 2 ;;
  pass) pass "$n reviewers ($block), blind, hunt classes: $classes" ;;
  one-reviewer) fail "single reviewer ($n dispatch), hunt=$hunt" ;;
  leaked) fail "$n reviewers ($block) but a later prompt carries the earlier reviewer's output" ;;
  no-hunt) fail "$n blind reviewers ($block) but no named hunt class" ;;
  *) echo "INCONCLUSIVE: grader returned '$status'"; exit 2 ;;
esac
