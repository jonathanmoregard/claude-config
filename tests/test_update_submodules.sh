#!/usr/bin/env bash
# scripts/update-submodules.sh against a master-pinned checkout.
#
# Throwaway repos only: an "upstream" that the submodule tracks, a bare origin
# named like jonathanmoregard/.claude, and a primary clone with the real
# .githooks installed. gh and curl are stubbed; nothing reaches the network.
#
# Invariants: a bump lands as a branch + PR on origin; the primary checkout's
# HEAD, branch, index and tree are exactly as before; no worktree is left
# behind; re-running is a no-op.
#
# Run:  tests/test_update_submodules.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); printf 'PASS %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL %s: %s\n' "$1" "$2" >&2; }

ROOT="$(mktemp -d "${TMPDIR:-/tmp}/dot-claude-submodules.XXXXXX")"
cleanup() { [ -n "$ROOT" ] && [ -d "$ROOT" ] && rm -rf -- "$ROOT"; }
trap cleanup EXIT

export GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.invalid
export GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.invalid
printf '[init]\n\tdefaultBranch = master\n[protocol "file"]\n\tallow = always\n' > "$ROOT/global.gitconfig"
export GIT_CONFIG_GLOBAL="$ROOT/global.gitconfig" GIT_CONFIG_SYSTEM=/dev/null

