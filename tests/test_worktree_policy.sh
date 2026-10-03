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
#   * repositories that are not jonathanmoregard/.claude are untouched.
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

# --- an origin that looks like jonathanmoregard/.claude ----------------------
ORIGIN="$ROOT/remote/jonathanmoregard/.claude.git"
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
  for s in install-worktree-policy.sh claude-worktree.sh worktree-policy-session-start.sh claude-pull.sh; do
    cp -a "$REPO_ROOT/scripts/$s" scripts/
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

expect_ok "installer activates on a jonathanmoregard/.claude clone" \
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
expect_refused "helper rejects an invalid slug" 'invalid slug' "$PRIMARY/scripts/claude-worktree.sh" 'a/b'
advance_origin upstream.txt three
STALE="$(head_of "$PRIMARY" master)"
expect_ok "helper creates a worktree" "$PRIMARY/scripts/claude-worktree.sh" policy-test
LINKED="$WT_ROOT/claude-policy-test"
if [ "$(branch_of "$LINKED")" = feat/policy-test ] && [ "$(head_of "$LINKED")" = "$(head_of "$PRIMARY" origin/master)" ] \
  && [ "$(head_of "$LINKED")" != "$STALE" ]; then
  ok "helper branches off freshly fetched origin/master"
else
  bad "helper branches off freshly fetched origin/master" "branch=$(branch_of "$LINKED")"
fi
expect_refused "helper refuses an existing path" 'already exists' "$PRIMARY/scripts/claude-worktree.sh" policy-test
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
git -C "$OTHER" remote add mine git@github.com:jonathanmoregard/.claude.git
git -C "$OTHER" config core.hooksPath "$PRIMARY/.githooks"
expect_refused "installer rejects a repo whose origin is not jonathanmoregard/.claude" \
  'origin does not point at jonathanmoregard/.claude' "$PRIMARY/scripts/install-worktree-policy.sh" "$OTHER"
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

# --- registration --------------------------------------------------------------
if python3 - "$REPO_ROOT/settings.json" <<'PY'
import json, sys
hooks = json.load(open(sys.argv[1]))["hooks"]["SessionStart"]
assert any("worktree-policy-session-start.sh" in h["command"] for e in hooks for h in e["hooks"])
PY
then ok "settings.json registers the session observer"
else bad "settings.json registers the session observer" "missing"; fi

printf '\npassed: %s  failed: %s\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
