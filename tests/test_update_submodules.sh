#!/usr/bin/env bash
# scripts/update-submodules.sh against a master-pinned checkout.
#
# Throwaway repos only: a "fork" whose files get vendored, a bare origin named
# like jonathanmoregard/.claude, and a primary clone with the real .githooks
# installed. gh and curl are stubbed; nothing reaches the network.
#
# Invariants: an update lands as a branch + PR on origin carrying the fork's
# FILES at its tracked paths and the new vendored_commit; paths we do not track
# are not vendored; a file deleted upstream disappears here too; a whole
# tracked path vanishing fails the run instead of being vendored away; the
# primary checkout's HEAD, branch, index and tree are exactly as before; no
# worktree is left behind; re-running is a no-op.
#
# Run:  tests/test_update_submodules.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); printf 'PASS %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL %s: %s\n' "$1" "$2" >&2; }

ROOT="$(mktemp -d "${TMPDIR:-/tmp}/dot-claude-vendor.XXXXXX")"
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

# The fork the vendored copy comes from. `.github/` is deliberately present and
# deliberately NOT tracked: vendoring is the tracked_paths subset, not a mirror.
FORK="$ROOT/fork/sp"
TRACKED=(LICENSE hooks skills/brainstorming)
git init -q -b main "$FORK"
mkdir -p "$FORK/skills/brainstorming" "$FORK/hooks" "$FORK/.github/workflows"
printf 'MIT\n' > "$FORK/LICENSE"
printf 'hook v1\n' > "$FORK/hooks/session-start"
chmod +x "$FORK/hooks/session-start"
printf 'v1\n' > "$FORK/skills/brainstorming/SKILL.md"
printf 'retired later\n' > "$FORK/skills/brainstorming/extra.md"
printf 'name: ci\n' > "$FORK/.github/workflows/ci.yml"
# Under a tracked path and excluded anyway: the fork's own test tree, which
# tests/enforced refuses to let the projection carry into ~/.claude.
mkdir -p "$FORK/skills/brainstorming/tests"
printf 'def test_x(): pass\n' > "$FORK/skills/brainstorming/tests/test_x.py"
git -C "$FORK" add -A
git -C "$FORK" commit -qm v1
V1="$(git -C "$FORK" rev-parse HEAD)"

# origin = jonathanmoregard/.claude with the policy bundle, the config and the
# vendored copy at V1.
ORIGIN="$ROOT/remote/jonathanmoregard/.claude.git"
mkdir -p "$(dirname "$ORIGIN")"
git init -q --bare -b master "$ORIGIN"
SEED="$ROOT/seed"
git init -q -b master "$SEED"
mkdir -p "$SEED/.githooks" "$SEED/runtime/scripts" "$SEED/runtime/vendor/sp" "$SEED/logs"
cp -a "$REPO_ROOT"/.githooks/. "$SEED/.githooks/"
# `runtime/scripts/` once the layout migration has landed.
SRC_SCRIPTS="$REPO_ROOT/scripts"
[ -d "$REPO_ROOT/runtime/scripts" ] && SRC_SCRIPTS="$REPO_ROOT/runtime/scripts"
# config-repo.sh is sourced by update-submodules.sh (as `$script_dir/config-repo.sh`),
# a dependency it acquired in the layout cutover: the repository checkout can no
# longer be assumed to sit at a fixed place relative to the script, so resolving
# it moved into a shared sibling. Staging the two scripts without it left seven
# cases failing with `config-repo.sh: No such file or directory` -- the script
# under test dying on line 26 before reaching any behaviour, which reads as seven
# unrelated assertion failures rather than one missing fixture file.
cp -a "$SRC_SCRIPTS/install-worktree-policy.sh" "$SRC_SCRIPTS/update-submodules.sh" \
      "$SRC_SCRIPTS/config-repo.sh" "$SEED/runtime/scripts/"
printf 'logs/\n' > "$SEED/.gitignore"
python3 - "$SEED/runtime/upstream-drift.json" "$FORK" "$V1" <<'PY'
import json, sys

path, fork, sha = sys.argv[1:]
with open(path, "w", encoding="utf-8") as fh:
    json.dump({"entries": [{
        "name": "sp",
        "fork_repo": fork,
        "fork_branch": "main",
        "upstream_url": fork,
        "upstream_branch": "main",
        "vendor_path": "runtime/vendor/sp",
        "vendored_commit": sha,
        "tracked_paths": ["LICENSE", "hooks", "skills/brainstorming"],
        "excluded_paths": ["skills/brainstorming/tests"],
        "customized_paths": [],
    }]}, fh, indent=2)
    fh.write("\n")
PY
git -C "$FORK" archive --format=tar HEAD -- "${TRACKED[@]}" | tar -x -C "$SEED/runtime/vendor/sp"
rm -r -- "$SEED/runtime/vendor/sp/skills/brainstorming/tests"
git -C "$SEED" add -A
git -C "$SEED" commit -qm seed
git -C "$SEED" remote add origin "$ORIGIN"
git -C "$SEED" push -q origin master