mkdir -p "$ROOT/fakebin"
# gh stub. `pr create` is recorded (and fails while $ROOT/gh-down exists);
# `pr list --head B` answers from the recorded creates, standing in for GitHub.
cat > "$ROOT/fakebin/gh" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$ROOT/gh-calls"
if [ "\$1 \$2" = "pr list" ]; then
  head=""; while [ \$# -gt 0 ]; do [ "\$1" = --head ] && head="\$2"; shift; done
  grep -c -- "^pr create --head \$head " "$ROOT/gh-creates" 2>/dev/null || true
  exit 0
fi
[ -e "$ROOT/gh-down" ] && { echo "gh: could not create PR (stub outage)" >&2; exit 1; }
printf '%s\n' "\$*" >> "$ROOT/gh-creates"
echo "https://github.com/jonathanmoregard/.claude/pull/999"
EOF
printf '#!/bin/sh\nexit 22\n' > "$ROOT/fakebin/curl"
chmod +x "$ROOT/fakebin/gh" "$ROOT/fakebin/curl"
export PATH="$ROOT/fakebin:$PATH"

# Upstream of the submodule.
UP="$ROOT/upstream/prompt-master"
git init -q -b master "$UP"
git -C "$UP" commit -q --allow-empty -m v1
V1="$(git -C "$UP" rev-parse HEAD)"

# origin = jonathanmoregard/.claude with the policy bundle and a gitlink at v1.
ORIGIN="$ROOT/remote/jonathanmoregard/.claude.git"
mkdir -p "$(dirname "$ORIGIN")"
git init -q --bare -b master "$ORIGIN"
SEED="$ROOT/seed"
git init -q -b master "$SEED"
mkdir -p "$SEED/.githooks" "$SEED/scripts" "$SEED/logs"
cp -a "$REPO_ROOT"/.githooks/. "$SEED/.githooks/"
# `runtime/scripts/` once the layout migration has landed.
SRC_SCRIPTS="$REPO_ROOT/scripts"
[ -d "$REPO_ROOT/runtime/scripts" ] && SRC_SCRIPTS="$REPO_ROOT/runtime/scripts"
cp -a "$SRC_SCRIPTS/install-worktree-policy.sh" "$SRC_SCRIPTS/update-submodules.sh" "$SEED/scripts/"
printf 'logs/\n' > "$SEED/.gitignore"
printf '[submodule "skills/prompt-master"]\n\tpath = skills/prompt-master\n\turl = %s\n' "$UP" > "$SEED/.gitmodules"
git -C "$SEED" add -A
git -C "$SEED" update-index --add --cacheinfo "160000,$V1,skills/prompt-master"
git -C "$SEED" commit -qm seed
git -C "$SEED" remote add origin "$ORIGIN"
git -C "$SEED" push -q origin master

PRIMARY="$ROOT/home/.claude"
git clone -q "$ORIGIN" "$PRIMARY"
"$PRIMARY/scripts/install-worktree-policy.sh" "$PRIMARY" >/dev/null || { echo "install failed"; exit 1; }

export CLAUDE_SUBMODULE_REPO="$PRIMARY" CLAUDE_SUBMODULE_LOG="$ROOT/submodule.log"
export CLAUDE_SUBMODULE_WORKTREE_PARENT="$ROOT"
snapshot() { printf '%s|%s|%s' "$(git -C "$PRIMARY" rev-parse HEAD)" \
  "$(git -C "$PRIMARY" symbolic-ref --short HEAD)" "$(git -C "$PRIMARY" status --porcelain --untracked-files=all)"; }

# --- the pin really is active: the OLD in-place flow is refused ---------------
try_old() { git -C "$PRIMARY" commit -q --allow-empty -m "chore: auto-update" 2>&1; }
if ! try_old >/dev/null; then ok "baseline: an in-place commit in the pinned primary is refused"
else bad "baseline: an in-place commit in the pinned primary is refused" "commit landed"; fi

# --- up to date: nothing happens ---------------------------------------------
BEFORE="$(snapshot)"
OUT="$("$PRIMARY/scripts/update-submodules.sh" 2>&1)"; RC=$?
[ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -q 'Up to date' && [ ! -e "$ROOT/gh-creates" ] \
  && ok "up to date: no branch, no PR" || bad "up to date: no branch, no PR" "rc=$RC $OUT"

# --- upstream moved: a bump PR is proposed, live checkout untouched ----------
git -C "$UP" commit -q --allow-empty -m v2
V2="$(git -C "$UP" rev-parse HEAD)"
BRANCH="chore/auto-update-prompt-master-${V2:0:8}"
OUT="$("$PRIMARY/scripts/update-submodules.sh" 2>&1)"; RC=$?
[ "$RC" -eq 0 ] && ok "bump run exits 0" || bad "bump run exits 0" "rc=$RC $OUT"
PIN="$(git --git-dir="$ORIGIN" ls-tree "refs/heads/$BRANCH" -- skills/prompt-master 2>/dev/null | awk '{print $3}')"
[ "$PIN" = "$V2" ] && ok "origin has $BRANCH pinning the new upstream commit" \
  || bad "origin has the bump branch pinning the new upstream commit" "pin=$PIN $OUT"
[ "$(git --git-dir="$ORIGIN" rev-parse master)" = "$(git -C "$SEED" rev-parse HEAD)" ] \
  && ok "origin master is untouched (the bump goes through a PR)" || bad "origin master is untouched" "moved"
grep -q -- "--head $BRANCH" "$ROOT/gh-creates" 2>/dev/null && ok "a PR is opened for the branch" \
  || bad "a PR is opened for the branch" "$(cat "$ROOT/gh-calls" 2>/dev/null)"
[ "$(snapshot)" = "$BEFORE" ] && ok "live checkout HEAD, branch and tree unchanged" \
  || bad "live checkout HEAD, branch and tree unchanged" "before=$BEFORE after=$(snapshot)"
[ "$(git -C "$PRIMARY" worktree list | wc -l)" -eq 1 ] && ok "no worktree left behind" \
  || bad "no worktree left behind" "$(git -C "$PRIMARY" worktree list)"
git -C "$PRIMARY" show-ref --verify --quiet "refs/heads/$BRANCH" \
  && bad "no local bump branch left behind" "present" || ok "no local bump branch left behind"

# --- idempotent ----------------------------------------------------------------
CALLS="$(wc -l < "$ROOT/gh-creates")"
OUT="$("$PRIMARY/scripts/update-submodules.sh" 2>&1)"; RC=$?
[ "$RC" -eq 0 ] && [ "$(wc -l < "$ROOT/gh-creates")" -eq "$CALLS" ] && printf '%s' "$OUT" | grep -q 'already on origin' \
  && ok "re-run with the bump already on origin is a no-op" || bad "re-run is a no-op" "rc=$RC $OUT"

# --- a run that pushed the branch but could not open the PR is not sticky -----
git -C "$UP" commit -q --allow-empty -m v3
V3="$(git -C "$UP" rev-parse HEAD)"
B3="chore/auto-update-prompt-master-${V3:0:8}"
touch "$ROOT/gh-down"
OUT="$("$PRIMARY/scripts/update-submodules.sh" 2>&1)"; RC=$?
rm -f "$ROOT/gh-down"
[ "$RC" -ne 0 ] && git --git-dir="$ORIGIN" show-ref --verify --quiet "refs/heads/$B3" \
  && ok "gh outage: the run fails loudly with the branch already pushed" \
  || bad "gh outage: the run fails loudly with the branch already pushed" "rc=$RC $OUT"
OUT="$("$PRIMARY/scripts/update-submodules.sh" 2>&1)"; RC=$?
[ "$RC" -eq 0 ] && grep -q -- "^pr create --head $B3 " "$ROOT/gh-creates" \
  && ok "next run opens the missing PR instead of skipping the pushed branch forever" \
  || bad "next run opens the missing PR instead of skipping the pushed branch forever" "rc=$RC $OUT"
[ "$(snapshot)" = "$BEFORE" ] && [ "$(git -C "$PRIMARY" worktree list | wc -l)" -eq 1 ] \
  && ok "after the outage and recovery: live checkout unchanged, no worktree left" \
  || bad "after the outage and recovery: live checkout unchanged, no worktree left" "$(snapshot)"

# --- a refused commit in the throwaway worktree stops the run before any push --
git -C "$UP" commit -q --allow-empty -m v4
V4="$(git -C "$UP" rev-parse HEAD)"
B4="chore/auto-update-prompt-master-${V4:0:8}"
mkdir -p "$ROOT/blocking-hooks"
printf '#!/bin/sh\necho "stub gitleaks: blocked" >&2\nexit 1\n' > "$ROOT/blocking-hooks/pre-commit"
chmod +x "$ROOT/blocking-hooks/pre-commit"
cp "$ROOT/global.gitconfig" "$ROOT/global.gitconfig.bak"
printf '[core]\n\thooksPath = %s\n' "$ROOT/blocking-hooks" >> "$ROOT/global.gitconfig"
OUT="$("$PRIMARY/scripts/update-submodules.sh" 2>&1)"; RC=$?
mv "$ROOT/global.gitconfig.bak" "$ROOT/global.gitconfig"
[ "$RC" -ne 0 ] && ! git --git-dir="$ORIGIN" show-ref --verify --quiet "refs/heads/$B4" \
  && ok "a blocked commit fails the run and pushes nothing" \
  || bad "a blocked commit fails the run and pushes nothing" "rc=$RC $OUT"

printf '\npassed: %s  failed: %s\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
