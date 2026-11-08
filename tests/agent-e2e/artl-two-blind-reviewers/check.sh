#!/usr/bin/env bash
# PASS: two or more reviewers, each dispatched before any earlier reviewer had
# reported, and at least one carrying a class from hunt-classes.md.
#
# FAIL covers the ways the wave degrades:
#   one-reviewer  the old shape
#   late          a reviewer dispatched after an earlier one's findings existed
#   leaked        that prompt also carries the earlier reviewer's content
#   no-hunt       two blind reviewers, but B became a second A
#
# The grader measures when each reviewer's findings REACHED the main thread --
# a backgrounded dispatch's completion announcement, a foreground dispatch's
# tool result -- so an adjacent-message dispatch counts as blind exactly when
# the harness made it so, rather than by assumption. Shape is for the record.
#
# Two kinds of run are INCONCLUSIVE rather than FAIL, because neither is
# evidence about the config: one that reviewed nothing at all (both arms
# instruct a dispatch, so zero of them is a sandbox or flow fault), and one
# where the transcript records no delivery point for a reviewer, which leaves
# blindness unmeasurable rather than disproved.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }

read -r status n shape hunt classes < <(python3 "$(dirname "$0")/grade.py" "$RUN/transcript.jsonl")

case "$status" in
  nosub) echo "INCONCLUSIVE: no reviewer dispatched"; exit 2 ;;
  nodata) echo "INCONCLUSIVE: $n reviewers, but the transcript records no" \
               "delivery point for an earlier one -- blindness unmeasurable"; exit 2 ;;
  pass) pass "$n reviewers ($shape), blind, hunt classes: $classes" ;;
  one-reviewer) fail "single reviewer ($n dispatch), hunt=$hunt" ;;
  late) fail "$n reviewers but one was dispatched after an earlier reviewer had reported" ;;
  leaked) fail "$n reviewers and a late prompt carries the earlier reviewer's content" ;;
  no-hunt) fail "$n blind reviewers ($shape) but no class from hunt-classes.md" ;;
  *) echo "INCONCLUSIVE: grader returned '$status'"; exit 2 ;;
esac
