#!/usr/bin/env bash
# dod-check test harness: a throwaway repo + a stubbed gh, one case per state.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DOD="$HOME/.claude/scripts/dod-check.py"
WORK="$HERE/work"
export PATH="$HERE:$PATH"          # stub gh wins
export GIT_CONFIG_GLOBAL=/dev/null # no user config bleed

pass=0; fail=0
ok()  { printf '  PASS  %s\n' "$1"; pass=$((pass+1)); }
bad() { printf '  FAIL  %s\n     want: %s\n     got : %s\n' "$1" "$2" "$3"; fail=$((fail+1)); }

fresh_repo() {
  rm -rf "$WORK" "$HERE/origin.git"; mkdir -p "$WORK"; cd "$WORK" || exit 1
  git init -q -b master .
  git config user.email t@t; git config user.name t
  mkdir -p src tests
  echo "def f(): return 1" > src/a.py
  echo "def test_f(): assert f() == 1" > tests/test_a.py
  echo ".x" > .gitignore
  git add -A; git commit -qm init
  # a bare "origin" so upstream/merge-base logic is real
  git init -q --bare "$HERE/origin.git"
  git remote add origin "$HERE/origin.git"
  git push -q origin master
  git symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/master
  git switch -q -c feat/x
}

# case <name> <fixture> <expected-exit> <substring-that-must-appear>
case_run() {
  local name="$1" fixture="$2" want_exit="$3" want_txt="$4"
  local out code
  out="$(DOD_FIXTURE="$fixture" python3 "$DOD" --repo "$WORK" 2>&1)"; code=$?
  if [ "$code" != "$want_exit" ]; then
    bad "$name" "exit $want_exit" "exit $code — $out"; return
  fi
  if [ -n "$want_txt" ] && ! grep -qF -- "$want_txt" <<<"$out"; then
    bad "$name" "text '$want_txt'" "$out"; return
  fi
  ok "$name"
}

echo "== dod-check test suite =="

# ---- the direct-push ladder -----------------------------------------------
# Being ON the default branch used to be n/a, which made the whole gate
# opt-out: never open a PR and nothing was ever measured, so commits could sit
# unpushed on main forever and read as finished. Where pushing straight to the
# default branch is how work ships, that IS the delivery path — committed,
# pushed, level with origin — and no PR is demanded, because none can exist.
fresh_repo
git switch -q master
case_run "clean pushed default branch is DONE" green 0 "DONE"

fresh_repo
git switch -q master
echo "straight to main" >> src/a.py; git add -A; git commit -qm "direct"
case_run "unpushed commits on main block" green 1 "UNPUSHED_COMMITS"

# Upstream tracking is a local convenience `git push` without -u never sets, so
# the default-branch ladder measures against origin/<base> instead. Before this,
# a branch level with origin reported "never pushed" and no amount of pushing
# could clear it.
fresh_repo
git switch -q master
echo "no -u" >> src/a.py; git add -A; git commit -qm direct
git push -q origin master           # deliberately WITHOUT -u
case_run "pushed without -u still reads as pushed" green 0 "DONE"

# "main is checked out at latest" is part of done.
fresh_repo
git switch -q master
git clone -q "$HERE/origin.git" "$HERE/clone2"
git -C "$HERE/clone2" config user.email t@t; git -C "$HERE/clone2" config user.name t
echo "from elsewhere" >> "$HERE/clone2/src/a.py"
git -C "$HERE/clone2" add -A; git -C "$HERE/clone2" commit -qm elsewhere
git -C "$HERE/clone2" push -q origin master
git fetch -q origin
case_run "behind origin on main blocks" green 1 "BEHIND_REMOTE"

# An unreachable origin is a MISSING MEASUREMENT on this ladder, not a warning:
# its terminal condition IS "level with origin", and a stale remote-tracking ref
# answers that from memory. Measured before the fix: DONE / exit 0 while
# genuinely behind, with the warning printed underneath the green verdict.
fresh_repo
git switch -q master
git remote set-url origin "$HERE/does-not-exist.git"
case_run "unreachable origin is not DONE on main" green 1 "REMOTE_UNVERIFIED"
rm -rf "$HERE/clone2"

# ---- not applicable -------------------------------------------------------

