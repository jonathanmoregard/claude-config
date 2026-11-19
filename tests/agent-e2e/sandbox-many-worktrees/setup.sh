#!/usr/bin/env bash
# A repository with 170 worktrees that all hold work, as a long-lived project
# accumulates. The session-start hook has already run once (an earlier session
# would have), and the Bash sandbox is on: the sandbox protects every listed
# directory separately, and bubblewrap takes only so many arguments.
set -euo pipefail
work=$1
run=$2
export GIT_AUTHOR_NAME=e2e GIT_AUTHOR_EMAIL=e2e@example.invalid
export GIT_COMMITTER_NAME=e2e GIT_COMMITTER_EMAIL=e2e@example.invalid

git init -q -b main "$work"
mkdir "$work/.claude"
printf '{}\n' >"$work/.claude/settings.json"
printf '#!/usr/bin/env bash\necho ran >>"%s/stamp.log"\necho "build stamp 4417"\n' "$run" >"$work/stamp.sh"
chmod +x "$work/stamp.sh"
git -C "$work" add -A
git -C "$work" commit -q -m "Initial commit"

mkdir "$run/wts"
for i in $(seq 1 170); do
  git -C "$work" worktree add -q -b "feat-$i" "$run/wts/feat-$i" main
  printf 'wip\n' >"$run/wts/feat-$i/wip.txt"
done

printf '{}' | CLAUDE_PROJECT_DIR=$work python3 "$HOME/.claude/worktree-additional-dirs.py"

python3 - "$HOME/.claude/settings.json" <<'PY'
import json, sys
path = sys.argv[1]
settings = json.load(open(path, encoding="utf-8"))
settings.setdefault("sandbox", {})["enabled"] = True
open(path, "w", encoding="utf-8").write(json.dumps(settings, indent=2) + "\n")
PY
