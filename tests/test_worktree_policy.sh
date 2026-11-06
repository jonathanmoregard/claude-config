#!/usr/bin/env bash
# Invariants for the ~/.claude master pin (.githooks/ + installer + helper +
# session observer), exercised with real git against throwaway repositories.
#
# Nothing here touches the real ~/.claude checkout, its git config, the real
# global hooks, or the network: the global git config and global hooks dir are
# replaced with per-run fakes that record every call.
#
# The invariants, not the implementation:
#   * the primary checkout cannot gain a commit, leave master, move master
#     backwards, or push — however the operation is spelled;
#   * the primary still fast-forwards (the real scripts/claude-pull.sh), fetches,
#     adds/removes worktrees, and can always get back onto master;
#   * linked worktrees on feature branches behave like any other git repo;
#   * every machine-wide hook still runs, with its exit status intact;
#   * repositories that are not jonathanmoregard/claude-config are untouched.
#
# Run:  tests/test_worktree_policy.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); printf 'PASS %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL %s: %s\n' "$1" "$2" >&2; }

ROOT="$(mktemp -d "${TMPDIR:-/tmp}/dot-claude-worktree-policy.XXXXXX")"
cleanup() { [ -n "$ROOT" ] && [ -d "$ROOT" ] && rm -rf -- "$ROOT"; }
trap cleanup EXIT

export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid
export GIT_EDITOR=true

# --- fake machine-wide hooks: record argv + stdin, fail on demand -----------
GLOBAL_HOOKS="$ROOT/global-hooks"
WITNESS="$ROOT/global-witness"
mkdir -p "$GLOBAL_HOOKS"
: > "$WITNESS"
for hook in pre-commit pre-push prepare-commit-msg reference-transaction; do
  cat > "$GLOBAL_HOOKS/$hook" <<EOF
#!/usr/bin/env bash
{ printf 'GLOBAL-$hook:%s\n' "\$*"; sed 's/^/STDIN-$hook:/'; } >> "$WITNESS"
[ "\${GLOBAL_FAIL_HOOK:-}" = "$hook" ] && exit 23
exit 0
EOF
  chmod +x "$GLOBAL_HOOKS/$hook"
done
printf '[core]\n\thooksPath = %s\n[init]\n\tdefaultBranch = master\n' "$GLOBAL_HOOKS" > "$ROOT/global.gitconfig"
export GIT_CONFIG_GLOBAL="$ROOT/global.gitconfig"
export GIT_CONFIG_SYSTEM=/dev/null

# claude-pull.sh side effects stay in the sandbox.
export CLAUDE_PULL_STATE_ROOT="$ROOT/pull-state"
export CLAUDE_PULL_ONLINE_PROBE_HOST=localhost
mkdir -p "$ROOT/fakebin"
printf '#!/usr/bin/env bash\necho "NOTIFY: $*" >> %s/notifications\n' "$ROOT" > "$ROOT/fakebin/notify-send"
chmod +x "$ROOT/fakebin/notify-send"
export PATH="$ROOT/fakebin:$PATH"

# Run a command; capture output and exit status into OUT / RC.
try() { OUT="$("$@" 2>&1)"; RC=$?; }

# expect_refused <name> <grep-pattern> <cmd...>: non-zero exit AND our message.
expect_refused() {
  local name="$1" pat="$2"; shift 2
  try "$@"
  if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | grep -q -- "$pat"; then ok "$name"
  else bad "$name" "rc=$RC output=$(printf '%s' "$OUT" | head -8 | tr '\n' ' ')"; fi
}
expect_ok() {
  local name="$1"; shift
  try "$@"
  if [ "$RC" -eq 0 ]; then ok "$name"
  else bad "$name" "rc=$RC output=$(printf '%s' "$OUT" | head -8 | tr '\n' ' ')"; fi
}
# Put the primary's index/tree back to HEAD after a refused operation left its
# half-applied changes behind. A same-commit reset is a no-op ref update, which
# the pin allows.
settle() { git -C "$PRIMARY" reset -q --hard HEAD >/dev/null 2>&1; }
head_of() { git -C "$1" rev-parse "${2:-HEAD}"; }
branch_of() { git -C "$1" symbolic-ref --quiet --short HEAD || printf '(detached)'; }

# --- an origin that looks like jonathanmoregard/claude-config ---------------
ORIGIN="$ROOT/remote/jonathanmoregard/claude-config.git"
PRIMARY="$ROOT/home/.claude"
WT_ROOT="$ROOT/worktrees"
export CLAUDE_WORKTREE_ROOT="$WT_ROOT"
mkdir -p "$(dirname "$ORIGIN")" "$(dirname "$PRIMARY")"
git init -q --bare -b master "$ORIGIN"
git init -q -b master "$ROOT/seed"
(
  cd "$ROOT/seed" || exit 1
  mkdir -p .githooks scripts
  cp -a "$REPO_ROOT"/.githooks/. .githooks/
  # `scripts/` is part of the runtime subset, so in a migrated checkout it
  # sits at `runtime/scripts/`. Take whichever this tree has: the suite is
  # about what these scripts do, not about where the repository keeps them.
  src_scripts="$REPO_ROOT/scripts"
  [ -d "$REPO_ROOT/runtime/scripts" ] && src_scripts="$REPO_ROOT/runtime/scripts"
  for s in install-worktree-policy.sh claude-worktree.sh worktree-policy-session-start.sh claude-pull.sh config-repo.sh; do
    cp -a "$src_scripts/$s" scripts/
  done
  printf '{\n  "model": "upstream-model",\n  "env": {}\n}\n' > settings.json
  printf 'seed\n' > tracked.txt
  git add -A
  git commit -qm seed
  # A second, ordinary commit, so "revert HEAD" below reverts content rather
  # than the commit that ships .githooks (a revert that deletes the hooks
  # directory also deletes the hooks that would refuse it — see the PR notes).
  printf 'content\n' >> tracked.txt
  git commit -qam content
  git remote add origin "$ORIGIN"
  git push -q -u origin master
)
git clone -q "$ORIGIN" "$PRIMARY"

