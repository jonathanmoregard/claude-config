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

# ---- not applicable -------------------------------------------------------
fresh_repo
git switch -q master
case_run "n/a on default branch" green 2 "on the default branch"

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
case_run "merged reads as DONE"    merged   0 "MERGED"
case_run "merged says do not ask"  merged   0 "Do NOT ask for a merge"
case_run "closed blocks"           closed   1 "PR_CLOSED"
case_run "pending checks block"    pending  1 "CHECKS_PENDING"
case_run "failing checks block"    failing  1 "CHECKS_FAILING"
case_run "unresolved threads"      failing  1 "UNRESOLVED_THREADS"
case_run "BEHIND blocks"           behind   1 "NOT_MERGEABLE"
case_run "all green is done"       green    0 "DONE"
case_run "gh error is exit 3"      apierror 3 ""
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
