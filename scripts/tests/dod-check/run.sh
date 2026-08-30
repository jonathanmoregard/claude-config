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

# ---- a measurement that FAILED is not a measurement that came back clean ---
# Same defect shape as the exit-2 case above, one layer down: git and gh both
# report failure in the exit code, and every place that reads only stdout turns
# "could not look" into "looked, found nothing".

# `dirty` was `bool(git(["status","--porcelain"], root)[1])` — stdout only. A
# git that cannot run returns ('', err) with rc 124 (timeout) or 127 (OSError),
# and an unreadable index returns rc 128, so a tree full of uncommitted work
# read as clean. Reproduced with the index at mode 000: `git status --porcelain`
# exits 128 with empty stdout while rev-parse, rev-list and fetch all still work.
fresh_repo; git push -q -u origin feat/x
echo "uncommitted and invisible" >> src/a.py
chmod 000 "$WORK/.git/index"
case_run "an unreadable index is not a clean tree" green 1 "TREE_UNKNOWN"
chmod 644 "$WORK/.git/index"

# The same asymmetry on the two ladders: a failed `rev-list --count @{u}..HEAD`
# yields unpushed=None, which is FALSY on the feature-branch path (no blocker at
# all) while the identical None becomes a blocker on the default-branch path.
# Reproduced by pointing the remote-tracking ref at an object that is not there:
# `@{u}` still resolves by name, so has_upstream is true, and only the count
# fails.
fresh_repo; git push -q -u origin feat/x
printf '%s\n' "0123456789012345678901234567890123456789" \
  > "$WORK/.git/refs/remotes/origin/feat/x"
case_run "an uncountable upstream is not silently pushed" green 1 "UNPUSHED_UNKNOWN"

# An unrecognised check bucket counts toward `total` but is tallied as neither
# fail nor pending, so it lands in the all-clear branch and an unmeasured check
# contributes to DONE.
fresh_repo; git push -q -u origin feat/x
case_run "an unrecognised check bucket is not a pass" weirdbucket 1 "CHECKS_UNRECOGNISED"
case_run "  ...and it names the bucket it did not know" weirdbucket 1 "neutral"

# reviewThreads(first:100) is not paginated. Thread 101 onwards simply did not
# exist as far as this tool was concerned, and a truncated page of resolved
# threads printed as a clean sheet.
fresh_repo; git push -q -u origin feat/x
# NAME MATTERS HERE. This case asserts exit 0, which IS the clean sheet — the
# earlier name ("...is not a clean sheet") claimed more than the assertion
# checks. Not blocking is deliberate and documented in the Stop gate: blocking
# on an unreadable remote would hang every offline session. What the branch
# fixed is that truncation is now SAID rather than silently dropped.
case_run "a truncated thread page says the threads were not counted" manythreads 0 "only the first 100"

# A `nodes: null` payload passes every lookup and raises on ITERATION. That
# escalated a soft-fail warning into a crash caught only by main()'s blanket
# handler, which reports exit 3 with an empty blockers list — a whole-predicate
# failure caused by an optional, deliberately advisory check.
case_run "a null thread list warns, it does not crash" nullnodes 0 "unexpected shape"

# argparse exits 2 on a usage error, and 2 is this tool's documented n/a code —
# so a typo in the flag reads to every caller as "the definition of done does
# not apply here".
out="$(python3 "$DOD" --repo "$WORK" --bogus 2>&1)"; code=$?
[ "$code" = 3 ] && ok "a usage error is exit 3, not the n/a code" \
  || bad "a usage error is exit 3, not the n/a code" "exit 3" "exit $code — $out"
out="$(python3 "$DOD" --help 2>&1)"; code=$?
[ "$code" = 0 ] && ok "  ...and --help is still exit 0" \
  || bad "  ...and --help is still exit 0" "exit 0" "exit $code — $out"