# Push a new commit to origin/master from an independent clone.
advance_origin() { # advance_origin <file> <content>
  local w="$ROOT/writer"
  [ -d "$w" ] || git clone -q "$ORIGIN" "$w"
  git -C "$w" pull -q --ff-only origin master
  printf '%s\n' "$2" > "$w/$1"
  git -C "$w" add -A
  git -C "$w" commit -qm "remote: $1"
  git -C "$w" push -q origin master
}

# --- installer ---------------------------------------------------------------
git -C "$PRIMARY" config core.hooksPath "$ROOT/some-project-hooks"
expect_refused "installer will not replace an unrelated repo-local hooksPath" \
  'repository-local core.hooksPath' "$PRIMARY/scripts/install-worktree-policy.sh" "$PRIMARY"
git -C "$PRIMARY" config --unset core.hooksPath

# The incident's precondition, created the only way it can be once the policy
# exists: before activation. Another session's branch, live checkout parked on it.
git -C "$PRIMARY" switch -q -c feat/other-session
try git -C "$PRIMARY" commit -q --allow-empty -m "settings edit on a parked branch"
if [ "$RC" -eq 0 ]; then ok "baseline: without the pin, a commit lands on the parked branch"
else bad "baseline: without the pin, a commit lands on the parked branch" "rc=$RC $OUT"; fi
git -C "$PRIMARY" switch -q master

expect_ok "installer activates on a jonathanmoregard/claude-config clone" \
  "$PRIMARY/scripts/install-worktree-policy.sh" "$PRIMARY"
if [ "$(git -C "$PRIMARY" config --get core.hooksPath)" = "$PRIMARY/.githooks" ]; then
  ok "hooksPath is the absolute primary .githooks"
else
  bad "hooksPath is the absolute primary .githooks" "got $(git -C "$PRIMARY" config --get core.hooksPath)"
fi
expect_ok "installer is idempotent" "$PRIMARY/scripts/install-worktree-policy.sh" "$PRIMARY"
if [ -z "$(git -C "$PRIMARY" status --porcelain --untracked-files=all)" ]; then
  ok "activation leaves the primary tree clean"
else
  bad "activation leaves the primary tree clean" "$(git -C "$PRIMARY" status --porcelain)"
fi

# --- the incident, after activation -----------------------------------------
P_MASTER="$(head_of "$PRIMARY" master)"
P_FEAT="$(head_of "$PRIMARY" feat/other-session)"
expect_refused "primary cannot be parked on another session's branch" \
  'pinned to master' git -C "$PRIMARY" switch feat/other-session
expect_refused "primary cannot checkout -b a new branch" \
  'pinned to master' git -C "$PRIMARY" checkout -b feat/new-here
expect_refused "primary cannot detach HEAD" \
  'pinned to master' git -C "$PRIMARY" checkout --detach HEAD
[ "$(branch_of "$PRIMARY")" = master ] && ok "primary is still on master" \
  || bad "primary is still on master" "on $(branch_of "$PRIMARY")"

# Simulate a pre-policy parking (hooks bypassed), then the commit attempt.
git -C "$PRIMARY" -c core.hooksPath=/dev/null switch -q feat/other-session
printf 'edit\n' >> "$PRIMARY/tracked.txt"
git -C "$PRIMARY" add tracked.txt
expect_refused "commit on a parked feature branch is refused" \
  'commits are forbidden in the primary checkout' git -C "$PRIMARY" commit -m incident
expect_refused "... and --no-verify does not get it through" \
  'commits are forbidden in the primary checkout' git -C "$PRIMARY" commit --no-verify -m incident
[ "$(head_of "$PRIMARY" feat/other-session)" = "$P_FEAT" ] && ok "parked branch did not move" \
  || bad "parked branch did not move" "moved"
expect_refused "update-ref cannot advance a feature branch from the primary" \
  'advancing a branch other than master' git -C "$PRIMARY" update-ref refs/heads/feat/other-session "$P_MASTER"
git -C "$PRIMARY" restore --staged tracked.txt
# Recovery must work from a parked branch with a dirty tree.
expect_ok "git switch master recovers from a parked branch with a dirty tree" \
  git -C "$PRIMARY" switch -q master
git -C "$PRIMARY" restore tracked.txt

# --- every other way of writing history in the primary ----------------------
printf 'x\n' >> "$PRIMARY/tracked.txt"; git -C "$PRIMARY" add tracked.txt
expect_refused "commit on master in the primary is refused" \
  'commits are forbidden' git -C "$PRIMARY" commit -m direct
