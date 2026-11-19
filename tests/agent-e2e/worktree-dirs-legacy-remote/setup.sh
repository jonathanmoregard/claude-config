#!/usr/bin/env bash
# A repo renamed with rewritten history: `origin` is the new remote, `legacy`
# the old one, sharing no commits. Three clean worktrees: one merged on
# legacy/master, one ahead of legacy/master, one ahead of origin/master.
# The worktree-additional-dirs SessionStart hook should scope only the two
# with unmerged work.
set -euo pipefail
WORK="${E2E_WORK:?}"; RUN="${E2E_RUN:?}"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid
export GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid

seed() {  # seed <bare> <message>
  git init -q --bare -b master "$1"
  git clone -q "$1" "$RUN/seed"
  git -C "$RUN/seed" commit -q --allow-empty -m "$2"
  git -C "$RUN/seed" push -q origin HEAD:master
  rm -r "$RUN/seed"
}
seed "$RUN/origin.git" "rewritten history"
seed "$RUN/legacy.git" "old history"

rmdir "$WORK"
git clone -q "$RUN/origin.git" "$WORK"
git -C "$WORK" remote add legacy "$RUN/legacy.git"
git -C "$WORK" fetch -q legacy
mkdir "$RUN/wts"
# The hook skips worktrees under a user-level additionalDirectories root, and
# the staged config lists /tmp: drop the roots that cover this run.
python3 - "$HOME/.claude/settings.json" "$RUN" <<'PY'
import json, pathlib, sys
path, run = sys.argv[1], pathlib.Path(sys.argv[2]).resolve()
settings = json.load(open(path, encoding="utf-8"))
perms = settings.setdefault("permissions", {})
perms["additionalDirectories"] = [
    d for d in perms.get("additionalDirectories", [])
    if pathlib.Path(d).expanduser().resolve() not in (run, *run.parents)]
open(path, "w", encoding="utf-8").write(json.dumps(settings, indent=2) + "\n")
PY
git -C "$WORK" worktree add -q -b old-merged "$RUN/wts/wt-legacy-merged" legacy/master
git -C "$WORK" worktree add -q -b old-ahead "$RUN/wts/wt-legacy-ahead" legacy/master
git -C "$RUN/wts/wt-legacy-ahead" commit -q --allow-empty -m "unmerged on legacy"
git -C "$WORK" worktree add -q -b new-ahead "$RUN/wts/wt-origin-ahead" origin/master
git -C "$RUN/wts/wt-origin-ahead" commit -q --allow-empty -m "unmerged on origin"