# Must be outside ANY git repo. $HERE lives inside ~/.claude once this suite is
# committed, so a notrepo/ beside the tests is emphatically inside a repo — the
# first run of this case reported exit 3 against ~/.claude itself.
NOTREPO="$(mktemp -d)"
out="$(python3 "$DOD" --repo "$NOTREPO" 2>&1)"; code=$?
rmdir "$NOTREPO" 2>/dev/null
[ "$code" = 2 ] && ok "n/a outside a repo" || bad "n/a outside a repo" "exit 2" "exit $code — $out"

# ---- branch hygiene -------------------------------------------------------
fresh_repo
echo "dirty" >> src/a.py
case_run "dirty tree blocks" green 1 "DIRTY_TREE"

fresh_repo
case_run "never-pushed blocks" green 1 "NOT_PUSHED"

fresh_repo
git push -q -u origin feat/x
echo "more" >> src/a.py; git add -A; git commit -qm more
case_run "unpushed commits block" green 1 "UNPUSHED_COMMITS"

# ---- PR states ------------------------------------------------------------
fresh_repo; git push -q -u origin feat/x
case_run "no PR blocks"            no_pr    1 "NO_PR"
# Merged is no longer the end of the job. The repo is still parked on the dead
# branch, and putting it back on the default branch at latest is the last step
# of the work rather than a chore for Jonathan — "no closing ceremony for me to
# do". What must NOT change is that a merged PR never reads as a live request.
case_run "merged still says do not ask" merged 1 "Do NOT ask for a merge"
case_run "merged says MERGED"      merged   1 "MERGED, NOT FINISHED"
case_run "merged demands going home" merged  1 "NOT_ON_DEFAULT"

# A PR merges the commits it HAD. Push more to the branch afterwards and they
# sit on a dead branch while the PR still reads MERGED — green, work nowhere.
fresh_repo; git push -q -u origin feat/x
echo "after the merge" >> src/a.py; git add -A; git commit -qm "landed nowhere"
git push -q
case_run "commits the merge left behind block" merged 1 "COMMITS_AFTER_MERGE"
case_run "and it counts them"                  merged 1 "1 commit(s) on feat/x"
case_run "closed blocks"           closed   1 "PR_CLOSED"
case_run "pending checks block"    pending  1 "CHECKS_PENDING"
case_run "failing checks block"    failing  1 "CHECKS_FAILING"
case_run "unresolved threads"      failing  1 "UNRESOLVED_THREADS"
case_run "BEHIND blocks"           behind   1 "NOT_MERGEABLE"
case_run "all green is done"       green    0 "DONE"
# `gh pr view` picks ONE pr for the branch and never says it chose. A green PR
# into release must not answer for a blocked one into main.
case_run "two open PRs on one head blocks" two_prs 1 "MULTIPLE_PRS"
case_run "the ambiguity names both"        two_prs 1 "#49 → release"
# Listing failing must not silently read as "only one PR".
case_run "PR list failure warns, not silent" listerror 0 "could not list PRs"
# UNKNOWN is not CLEAN. This exact state produced a DONE verdict on a branch
# that was two commits behind master.
case_run "UNKNOWN mergeability blocks" unknown_merge 1 "MERGEABLE_UNKNOWN"
case_run "gh error is exit 3"      apierror 3 ""
case_run "no GitHub remote is n/a, not a blocker" notgithub 2 "no GitHub remote"
# Advisory, not blocking: threads that actually gate a merge surface as
# mergeStateStatus BLOCKED. But the warning must survive into a DONE
# verdict, or "green" and "green as far as I got" print identically.
case_run "thread count failure warns"  threaderror 0 "not counted"
case_run "measure failure hides verdict" apierror 3 "COULD NOT MEASURE"

# ---- zero checks: vandalism vs. genuinely no CI ---------------------------
# Zero checks means two opposite things and the predicate has to tell them
# apart. Blocking a repo that never had CI parks the agent on a check that is
# never coming; not blocking one that just deleted its workflow is the
# green-by-vandalism hole the blocker exists to plug.
WF='on: [pull_request]\njobs:\n  a:\n    steps:\n      - run: pytest\n'

fresh_repo; git push -q -u origin feat/x
case_run "no CI at all is not a blocker" nochecks 0 "no CI configured"

fresh_repo
mkdir -p .github/workflows
printf "$WF" > .github/workflows/ci.yml
git add -A; git commit -qm ci; git push -q -u origin feat/x
case_run "zero checks WITH CI blocks" nochecks 1 "NO_CHECKS"