git -C "$PRIMARY" restore --staged tracked.txt; git -C "$PRIMARY" restore tracked.txt
expect_refused "commit --amend is refused" \
  'commits are forbidden' git -C "$PRIMARY" commit --amend --no-edit
SIDE="$(git -C "$PRIMARY" commit-tree "$(head_of "$PRIMARY" 'HEAD^{tree}')" -p HEAD -m side)"
git -C "$PRIMARY" -c core.hooksPath=/dev/null update-ref refs/heads/feat/side "$SIDE"
expect_refused "cherry-pick is refused" 'commits are forbidden' \
  git -C "$PRIMARY" cherry-pick --allow-empty feat/side
git -C "$PRIMARY" cherry-pick --abort >/dev/null 2>&1; settle
expect_refused "revert is refused" 'commits are forbidden' git -C "$PRIMARY" revert --no-edit HEAD
git -C "$PRIMARY" revert --abort >/dev/null 2>&1; settle
DIVERGE="$(git -C "$PRIMARY" commit-tree "$(head_of "$PRIMARY" 'HEAD^{tree}')" -p HEAD~0 -m diverge)"
git -C "$PRIMARY" -c core.hooksPath=/dev/null update-ref refs/heads/feat/diverge "$DIVERGE"
expect_refused "merge (creating a merge commit) is refused" 'commits are forbidden' \
  git -C "$PRIMARY" merge --no-ff --no-verify -m m feat/side
git -C "$PRIMARY" merge --abort >/dev/null 2>&1; settle
expect_refused "rebase is refused" 'pinned to master' git -C "$PRIMARY" rebase feat/side
git -C "$PRIMARY" rebase --abort >/dev/null 2>&1; settle
PREPUSH_SEEN="$(grep -c '^GLOBAL-pre-push:' "$WITNESS")"
expect_refused "push from the primary is refused" 'pushes are forbidden' \
  git -C "$PRIMARY" push origin master
expect_refused "push of another branch from the primary is refused" 'pushes are forbidden' \
  git -C "$PRIMARY" push origin feat/side
# A refused push must have no side effects. The machine-wide pre-push has a
# body keyed on the ~/.claude toplevel (mcp-sync) that rewrites and STAGES a
# tracked file in the live checkout; running it before refusing leaves the live
# checkout dirty after a push that never happened.
[ "$(grep -c '^GLOBAL-pre-push:' "$WITNESS")" -eq "$PREPUSH_SEEN" ] \
  && ok "a refused primary push does not run the global pre-push" \
  || bad "a refused primary push does not run the global pre-push" "global pre-push ran"
[ "$(head_of "$PRIMARY" master)" = "$P_MASTER" ] && [ "$(branch_of "$PRIMARY")" = master ] \
  && [ -z "$(git -C "$PRIMARY" status --porcelain --untracked-files=no)" ] \
  && ok "after all refusals: master unmoved, on master, tree clean" \
  || bad "after all refusals: master unmoved, on master, tree clean" "$(git -C "$PRIMARY" status -sb)"

# --- what the primary must still do -----------------------------------------
advance_origin upstream.txt one
expect_ok "fetch works in the primary" git -C "$PRIMARY" fetch -q origin
expect_ok "status/log/diff work in the primary" \
  bash -c "git -C '$PRIMARY' status >/dev/null && git -C '$PRIMARY' log -1 >/dev/null && git -C '$PRIMARY' diff origin/master >/dev/null"

# The real claude-pull.sh, as the timer runs it, with a local /model choice to carry.
python3 - "$PRIMARY/settings.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); d["model"] = "local-model"
json.dump(d, open(p, "w"), indent=2); open(p, "a").write("\n")
PY
BEFORE_PULL="$(head_of "$PRIMARY")"
try env CLAUDE_PULL_REPO="$PRIMARY" "$PRIMARY/scripts/claude-pull.sh"
if [ "$RC" -eq 0 ] && [ "$(head_of "$PRIMARY")" = "$(head_of "$PRIMARY" origin/master)" ] \
  && [ "$(head_of "$PRIMARY")" != "$BEFORE_PULL" ] && [ "$(branch_of "$PRIMARY")" = master ]; then
  ok "real claude-pull.sh fast-forwards the pinned primary"
else
  bad "real claude-pull.sh fast-forwards the pinned primary" "rc=$RC $OUT"
fi
grep -q '"local-model"' "$PRIMARY/settings.json" && ok "claude-pull.sh still carries the local model across the pull" \
  || bad "claude-pull.sh still carries the local model across the pull" "$(cat "$PRIMARY/settings.json")"
git -C "$PRIMARY" checkout -q HEAD -- settings.json
advance_origin upstream.txt two
try env CLAUDE_PULL_REPO="$PRIMARY" CLAUDE_PULL_EXPECT_BRANCH=master \
  CLAUDE_PULL_EXPECT_UPSTREAM=origin/master CLAUDE_PULL_REQUIRE_CLEAN=1 "$PRIMARY/scripts/claude-pull.sh"
