#!/usr/bin/env bash
# A repository with 170 worktrees that all hold work, as a long-lived project
# accumulates, and the list an earlier session's hook wrote for them. The Bash
# sandbox is on: it protects every listed directory separately, and bubblewrap
# takes only so many arguments. The session's own hook run has to shrink the
# list before the first shell command.
set -euo pipefail
work=$1
run=$2
export GIT_AUTHOR_NAME=e2e GIT_AUTHOR_EMAIL=e2e@example.invalid
export GIT_COMMITTER_NAME=e2e GIT_COMMITTER_EMAIL=e2e@example.invalid

git init -q -b main "$work"
mkdir "$work/.claude"
printf '{}\n' >"$work/.claude/settings.json"
cat >"$work/stamp.sh" <<'EOF'
#!/usr/bin/env bash
echo "sandboxed=${SANDBOX_RUNTIME:-0}" >>"$(dirname "$0")/stamp.log"
echo "build stamp 4417"
EOF
chmod +x "$work/stamp.sh"
git -C "$work" add -A
git -C "$work" commit -q -m "Initial commit"

mkdir "$run/wts"
for i in $(seq 1 170); do
  git -C "$work" worktree add -q -b "feat-$i" "$run/wts/feat-$i" main
  printf 'wip\n' >"$run/wts/feat-$i/wip.txt"
done

python3 - "$HOME/.claude/settings.json" "$work/.claude/settings.local.json" "$run" <<'PY'
import json, sys
user, local, run = sys.argv[1:]
settings = json.load(open(user, encoding="utf-8"))
settings.setdefault("sandbox", {})["enabled"] = True
# The worktrees sit under a user-level root wherever the run directory is.
settings.setdefault("permissions", {}).setdefault("additionalDirectories", []).append(run)
open(user, "w", encoding="utf-8").write(json.dumps(settings, indent=2) + "\n")
dirs = [f"{run}/wts/feat-{i}" for i in range(1, 171)]
json.dump({"permissions": {"additionalDirectories": dirs}, "_managedWorktreeDirs": dirs},
          open(local, "w", encoding="utf-8"))
PY
