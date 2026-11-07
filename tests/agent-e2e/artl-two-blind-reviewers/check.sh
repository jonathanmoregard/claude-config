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
# `Agent` dispatches are asynchronous, so adjacent-message dispatch is as blind
# as one tool-use block; the grader reads concurrency off the subagent turn
# spans rather than counting tool-use blocks. Shape is reported for the record.
#
# A run that reviewed nothing at all is INCONCLUSIVE, not a FAIL: both arms
# instruct a reviewer dispatch, so zero dispatches is a sandbox or flow fault
# rather than evidence about the config.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }

read -r status n shape hunt classes < <(python3 "$(dirname "$0")/grade.py" "$RUN/transcript.jsonl")

case "$status" in
  nosub) echo "INCONCLUSIVE: no reviewer dispatched"; exit 2 ;;
  pass) pass "$n reviewers ($shape), blind, hunt classes: $classes" ;;
  one-reviewer) fail "single reviewer ($n dispatch), hunt=$hunt" ;;
  late) fail "$n reviewers but one was dispatched after an earlier reviewer had reported" ;;
  leaked) fail "$n reviewers and a late prompt carries the earlier reviewer's content" ;;
  no-hunt) fail "$n blind reviewers ($shape) but no class from hunt-classes.md" ;;
  *) echo "INCONCLUSIVE: grader returned '$status'"; exit 2 ;;
esac