[ "$RC" -eq 0 ] && [ "$(head_of "$PRIMARY")" = "$(head_of "$PRIMARY" origin/master)" ] \
  && ok "strict claude-pull.sh (expect master, clean) fast-forwards" \
  || bad "strict claude-pull.sh (expect master, clean) fast-forwards" "rc=$RC $OUT"

AFTER_FF="$(head_of "$PRIMARY")"
expect_refused "reset --hard to an earlier commit is refused" 'non-fast-forward' \
  git -C "$PRIMARY" reset --hard HEAD~1
expect_refused "update-ref moving master backwards is refused" 'non-fast-forward' \
  git -C "$PRIMARY" update-ref refs/heads/master "$BEFORE_PULL"
expect_refused "deleting master is refused" 'deleting refs/heads/master' \
  git -C "$PRIMARY" update-ref -d refs/heads/master
# git reset writes the index and tree before it moves the ref, so a refused
# reset leaves the tree at the target commit; HEAD itself must not move.
settle
[ "$(head_of "$PRIMARY")" = "$AFTER_FF" ] && ok "master still at the fast-forwarded commit" \
  || bad "master still at the fast-forwarded commit" "moved"
expect_ok "pack-refs and gc still run" \
  bash -c "git -C '$PRIMARY' pack-refs --all --prune && git -C '$PRIMARY' gc -q --prune=now"
expect_ok "feature-branch deletion still works" git -C "$PRIMARY" branch -q -D feat/side feat/diverge

# --- helper + linked worktrees ----------------------------------------------
# A HOME with no dev checkout, so the caller's own clone is the only anchor and
# the real ~/Repos/claude-config can never leak into these runs.
mkdir -p "$ROOT/bare-home"
expect_refused "helper rejects an invalid slug" 'invalid slug' env HOME="$ROOT/bare-home" "$PRIMARY/scripts/claude-worktree.sh" 'a/b'
advance_origin upstream.txt three
STALE="$(head_of "$PRIMARY" master)"
expect_ok "helper creates a worktree" env HOME="$ROOT/bare-home" "$PRIMARY/scripts/claude-worktree.sh" policy-test
LINKED="$WT_ROOT/claude-policy-test"
if [ "$(branch_of "$LINKED")" = feat/policy-test ] && [ "$(head_of "$LINKED")" = "$(head_of "$PRIMARY" origin/master)" ] \
  && [ "$(head_of "$LINKED")" != "$STALE" ]; then
  ok "helper branches off freshly fetched origin/master"
else
  bad "helper branches off freshly fetched origin/master" "branch=$(branch_of "$LINKED")"
fi
expect_refused "helper refuses an existing path" 'already exists' env HOME="$ROOT/bare-home" "$PRIMARY/scripts/claude-worktree.sh" policy-test
expect_ok "plain git worktree add -b from the primary works" \
  git -C "$PRIMARY" worktree add -q -b feat/plain "$WT_ROOT/plain" origin/master
printf 'linked\n' >> "$LINKED/tracked.txt"
git -C "$LINKED" add tracked.txt
expect_ok "linked worktree: commit" git -C "$LINKED" commit -qm linked
expect_ok "linked worktree: amend" git -C "$LINKED" commit -q --amend --no-edit
expect_ok "linked worktree: switch to another feature branch and back" \
  bash -c "git -C '$LINKED' switch -q -c feat/other && git -C '$LINKED' switch -q feat/policy-test"
expect_ok "linked worktree: reset to an earlier commit" git -C "$LINKED" reset -q --hard HEAD~1
git -C "$LINKED" reset -q --hard ORIG_HEAD
advance_origin upstream.txt four
expect_ok "linked worktree: rebase onto origin/master" \
  bash -c "git -C '$LINKED' fetch -q origin && git -C '$LINKED' rebase -q origin/master"
expect_ok "linked worktree: push" git -C "$LINKED" push -q -u origin feat/policy-test
expect_refused "linked worktree: commit on local master is refused" 'local master' \
  bash -c "git -C '$WT_ROOT/plain' -c core.hooksPath=/dev/null checkout -q --ignore-other-worktrees master && git -C '$WT_ROOT/plain' commit -q --allow-empty -m nope"
git -C "$WT_ROOT/plain" -c core.hooksPath=/dev/null switch -q --detach feat/plain
expect_ok "git worktree remove from the primary works" git -C "$PRIMARY" worktree remove "$WT_ROOT/plain"

# --- the helper after the runtime-projection cutover -------------------------
# Post-cutover, ~/.claude/scripts is a symlink into ~/.claude/runtime, a bind
# mount exposing the checkout's runtime/ subtree alone, and ~/.claude/.git has
# moved to the backing checkout at ~/Repos/claude-config. So the directory the
# script resolves to has no .git above it, and the helper has to find the
# checkout by name rather than by its own location. The fixture reproduces that
# shape with a copy: what matters is that nothing up the tree is a checkout.
SRC_SCRIPTS="$REPO_ROOT/scripts"
[ -d "$REPO_ROOT/runtime/scripts" ] && SRC_SCRIPTS="$REPO_ROOT/runtime/scripts"
CUT_HOME="$ROOT/cutover-home"
CUT_LIVE="$CUT_HOME/.claude"
mkdir -p "$CUT_LIVE/runtime/scripts" "$CUT_HOME/Repos"
for s in install-worktree-policy.sh claude-worktree.sh config-repo.sh; do
  cp -a "$SRC_SCRIPTS/$s" "$CUT_LIVE/runtime/scripts/"
