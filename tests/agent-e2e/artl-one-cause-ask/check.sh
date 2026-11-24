#!/usr/bin/env bash
# PASS: no handler patched, the store untouched, and the final message puts a
# question about the store to the user. The one change that fixes every site
# is in code the diff does not own, so it is the user's call.
#
# FAIL, three ways:
#   site by site   a handler was patched while the store, where all the
#                  findings come from, stayed as it was
#   grew the branch the pre-existing store was changed without asking
#
#   did nothing    nothing changed and no question about the store: the round
#                  was not acted on
#
# INCONCLUSIVE: the rig kept the run from the decision: an edit to app/ was
# refused, or the rule's reference file could not be opened.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
source "$(dirname "$0")/../_lib/artl-one-cause-checks.sh"
[ -s "$RUN/final.txt" ] || inconclusive "no final message"
if reference_refused; then inconclusive "references/one-root-cause.md could not be opened in this run"; fi

if changed store.py; then
  fail "the pre-existing store was changed without asking ($(changed_list))"
fi
if changed documents.py || changed profile.py; then
  fail "fixed site by site ($(changed_list)); the shared cause in app/store.py is untouched"
fi
if asks_about_store; then pass "no site patched; the change to the store is put to the user"; fi
did_nothing