# ---- a broken git is not "not a git repository" ---------------------------
# `run()` returns 124 on a timeout and 127 when the command cannot be spawned,
# and collect_repo folded both into "not a git repository" -> exit 2. Exit 2 is
# the one code that means STOP LOOKING: the Stop hook reads it as nothing to
# check, skips the repo, unlinks the armfile and ends the turn. Measured end to
# end — same gate, same repo, same real UNPUSHED_COMMITS blocker, and the only
# variable was whether git was on PATH.
fresh_repo; git push -q -u origin feat/x
PY3="$(command -v python3)"
EMPTYBIN="$(mktemp -d)"
out="$(cd "$WORK" && env PATH="$EMPTYBIN" "$PY3" "$DOD" --repo "$WORK" 2>&1)"; code=$?
rmdir "$EMPTYBIN" 2>/dev/null
if [ "$code" = 3 ]; then ok "a git that cannot run is exit 3, not n/a"
else bad "a git that cannot run is exit 3, not n/a" "exit 3" "exit $code — $out"; fi
if grep -qF "COULD NOT MEASURE" <<<"$out"; then ok "  ...and says it could not measure"
else bad "  ...and says it could not measure" "COULD NOT MEASURE" "$out"; fi

# ---- an unreadable base ref is not "this repo has no CI" -------------------
# ci_configured required BOTH refs to be unreadable before admitting it could
# not tell. With HEAD readable and empty, one unreadable ref returned
# False = "no CI configured", which waves the branch through. The trigger needs
# no tampering: rename the base branch upstream while the local origin/HEAD
# symref still names the old one, and `git fetch origin <base>` fails while
# everything else keeps working. Reproduced 2026-08-30 on a branch that had
# deleted its only workflow: exit 0, DONE, no NO_CHECKS.
fresh_repo
git switch -q master
mkdir -p .github/workflows
printf "$WF" > .github/workflows/ci.yml
git add -A; git commit -qm ci; git push -q origin master
git switch -q -C feat/x master
git rm -q -r .github/workflows; git commit -qm "drop ci"
git push -q -u origin feat/x
git update-ref -d refs/remotes/origin/master
git remote set-url origin "$HERE/gone-away.git"
case_run "an unreadable base ref still demands CI" nochecks 1 "NO_CHECKS"
case_run "  ...and says the ref could not be read"  nochecks 1 "REMOTE_UNVERIFIED"

# ---- a git that answers ONCE and then fails ------------------------------
# The GIT_UNAVAILABLE guard covered only the FIRST git call. A git that answered
# `rev-parse --show-toplevel` and then failed on `git remote` still produced n/a,
# and n/a is the code the Stop hook reads as "nothing to check here": measured,
# it skipped the repo, unlinked the armfile and ended the turn with a real
# unpushed commit sitting in it. Nine of nine later-call cases were exposed.
fresh_repo; git push -q -u origin feat/x
echo "unpushed work" >> src/a.py; git add -A; git commit -qm "real work"
STUBGIT="$(mktemp -d)"
REALGIT="$(command -v git)"
printf '#!/usr/bin/env bash\n[ "$1" = remote ] && [ $# -eq 1 ] && exit 127\nexec %s "$@"\n' \
  "$REALGIT" > "$STUBGIT/git"
chmod +x "$STUBGIT/git"
out="$(cd "$WORK" && env PATH="$STUBGIT:$HERE:$PATH" DOD_FIXTURE=green \
       python3 "$DOD" --repo "$WORK" 2>&1)"; code=$?
rm -f "$STUBGIT/git"; rmdir "$STUBGIT" 2>/dev/null
if [ "$code" = 3 ]; then ok "a git that fails on a LATER call is exit 3 too"
else bad "a git that fails on a LATER call is exit 3 too" "exit 3" "exit $code — $out"; fi
if grep -qF "COULD NOT MEASURE" <<<"$out"; then ok "  ...and does not claim there is no remote"
else bad "  ...and does not claim there is no remote" "COULD NOT MEASURE" "$out"; fi