done
ln -s runtime/scripts "$CUT_LIVE/scripts"
if [ -z "$(git -C "$CUT_LIVE/scripts" rev-parse --show-toplevel 2>/dev/null)" ]; then
  ok "fixture: the projected scripts directory is outside any checkout"
else
  bad "fixture: the projected scripts directory is outside any checkout" \
    "git found $(git -C "$CUT_LIVE/scripts" rev-parse --show-toplevel 2>/dev/null)"
fi
git clone -q "$ORIGIN" "$CUT_HOME/Repos/claude-config"

expect_ok "helper works when reached through the projection" \
  env HOME="$CUT_HOME" "$CUT_LIVE/scripts/claude-worktree.sh" projected
PROJECTED="$WT_ROOT/claude-projected"
if [ "$(branch_of "$PROJECTED")" = feat/projected ] \
  && printf '%s' "$(git -C "$PROJECTED" rev-parse --git-common-dir)" \
     | grep -q "$CUT_HOME/Repos/claude-config"; then
  ok "helper branches in the backing checkout, not the live path"
else
  bad "helper branches in the backing checkout, not the live path" \
    "branch=$(branch_of "$PROJECTED") gitdir=$(git -C "$PROJECTED" rev-parse --git-common-dir 2>&1)"
fi

# An explicit override wins over both, so a second checkout can be addressed
# without moving the live tree.
git clone -q "$ORIGIN" "$ROOT/alt-config"
expect_ok "helper honours CLAUDE_CONFIG_REPO" \
  env HOME="$CUT_HOME" CLAUDE_CONFIG_REPO="$ROOT/alt-config" \
  "$CUT_LIVE/scripts/claude-worktree.sh" projected-alt
if printf '%s' "$(git -C "$WT_ROOT/claude-projected-alt" rev-parse --git-common-dir 2>&1)" \
  | grep -q "$ROOT/alt-config"; then
  ok "CLAUDE_CONFIG_REPO decides which checkout the worktree belongs to"
else
  bad "CLAUDE_CONFIG_REPO decides which checkout the worktree belongs to" \
    "gitdir=$(git -C "$WT_ROOT/claude-projected-alt" rev-parse --git-common-dir 2>&1)"
fi

# Nothing to resolve must name what it looked for, not leak a raw git error.
mkdir -p "$ROOT/bare-home"
expect_refused "helper names the checkout it looked for when none resolves" \
  'claude-config' env HOME="$ROOT/bare-home" "$CUT_LIVE/scripts/claude-worktree.sh" nowhere

# The caller's own location is the LAST resort: it is how a developer running
# the script from inside some other clone still gets that clone, but only when
# the dev checkout is not there to answer.
expect_ok "helper still anchors on its own checkout when there is no dev checkout" \
  env HOME="$ROOT/bare-home" "$PRIMARY/scripts/claude-worktree.sh" anchored
if printf '%s' "$(git -C "$WT_ROOT/claude-anchored" rev-parse --git-common-dir 2>&1)" \
  | grep -q "$PRIMARY"; then
  ok "the script's own checkout is the answer when the dev checkout is absent"
else
  bad "the script's own checkout is the answer when the dev checkout is absent" \
    "gitdir=$(git -C "$WT_ROOT/claude-anchored" rev-parse --git-common-dir 2>&1)"
fi

# --- resolution precedence: dev operations resolve to the DEV checkout --------
# The resolver is driven directly. Three distinct clones so the answer names
# which one won: the dev checkout (~/Repos/claude-config), the caller's own
# clone, and a stand-in for the deploy-only clone that serves ~/.claude.
RES_HOME="$ROOT/res-home"
RES_DEV="$RES_HOME/Repos/claude-config"
RES_CALLER="$ROOT/res-caller"
RES_DEPLOY="$ROOT/res-deploy-root/checkout"
mkdir -p "$RES_HOME/Repos"
git clone -q "$ORIGIN" "$RES_DEV"
git clone -q "$ORIGIN" "$RES_CALLER"
mkdir -p "$(dirname "$RES_DEPLOY")"
git clone -q "$ORIGIN" "$RES_DEPLOY"
# resolve <hint> [VAR=value...]: print what claude_config_repo answers.
resolve() {
  local hint="$1"; shift
  env -u CLAUDE_CONFIG_REPO HOME="$RES_HOME" CLAUDE_DEPLOY_CHECKOUT="$RES_DEPLOY" "$@" \
    bash -c 'source "$1/config-repo.sh"; claude_config_repo "$2"' _ "$SRC_SCRIPTS" "$hint"
}
expect_resolves() {
  local name="$1" want="$2"; shift 2
  try resolve "$@"
  if [ "$RC" -eq 0 ] && [ "$OUT" = "$want" ]; then ok "$name"
  else bad "$name" "rc=$RC want=$want got=$(printf '%s' "$OUT" | head -4 | tr '\n' ' ')"; fi
}

expect_resolves "dev checkout outranks the caller's own clone" "$RES_DEV" "$RES_CALLER"
expect_resolves "CLAUDE_CONFIG_REPO outranks the dev checkout" "$ROOT/alt-config" \
  "$RES_CALLER" CLAUDE_CONFIG_REPO="$ROOT/alt-config"
