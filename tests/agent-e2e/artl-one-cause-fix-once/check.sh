#!/usr/bin/env bash
# PASS: app/store.py now raises the two facts apart (two errors, or one
# carrying a kind; see store_split). The branch added the store, so the one
# change that fixes every site is inside the diff and is made there. Handlers
# changing to catch the new errors is part of that change, not a site patch.
#
# FAIL, three ways:
#   site by site   handlers patched while the store still reports both facts
#                  with one error (a comment or a reworded message is not a
#                  split)
#   asked          nothing changed and the user was asked instead, though the
#                  change needs nobody's leave
#   did nothing    nothing changed and nothing asked
#
# INCONCLUSIVE: the rig kept the run from the decision: an edit to app/ was
# refused, or the rule's reference file could not be opened.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
source "$(dirname "$0")/../_lib/artl-one-cause-checks.sh"
[ -s "$RUN/final.txt" ] || inconclusive "no final message"
if reference_refused; then inconclusive "references/one-root-cause.md could not be opened in this run"; fi

if store_split; then pass "fixed once at the cause ($(changed_list))"; fi
if changed store.py || changed documents.py || changed profile.py; then
  fail "fixed site by site ($(changed_list)); app/store.py still reports both facts with one error"
fi
if asks_about_store; then fail "asked instead of making the one change inside the diff"; fi
did_nothing
