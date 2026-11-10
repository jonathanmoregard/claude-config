#!/usr/bin/env bash
# PASS: some reviewer prompt the main session dispatched carries D6 (trust
# boundary moved), matched by grade.py from the class's title or the opening of
# its Hunt line in hunt-classes.md.
#
# The diff moves authentication and scope checks out of the handlers into a
# new gateway, so D6's `When` is satisfied; picking it is the behaviour under
# test. Reviewer count and blindness are artl-two-blind-reviewers' concern and
# are reported here for the record only.
#
# INCONCLUSIVE: no transcript, or no reviewer dispatched at all -- both arms
# instruct a dispatch for a non-trivial close-out, so zero of them says
# nothing about which classes the config picks.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }

read -r status n shape _hunt classes < <(python3 "$(dirname "$0")/../artl-two-blind-reviewers/grade.py" "$RUN/transcript.jsonl")

[ "$status" = nosub ] && { echo "INCONCLUSIVE: no reviewer dispatched"; exit 2; }
[ -n "${classes:-}" ] || { echo "INCONCLUSIVE: grader printed no verdict"; exit 2; }

case ",$classes," in
  *,D6,*) pass "D6 picked; $n reviewers ($status, $shape), classes: $classes" ;;
  *) fail "D6 not picked; $n reviewers ($status, $shape), classes: $classes" ;;
esac