# ---- checks that were all SKIPPED are not checks that passed --------------
# `total > 0` skips the NO_CHECKS branch, and `skipping` was tallied and then
# never read — so a PR whose every job skipped reported DONE. One skipped job
# away from NO_CHECKS, and the verdict flips from blocked to green.
fresh_repo; git push -q -u origin feat/x
case_run "a PR where every check skipped is not DONE" allskip 1 "CHECKS_ALL_SKIPPED"
# ...but a skipped job ALONGSIDE a real one is ordinary, and must stay green.
case_run "a skipped job beside a passing one is fine" mixedskip 0 "DONE"

# A shape gh does not return today, reaching `for row in rows`. Exit 3, not a
# blocker: the check state was never READ, and this tool's exit 3 is exactly
# "could not measure" — callers treat it like 1. What must not happen is the
# AttributeError reaching main()'s blanket handler, which reports the same 3
# with a bare "dod-check crashed" and no idea what was wrong.
case_run "a non-list check payload is named, not crashed on" dictchecks 3 "expected a list"

# ---- the integrity scan runs on the DIRECT-PUSH ladder too ----------------
# It sat below the direct-push return, so work pushed straight to the default
# branch was never scanned for deleted tests, continue-on-error or `|| true`.
# Reproduced with all three weakenings: DONE on this ladder, INTEGRITY on the
# PR ladder, same commit. The window where it is measurable is the one before
# the work ships — committed, not yet pushed — because being level with origin
# collapses the merge-base onto HEAD and empties the range by construction.
fresh_repo
git switch -q master
git rm -q tests/test_a.py; git commit -qm "drop the test on main"
case_run "a deleted test on the default branch is flagged" green 1 "TEST_FILE_DELETED"
case_run "  ...as an integrity blocker, not just a note" green 1 "INTEGRITY"

# ---- an uncountable gap is not a gap of zero, on either ladder ------------
# `if repo["behind"]:` swallowed None — the fetch worked, the rev-list did not,
# and "level with origin" was never established while that IS this ladder's
# terminal condition.
fresh_repo
git switch -q master
STUBGIT2="$(mktemp -d)"
printf '#!/usr/bin/env bash\nif [ "$1" = rev-list ] && [ "$3" = "HEAD..origin/master" ]; then exit 128; fi\nexec %s "$@"\n' \
  "$REALGIT" > "$STUBGIT2/git"
chmod +x "$STUBGIT2/git"
out="$(cd "$WORK" && env PATH="$STUBGIT2:$HERE:$PATH" DOD_FIXTURE=green \
       python3 "$DOD" --repo "$WORK" 2>&1)"; code=$?
rm -f "$STUBGIT2/git"; rmdir "$STUBGIT2" 2>/dev/null
if [ "$code" = 1 ] && grep -qF "BEHIND_UNKNOWN" <<<"$out"; then
  ok "an uncountable 'behind' blocks instead of reading as zero"
else
  bad "an uncountable 'behind' blocks instead of reading as zero" "exit 1 + BEHIND_UNKNOWN" "exit $code — $out"
fi

# Same shape after a merge: None became NOT_ON_DEFAULT, a confident and WRONG
# blocker telling the agent to go home while stranded commits may still exist.
fresh_repo; git push -q -u origin feat/x
STUBGIT3="$(mktemp -d)"
printf '#!/usr/bin/env bash\nif [ "$1" = rev-list ] && [ "$3" = "origin/master..HEAD" ]; then exit 128; fi\nexec %s "$@"\n' \
  "$REALGIT" > "$STUBGIT3/git"
chmod +x "$STUBGIT3/git"
out="$(cd "$WORK" && env PATH="$STUBGIT3:$HERE:$PATH" DOD_FIXTURE=merged \
       python3 "$DOD" --repo "$WORK" 2>&1)"; code=$?
rm -f "$STUBGIT3/git"; rmdir "$STUBGIT3" 2>/dev/null
if grep -qF "STRANDED_UNKNOWN" <<<"$out"; then
  ok "an uncountable stranded-commit count says so"
else
  bad "an uncountable stranded-commit count says so" "STRANDED_UNKNOWN" "exit $code — $out"
fi

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