expect_resolves "a caller inside the deploy clone resolves to the dev checkout" "$RES_DEV" "$RES_DEPLOY"
expect_resolves "a caller inside the deploy clone's subtree resolves to the dev checkout" "$RES_DEV" \
  "$RES_DEPLOY/.githooks"
expect_resolves "no dev checkout: the caller's own clone is the answer" "$RES_CALLER" "$RES_CALLER" \
  HOME="$ROOT/bare-home"

# Never the deploy clone, however it is reached: a bug, not a result.
expect_refused "deploy clone as the only candidate is refused, not returned" \
  'CLAUDE_CONFIG_REPO' resolve "$RES_DEPLOY" HOME="$ROOT/bare-home"
if printf '%s' "$OUT" | grep -q "^$RES_DEPLOY\$"; then
  bad "the refusal prints no deploy-clone path on stdout" "stdout=$OUT"
else ok "the refusal prints no deploy-clone path on stdout"; fi
expect_refused "CLAUDE_CONFIG_REPO naming the deploy clone is refused" \
  'deploy' resolve "$RES_CALLER" CLAUDE_CONFIG_REPO="$RES_DEPLOY"

# --- the quiet caller: update-submodules.sh ----------------------------------
# It runs from cron on Mondays, nothing watches its exit status, and its output
# goes to a log nobody opens — so a wrong repository here fails silently. Its
# own origin, because the fixture needs a .gitmodules on origin/master and the
# shared one must stay as the other sections left it.
SUB_ORIGIN="$ROOT/remote/sub/claude-config.git"
SUB_HOME="$ROOT/sub-home"
mkdir -p "$(dirname "$SUB_ORIGIN")" "$SUB_HOME/Repos"
git init -q --bare -b master "$SUB_ORIGIN"
git init -q -b master "$ROOT/sub-seed"
(
  cd "$ROOT/sub-seed" || exit 1
  # A submodule entry with a path and no url: the script reads .gitmodules from
  # origin/master as a blob, logs that it is skipping, and stops before
  # anything that would reach the network. Only the repository this fixture
  # built can produce that line, so the log says WHICH checkout it resolved.
  printf '[submodule "fixture-only"]\n\tpath = vendor/fixture-only\n' > .gitmodules
  git add -A
  git commit -qm seed
  git remote add origin "$SUB_ORIGIN"
  git push -q -u origin master
)
git clone -q "$SUB_ORIGIN" "$SUB_HOME/Repos/claude-config"
mkdir -p "$SUB_HOME/.claude/runtime/scripts"
for s in update-submodules.sh config-repo.sh; do
  cp -a "$SRC_SCRIPTS/$s" "$SUB_HOME/.claude/runtime/scripts/"
done
ln -s runtime/scripts "$SUB_HOME/.claude/scripts"

SUB_LOG="$ROOT/submodule-run.log"
expect_ok "update-submodules runs when reached through the projection" \
  env HOME="$SUB_HOME" CLAUDE_SUBMODULE_LOG="$SUB_LOG" \
  "$SUB_HOME/.claude/scripts/update-submodules.sh"
if grep -q 'fixture-only' "$SUB_LOG" 2>/dev/null && grep -q 'no url in .gitmodules' "$SUB_LOG" 2>/dev/null; then
  ok "update-submodules read the backing checkout's own origin/master"
else
  bad "update-submodules read the backing checkout's own origin/master" \
    "log=$(tail -3 "$SUB_LOG" 2>&1 | tr '\n' ' ')"
fi

# The log must not follow the repository: post-cutover that would write an
# un-ignored file inside a checkout automation commits from.
rm -f "$SUB_HOME/.claude/logs/submodule-update.log"
expect_ok "update-submodules runs with the default log path" \
  env HOME="$SUB_HOME" "$SUB_HOME/.claude/scripts/update-submodules.sh"
if [ -s "$SUB_HOME/.claude/logs/submodule-update.log" ] && [ ! -e "$SUB_HOME/Repos/claude-config/logs" ]; then
  ok "the default log stays at ~/.claude/logs, outside the resolved checkout"
else
  bad "the default log stays at ~/.claude/logs, outside the resolved checkout" \
    "live=$(ls -d "$SUB_HOME/.claude/logs/submodule-update.log" 2>&1) in-repo=$(ls -d "$SUB_HOME/Repos/claude-config/logs" 2>&1)"
fi

# The long-standing override still outranks resolution: pointed at a checkout
# whose origin/master has no .gitmodules, the run must find no submodule.
OVERRIDE_LOG="$ROOT/submodule-override.log"
expect_ok "update-submodules honours CLAUDE_SUBMODULE_REPO" \
  env HOME="$SUB_HOME" CLAUDE_SUBMODULE_REPO="$ROOT/alt-config" \
  CLAUDE_SUBMODULE_LOG="$OVERRIDE_LOG" \
  "$SUB_HOME/.claude/scripts/update-submodules.sh"
if grep -q 'Done' "$OVERRIDE_LOG" 2>/dev/null && ! grep -q 'fixture-only' "$OVERRIDE_LOG" 2>/dev/null; then
  ok "CLAUDE_SUBMODULE_REPO outranks the resolved checkout"
