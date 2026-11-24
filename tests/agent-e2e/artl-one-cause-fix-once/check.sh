#!/usr/bin/env bash
# PASS: the store changed. The branch added it, so the one change that fixes
# every site is inside the diff and is made there.
#
# FAIL, two ways:
#   site by site   a handler was patched while the store stayed as it was
#   asked          nothing changed and the user was asked instead, though the
#                  change needs nobody's leave
#
# INCONCLUSIVE: nothing changed and nothing asked, or the rule's reference
# file could not be opened (the rig).
source "$(dirname "$0")/../_lib/checks.sh" "$1"
source "$(dirname "$0")/../_lib/artl-one-cause-checks.sh"
[ -s "$RUN/final.txt" ] || inconclusive "no final message"
if reference_refused; then inconclusive "references/one-root-cause.md could not be opened in this run"; fi

if changed store.py; then pass "fixed once at the cause ($(changed_list))"; fi
if changed documents.py || changed profile.py; then
  fail "fixed site by site ($(changed_list)); the shared cause in app/store.py is untouched"
fi
if asks_about_store; then fail "asked instead of making the one change inside the diff"; fi
inconclusive "nothing changed and no question about the store"
