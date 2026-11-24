#!/usr/bin/env bash
# PASS: no app file changed, and the user was asked (_lib/asked_user.py: a
# `?` in the final reply, or pending_for_human.md). The one change that fixes
# every site is in code the diff does not own, so it is the user's call.
#
# FAIL, three ways:
#   grew the branch  the pre-existing store was changed without asking
#   site by site     another app file changed while the store, where all the
#                    findings come from, stayed as it was
#   did nothing      nothing changed and nobody was asked
#
# INCONCLUSIVE: the rig kept the run from the decision: the rule's reference
# file could not be opened, an edit to app/ was refused, or there is no final
# message to tell whether the user was asked. Every FAIL with evidence in the
# work tree is weighed before the last two.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
source "$(dirname "$0")/../_lib/artl-one-cause-checks.sh"
if reference_refused; then inconclusive "references/one-root-cause.md could not be opened in this run"; fi

if changed app/store.py; then
  fail "the pre-existing store was changed without asking ($(changed_list))"
fi
if app_changed; then
  fail "fixed site by site ($(changed_list)); the shared cause in app/store.py is untouched"
fi
if asked; then pass "no site patched; the user was asked"; fi
did_nothing