else
  bad "CLAUDE_SUBMODULE_REPO outranks the resolved checkout" \
    "log=$(tail -3 "$OVERRIDE_LOG" 2>&1 | tr '\n' ' ')"
fi

# --- global hooks stay chained and keep their teeth ---------------------------
for h in pre-commit prepare-commit-msg pre-push reference-transaction; do
  grep -q "GLOBAL-$h:" "$WITNESS" && ok "global $h still runs" || bad "global $h still runs" "no witness"
done
grep -q 'STDIN-pre-push:refs/heads/feat/policy-test' "$WITNESS" \
  && ok "global pre-push receives the pushed refs on stdin" \
  || bad "global pre-push receives the pushed refs on stdin" "missing"
BEFORE="$(head_of "$LINKED")"
printf 'blocked\n' >> "$LINKED/tracked.txt"; git -C "$LINKED" add tracked.txt
try env GLOBAL_FAIL_HOOK=pre-commit git -C "$LINKED" commit -qm blocked
[ "$RC" -ne 0 ] && [ "$(head_of "$LINKED")" = "$BEFORE" ] && ok "a blocking global pre-commit still blocks" \
  || bad "a blocking global pre-commit still blocks" "rc=$RC"
git -C "$LINKED" restore --staged tracked.txt; git -C "$LINKED" restore tracked.txt
try env GLOBAL_FAIL_HOOK=pre-push git -C "$LINKED" push -q origin feat/policy-test:feat/x
[ "$RC" -ne 0 ] && ok "a blocking global pre-push still blocks" || bad "a blocking global pre-push still blocks" "rc=$RC"

# --- other repositories are untouched ----------------------------------------
OTHER="$ROOT/other"
git init -q -b master "$OTHER"
git -C "$OTHER" remote add origin git@github.com:someone/.claude-fork.git
git -C "$OTHER" remote add mine git@github.com:jonathanmoregard/claude-config.git
git -C "$OTHER" config core.hooksPath "$PRIMARY/.githooks"
expect_refused "installer rejects a repo whose origin is not jonathanmoregard/claude-config" \
  'origin does not point at jonathanmoregard/claude-config' "$PRIMARY/scripts/install-worktree-policy.sh" "$OTHER"
expect_ok "hooks are inert in another repo (commit, switch, push)" bash -c "
  git -C '$OTHER' commit -q --allow-empty -m a && git -C '$OTHER' switch -q -c feat/x &&
  git -C '$OTHER' commit -q --allow-empty -m b && git -C '$OTHER' reset -q --hard HEAD~1"

# --- session observer --------------------------------------------------------
observe() { CLAUDE_PRIMARY_CHECKOUT="$PRIMARY" "$PRIMARY/scripts/worktree-policy-session-start.sh" 2>&1; }
git -C "$PRIMARY" config --unset core.hooksPath
printf 'local edit\n' >> "$PRIMARY/tracked.txt"
OBS="$(observe)"; OBS_RC=$?
if [ "$OBS_RC" -eq 0 ] && [ -z "$(git -C "$PRIMARY" config --local --get core.hooksPath)" ] \
  && printf '%s' "$OBS" | grep -q 'stop and report exact state'; then
  ok "observer reports tracked changes in the primary and changes nothing"
else
  bad "observer reports tracked changes in the primary and changes nothing" "rc=$OBS_RC $OBS"
fi
printf '%s' "$OBS" | grep -q 'other repositories' \
  && ok "observer's stop is scoped to ~/.claude, not every project's session" \
  || bad "observer's stop is scoped to ~/.claude, not every project's session" "$OBS"
git -C "$PRIMARY" restore tracked.txt
# Untracked strays (the live checkout has had MEMORY.md and `e` for weeks) are
# not unknown work the installer could disturb: it only writes git config,
# .git/info/exclude and .githooks/. Leaving the pin OFF because of a stray file,
# and telling every session in every project to stop, is the wrong trade.
touch "$PRIMARY/stray-untracked"
OBS="$(observe)"
if [ "$(git -C "$PRIMARY" config --get core.hooksPath)" = "$PRIMARY/.githooks" ] \
  && printf '%s' "$OBS" | grep -q 'stray-untracked' \
  && ! printf '%s' "$OBS" | grep -q 'stop and report'; then
  ok "observer activates the pin despite untracked strays, and notes them without a stop"
else
  bad "observer activates the pin despite untracked strays, and notes them without a stop" "$OBS"
fi
rm -f -- "$PRIMARY/stray-untracked"
# `/model` writes settings.json's "model" key in the live checkout; claude-pull
# carries exactly that difference across pulls. It is expected state, not
# unknown work, so it neither stops sessions nor holds activation back.
git -C "$PRIMARY" config --unset core.hooksPath
python3 - "$PRIMARY/settings.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); d["model"] = "operator-choice"
json.dump(d, open(p, "w"), indent=2); open(p, "a").write("\n")
PY
OBS="$(observe)"
if [ "$(git -C "$PRIMARY" config --get core.hooksPath)" = "$PRIMARY/.githooks" ] \
  && ! printf '%s' "$OBS" | grep -q 'stop and report'; then
  ok "observer treats a model-only settings.json change as expected state"
else
  bad "observer treats a model-only settings.json change as expected state" "$OBS"