# The vandalism case: CI on the base, removed by the branch. Head alone would
# read as "no CI here" and wave it through.
fresh_repo
git switch -q master
mkdir -p .github/workflows
printf "$WF" > .github/workflows/ci.yml
git add -A; git commit -qm ci; git push -q origin master
git switch -q -C feat/x master
git rm -q -r .github/workflows; git commit -qm "drop ci"
git push -q -u origin feat/x
case_run "CI deleted by the branch still blocks" nochecks 1 "NO_CHECKS"

# A workflow that only ever runs on a schedule produces no PR checks, so its
# presence must not be held against the branch.
fresh_repo
mkdir -p .github/workflows
printf 'on:\n  schedule:\n    - cron: "0 3 * * *"\njobs:\n  a:\n    steps:\n      - run: pytest\n' > .github/workflows/nightly.yml
git add -A; git commit -qm nightly; git push -q -u origin feat/x
case_run "schedule-only workflow is not CI" nochecks 0 "none trigger on pull_request"

# ---- integrity scan -------------------------------------------------------
fresh_repo; git push -q -u origin feat/x
git rm -q tests/test_a.py; git commit -qm "drop tests"; git push -q
case_run "deleted test file flagged" green 1 "TEST_FILE_DELETED"

fresh_repo; git push -q -u origin feat/x
mkdir -p .github/workflows
printf 'jobs:\n  a:\n    steps:\n      - run: pytest\n        continue-on-error: true\n' > .github/workflows/ci.yml
git add -A; git commit -qm ci; git push -q
case_run "continue-on-error flagged" green 1 "CI_CONTINUE_ON_ERROR"

fresh_repo; git push -q -u origin feat/x
echo "@pytest.mark.skip" >> tests/test_a.py
git add -A; git commit -qm skip; git push -q
case_run "skipped test flagged" green 1 "TEST_SKIPPED"

fresh_repo; git push -q -u origin feat/x
echo "@pytest.mark.skip  # dod-ok flaky upstream, tracked in #12" >> tests/test_a.py
git add -A; git commit -qm skip; git push -q
case_run "dod-ok justification exempts" green 0 "DONE"

fresh_repo; git push -q -u origin feat/x
echo "y = 1  # ordinary change" >> src/a.py
git add -A; git commit -qm ok; git push -q
case_run "clean diff not flagged" green 0 "DONE"

# ---- a non-GitHub remote is n/a for the PR half ONLY ----------------------
# The committed -> pushed half is measured before `gh` is ever called, and this
# path used to discard it: reproduced 2026-08-30 on a gitlab.com remote with a
# dirty tree AND an unpushed commit, returning exit 2 with `blockers: []` and
# "n/a — nothing to open a PR against", while the same payload's repo block read
# dirty: true, ahead_of_default: 1. Exit 2 is the worst code to lose blockers
# on: the Stop hook reads it as "nothing to check here" and ends the turn.
fresh_repo
echo "committed but never pushed" >> src/a.py
git add -A; git commit -qm "never pushed"
echo "and uncommitted on top" >> src/a.py
case_run "non-GitHub remote keeps the dirty tree it measured" nongithub 1 "DIRTY_TREE"
case_run "  ...and the unpushed half of the ladder"           nongithub 1 "NOT_PUSHED"

# With nothing to report it is still n/a — the PR half genuinely does not apply.
fresh_repo; git push -q -u origin feat/x
case_run "non-GitHub remote with nothing to report is n/a" nongithub 2 "n/a"

# ---- JSON contract --------------------------------------------------------
fresh_repo; git push -q -u origin feat/x
j="$(DOD_FIXTURE=pending python3 "$DOD" --repo "$WORK" --json 2>&1)"
if python3 -c "
import json,sys
d=json.loads(sys.stdin.read())
assert d['done'] is False, 'done should be False'
assert any(b['code']=='CHECKS_PENDING' for b in d['blockers']), 'missing CHECKS_PENDING'
assert d['pr']['url'].startswith('https://'), 'missing pr url'
assert d['checks']['pending']==1, 'pending tally wrong'
" <<<"$j" 2>/dev/null; then ok "JSON contract"; else bad "JSON contract" "valid schema" "$j"; fi

echo
echo "== $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
