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
cat > "$ROOT/fakebin/gh" <<EOF
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$ROOT/gh-calls"
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
cp -a "$REPO_ROOT/scripts/install-worktree-policy.sh" "$REPO_ROOT/scripts/update-submodules.sh" "$SEED/scripts/"
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
[ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -q 'Up to date' && [ ! -e "$ROOT/gh-calls" ] \
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
grep -q -- "--head $BRANCH" "$ROOT/gh-calls" 2>/dev/null && ok "a PR is opened for the branch" \
  || bad "a PR is opened for the branch" "$(cat "$ROOT/gh-calls" 2>/dev/null)"
[ "$(snapshot)" = "$BEFORE" ] && ok "live checkout HEAD, branch and tree unchanged" \
  || bad "live checkout HEAD, branch and tree unchanged" "before=$BEFORE after=$(snapshot)"
[ "$(git -C "$PRIMARY" worktree list | wc -l)" -eq 1 ] && ok "no worktree left behind" \
  || bad "no worktree left behind" "$(git -C "$PRIMARY" worktree list)"
git -C "$PRIMARY" show-ref --verify --quiet "refs/heads/$BRANCH" \
  && bad "no local bump branch left behind" "present" || ok "no local bump branch left behind"

# --- idempotent ----------------------------------------------------------------
CALLS="$(wc -l < "$ROOT/gh-calls")"
OUT="$("$PRIMARY/scripts/update-submodules.sh" 2>&1)"; RC=$?
[ "$RC" -eq 0 ] && [ "$(wc -l < "$ROOT/gh-calls")" -eq "$CALLS" ] && printf '%s' "$OUT" | grep -q 'already on origin' \
  && ok "re-run with the bump already on origin is a no-op" || bad "re-run is a no-op" "rc=$RC $OUT"

printf '\npassed: %s  failed: %s\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