fi
python3 - "$PRIMARY/settings.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); d["env"]["X"] = "1"
json.dump(d, open(p, "w"), indent=2); open(p, "a").write("\n")
PY
git -C "$PRIMARY" config --unset core.hooksPath
OBS="$(observe)"
if [ -z "$(git -C "$PRIMARY" config --local --get core.hooksPath)" ] \
  && printf '%s' "$OBS" | grep -q 'stop and report'; then
  ok "observer still stops on settings.json changes beyond the model key"
else
  bad "observer still stops on settings.json changes beyond the model key" "$OBS"
fi
git -C "$PRIMARY" restore settings.json
# A linked worktree attached to local master is reported, but does not keep
# the pin switched off either.
git -C "$PRIMARY" config --unset core.hooksPath
git -C "$PRIMARY" worktree add -q -f "$WT_ROOT/on-master" master
OBS="$(observe)"
if [ "$(git -C "$PRIMARY" config --get core.hooksPath)" = "$PRIMARY/.githooks" ] \
  && printf '%s' "$OBS" | grep -q 'repair: git -C'; then
  ok "observer activates the pin while reporting a linked worktree on master"
else
  bad "observer activates the pin while reporting a linked worktree on master" "$OBS"
fi
git -C "$WT_ROOT/on-master" switch -q --detach
git -C "$PRIMARY" worktree remove "$WT_ROOT/on-master"
git -C "$PRIMARY" config --unset core.hooksPath
OBS="$(observe)"
[ "$(git -C "$PRIMARY" config --get core.hooksPath)" = "$PRIMARY/.githooks" ] \
  && ok "observer self-heals activation on a clean primary" || bad "observer self-heals activation on a clean primary" "$OBS"
OBS="$(observe)"
[ -z "$OBS" ] && ok "observer is silent when healthy" || bad "observer is silent when healthy" "$OBS"

# The observer must survive the cutover too, and this is the one that fails by
# saying NOTHING: reached through the projection, with no CLAUDE_PRIMARY_CHECKOUT
# set, the old script resolved to a directory with no .git and exited 0.
cp -a "$SRC_SCRIPTS/worktree-policy-session-start.sh" "$CUT_LIVE/runtime/scripts/"
CUT_REPO="$CUT_HOME/Repos/claude-config"
git -C "$CUT_REPO" config --unset core.hooksPath 2>/dev/null || true
printf 'local edit\n' >> "$CUT_REPO/tracked.txt"
PROJ_OBS="$(env HOME="$CUT_HOME" "$CUT_LIVE/scripts/worktree-policy-session-start.sh" 2>&1)"
PROJ_RC=$?
if [ "$PROJ_RC" -eq 0 ] && printf '%s' "$PROJ_OBS" | grep -q 'stop and report exact state'; then
  ok "observer reports the backing checkout when reached through the projection"
else
  bad "observer reports the backing checkout when reached through the projection" \
    "rc=$PROJ_RC out=${PROJ_OBS:-<silence — the fail-open this check exists for>}"
fi
printf '%s' "$PROJ_OBS" | grep -q "$CUT_REPO" \
  && ok "observer names the resolved checkout, not the live path" \
  || bad "observer names the resolved checkout, not the live path" "${PROJ_OBS:-<silence>}"
git -C "$CUT_REPO" restore tracked.txt
# The historical seam still outranks resolution.
printf 'local edit\n' >> "$PRIMARY/tracked.txt"
PROJ_OBS="$(env HOME="$CUT_HOME" CLAUDE_PRIMARY_CHECKOUT="$PRIMARY" \
  "$CUT_LIVE/scripts/worktree-policy-session-start.sh" 2>&1)"
printf '%s' "$PROJ_OBS" | grep -q "$PRIMARY" \
  && ok "CLAUDE_PRIMARY_CHECKOUT outranks resolution for the observer" \
  || bad "CLAUDE_PRIMARY_CHECKOUT outranks resolution for the observer" "${PROJ_OBS:-<silence>}"
git -C "$PRIMARY" restore tracked.txt
# A machine with no checkout anywhere stays silent: this runs at every
# SessionStart in every project, and absence is not a condition to report.
QUIET_OBS="$(env HOME="$ROOT/bare-home" "$CUT_LIVE/scripts/worktree-policy-session-start.sh" 2>&1)"
QUIET_RC=$?
[ "$QUIET_RC" -eq 0 ] && [ -z "$QUIET_OBS" ] \
  && ok "observer stays silent where there is no checkout at all" \
  || bad "observer stays silent where there is no checkout at all" "rc=$QUIET_RC out=$QUIET_OBS"

# --- registration --------------------------------------------------------------
live_settings="$REPO_ROOT/settings.json"
[ -f "$REPO_ROOT/runtime/settings.json" ] && live_settings="$REPO_ROOT/runtime/settings.json"
if python3 - "$live_settings" <<'PY'
import json, sys
hooks = json.load(open(sys.argv[1]))["hooks"]["SessionStart"]
assert any("worktree-policy-session-start.sh" in h["command"] for e in hooks for h in e["hooks"])
PY
then ok "settings.json registers the session observer"
else bad "settings.json registers the session observer" "missing"; fi

printf '\npassed: %s  failed: %s\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