PRIMARY="$ROOT/home/.claude"
git clone -q "$ORIGIN" "$PRIMARY"
"$PRIMARY/runtime/scripts/install-worktree-policy.sh" "$PRIMARY" >/dev/null || { echo "install failed"; exit 1; }
UPDATER="$PRIMARY/runtime/scripts/update-submodules.sh"

export CLAUDE_SUBMODULE_REPO="$PRIMARY" CLAUDE_SUBMODULE_LOG="$ROOT/submodule.log"
export CLAUDE_SUBMODULE_WORKTREE_PARENT="$ROOT"
snapshot() { printf '%s|%s|%s' "$(git -C "$PRIMARY" rev-parse HEAD)" \
  "$(git -C "$PRIMARY" symbolic-ref --short HEAD)" "$(git -C "$PRIMARY" status --porcelain --untracked-files=all)"; }
on_branch() { git --git-dir="$ORIGIN" show "refs/heads/$1:$2" 2>/dev/null; }
mode_on_branch() { git --git-dir="$ORIGIN" ls-tree "refs/heads/$1" -- "$2" 2>/dev/null | awk '{print $1}'; }
tree_on_branch() { git --git-dir="$ORIGIN" ls-tree -r --name-only "refs/heads/$1" -- "$2" 2>/dev/null; }

# --- the pin really is active: the OLD in-place flow is refused ---------------
try_old() { git -C "$PRIMARY" commit -q --allow-empty -m "chore: auto-update" 2>&1; }
if ! try_old >/dev/null; then ok "baseline: an in-place commit in the pinned primary is refused"
else bad "baseline: an in-place commit in the pinned primary is refused" "commit landed"; fi

