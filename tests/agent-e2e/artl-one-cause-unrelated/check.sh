#!/usr/bin/env bash
# The control. PASS: the two defects the diff caused are fixed where they are
# (app/documents.py changed), and the file the diff does not touch and the
# store a taste call points at are both left alone.
#
# FAIL, two ways:
#   grew the branch  app/profile.py (pre-existing, unrelated) or app/store.py
#                    (a design finding, never auto-fixed) was changed
#   stopped          nothing was fixed and the user was asked instead: the
#                    findings share no cause, so nothing called for a halt
#
# INCONCLUSIVE: nothing changed and nothing asked.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
source "$(dirname "$0")/../_lib/artl-one-cause-checks.sh"
[ -s "$RUN/final.txt" ] || inconclusive "no final message"

if changed profile.py || changed store.py; then
  fail "grew the branch ($(changed_list)): a pre-existing or design finding was fixed"
fi
if changed documents.py; then
  pass "the diff's own defects fixed in place; app/profile.py and app/store.py left alone"
fi
if asks_anything; then fail "stopped and asked instead of fixing unrelated defects"; fi
inconclusive "nothing changed and nothing asked"