# --- up to date: nothing happens ---------------------------------------------
BEFORE="$(snapshot)"
OUT="$("$UPDATER" 2>&1)"; RC=$?
[ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -q 'Up to date' && [ ! -e "$ROOT/gh-creates" ] \
  && ok "up to date: no branch, no PR" || bad "up to date: no branch, no PR" "rc=$RC $OUT"

# --- the fork moved: a sync PR is proposed, live checkout untouched ----------
printf 'v2\n' > "$FORK/skills/brainstorming/SKILL.md"
printf 'hook v2\n' > "$FORK/hooks/session-start"
printf 'name: ci\nchanged: true\n' > "$FORK/.github/workflows/ci.yml"
git -C "$FORK" rm -q "$FORK/skills/brainstorming/extra.md"
git -C "$FORK" add -A
git -C "$FORK" commit -qm v2
V2="$(git -C "$FORK" rev-parse HEAD)"
BRANCH="chore/auto-update-sp-${V2:0:8}"
OUT="$("$UPDATER" 2>&1)"; RC=$?
[ "$RC" -eq 0 ] && ok "sync run exits 0" || bad "sync run exits 0" "rc=$RC $OUT"

[ "$(on_branch "$BRANCH" runtime/vendor/sp/skills/brainstorming/SKILL.md)" = v2 ] \
  && ok "origin has $BRANCH carrying the fork's new file contents" \
  || bad "origin has the branch carrying the fork's new file contents" \
    "got=$(on_branch "$BRANCH" runtime/vendor/sp/skills/brainstorming/SKILL.md) $OUT"
RECORDED="$(on_branch "$BRANCH" runtime/upstream-drift.json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["entries"][0]["vendored_commit"])' 2>/dev/null)"
[ "$RECORDED" = "$V2" ] && ok "the branch records the synced commit in upstream-drift.json" \
  || bad "the branch records the synced commit in upstream-drift.json" "recorded=$RECORDED want=$V2"
[ -z "$(tree_on_branch "$BRANCH" runtime/vendor/sp/.github)" ] \
  && ok "a path we do not track is not vendored" \
  || bad "a path we do not track is not vendored" "$(tree_on_branch "$BRANCH" runtime/vendor/sp/.github)"
[ -z "$(tree_on_branch "$BRANCH" runtime/vendor/sp/skills/brainstorming/tests)" ] \
  && ok "an excluded path under a tracked one is not vendored" \
  || bad "an excluded path under a tracked one is not vendored" \
    "$(tree_on_branch "$BRANCH" runtime/vendor/sp/skills/brainstorming/tests)"
[ -z "$(on_branch "$BRANCH" runtime/vendor/sp/skills/brainstorming/extra.md)" ] \
  && ok "a file deleted in the fork disappears from the vendored copy" \
  || bad "a file deleted in the fork disappears from the vendored copy" "still present"
[ "$(mode_on_branch "$BRANCH" runtime/vendor/sp/hooks/session-start)" = 100755 ] \
  && ok "the executable bit survives the sync" \
  || bad "the executable bit survives the sync" "mode=$(mode_on_branch "$BRANCH" runtime/vendor/sp/hooks/session-start)"
[ "$(git --git-dir="$ORIGIN" rev-parse master)" = "$(git -C "$SEED" rev-parse HEAD)" ] \
  && ok "origin master is untouched (the sync goes through a PR)" || bad "origin master is untouched" "moved"
grep -q -- "--head $BRANCH" "$ROOT/gh-creates" 2>/dev/null && ok "a PR is opened for the branch" \
  || bad "a PR is opened for the branch" "$(cat "$ROOT/gh-calls" 2>/dev/null)"
[ "$(snapshot)" = "$BEFORE" ] && ok "live checkout HEAD, branch and tree unchanged" \
  || bad "live checkout HEAD, branch and tree unchanged" "before=$BEFORE after=$(snapshot)"
[ "$(git -C "$PRIMARY" worktree list | wc -l)" -eq 1 ] && ok "no worktree left behind" \
  || bad "no worktree left behind" "$(git -C "$PRIMARY" worktree list)"
git -C "$PRIMARY" show-ref --verify --quiet "refs/heads/$BRANCH" \
  && bad "no local sync branch left behind" "present" || ok "no local sync branch left behind"
[ -z "$(ls -d "$ROOT"/claude-vendor-fetch.* 2>/dev/null)" ] && ok "no fetch cache left behind" \
  || bad "no fetch cache left behind" "$(ls -d "$ROOT"/claude-vendor-fetch.* 2>&1)"

# --- idempotent ----------------------------------------------------------------
CALLS="$(wc -l < "$ROOT/gh-creates")"
OUT="$("$UPDATER" 2>&1)"; RC=$?
[ "$RC" -eq 0 ] && [ "$(wc -l < "$ROOT/gh-creates")" -eq "$CALLS" ] && printf '%s' "$OUT" | grep -q 'already on origin' \
  && ok "re-run with the sync already on origin is a no-op" || bad "re-run is a no-op" "rc=$RC $OUT"

# --- a run that pushed the branch but could not open the PR is not sticky -----
printf 'v3\n' > "$FORK/skills/brainstorming/SKILL.md"
git -C "$FORK" commit -qam v3
V3="$(git -C "$FORK" rev-parse HEAD)"
B3="chore/auto-update-sp-${V3:0:8}"
touch "$ROOT/gh-down"
OUT="$("$UPDATER" 2>&1)"; RC=$?
rm -f "$ROOT/gh-down"
[ "$RC" -ne 0 ] && git --git-dir="$ORIGIN" show-ref --verify --quiet "refs/heads/$B3" \
  && ok "gh outage: the run fails loudly with the branch already pushed" \
  || bad "gh outage: the run fails loudly with the branch already pushed" "rc=$RC $OUT"
OUT="$("$UPDATER" 2>&1)"; RC=$?
[ "$RC" -eq 0 ] && grep -q -- "^pr create --head $B3 " "$ROOT/gh-creates" \
  && ok "next run opens the missing PR instead of skipping the pushed branch forever" \
  || bad "next run opens the missing PR instead of skipping the pushed branch forever" "rc=$RC $OUT"
[ "$(snapshot)" = "$BEFORE" ] && [ "$(git -C "$PRIMARY" worktree list | wc -l)" -eq 1 ] \
  && ok "after the outage and recovery: live checkout unchanged, no worktree left" \
  || bad "after the outage and recovery: live checkout unchanged, no worktree left" "$(snapshot)"

# --- a refused commit in the throwaway worktree stops the run before any push --
printf 'v4\n' > "$FORK/skills/brainstorming/SKILL.md"
git -C "$FORK" commit -qam v4
V4="$(git -C "$FORK" rev-parse HEAD)"
B4="chore/auto-update-sp-${V4:0:8}"
mkdir -p "$ROOT/blocking-hooks"
printf '#!/bin/sh\necho "stub gitleaks: blocked" >&2\nexit 1\n' > "$ROOT/blocking-hooks/pre-commit"
chmod +x "$ROOT/blocking-hooks/pre-commit"
cp "$ROOT/global.gitconfig" "$ROOT/global.gitconfig.bak"
printf '[core]\n\thooksPath = %s\n' "$ROOT/blocking-hooks" >> "$ROOT/global.gitconfig"
OUT="$("$UPDATER" 2>&1)"; RC=$?
mv "$ROOT/global.gitconfig.bak" "$ROOT/global.gitconfig"
[ "$RC" -ne 0 ] && ! git --git-dir="$ORIGIN" show-ref --verify --quiet "refs/heads/$B4" \
  && ok "a blocked commit fails the run and pushes nothing" \
  || bad "a blocked commit fails the run and pushes nothing" "rc=$RC $OUT"

# --- a tracked path gone from the fork is a thing to look at ------------------
# Vendoring it away would silently drop a skill ~/.claude links to, so the run
# fails with nothing pushed and the next run fails again until someone looks.
git -C "$FORK" rm -rq hooks
git -C "$FORK" commit -qm v5
V5="$(git -C "$FORK" rev-parse HEAD)"
B5="chore/auto-update-sp-${V5:0:8}"
OUT="$("$UPDATER" 2>&1)"; RC=$?
[ "$RC" -ne 0 ] && ! git --git-dir="$ORIGIN" show-ref --verify --quiet "refs/heads/$B5" \
  && ok "a tracked path missing from the fork fails the run and pushes nothing" \
  || bad "a tracked path missing from the fork fails the run and pushes nothing" "rc=$RC $OUT"

printf '\npassed: %s  failed: %s\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
