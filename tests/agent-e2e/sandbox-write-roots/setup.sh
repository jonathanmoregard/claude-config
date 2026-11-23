#!/usr/bin/env bash
# Workspace: probe.sh, which writes one file per target and records each
# outcome. The agent only has to run it; check.sh grades the files on disk.
#
# The arm's own sandbox block is kept and only `enabled` is turned on, so
# the arms differ exactly in the write roots they declare. SANDBOX_ENABLE=0
# (the -off control scenario) turns it off instead.
#
# Every target must be writable by the harness's own bubblewrap, or a
# refusal would be the harness talking rather than Claude's sandbox. The
# harness keeps only the run dir and $E2E_SHARED writable under the real
# HOME, so run with
#   --out under /home/jonathan/worktrees   (the run dir is in that root)
#   E2E_SHARED under /home/jonathan/Repos   (a second root)
# The outside target sits in the XDG runtime dir: writable to the harness,
# under no declared root.
set -euo pipefail

work=$1
run=$2
case $run in
  /home/jonathan/worktrees/*) ;;
  *) echo "run dir must be under /home/jonathan/worktrees (pass --out)" >&2; exit 2 ;;
esac
case ${E2E_SHARED:-} in
  /home/jonathan/Repos/*) ;;
  *) echo "E2E_SHARED must be under /home/jonathan/Repos" >&2; exit 2 ;;
esac

tag="$(basename "$(dirname "$run")")-$(basename "$run")-$(date +%s%N)"
repos_dir=$E2E_SHARED/$tag
outside_dir=/run/user/$(id -u)/e2e-write-roots/$tag
mkdir -p "$repos_dir" "$outside_dir"

cat >"$run/targets" <<EOF
ctl=$work/ctl.txt
worktrees=$run/in-worktrees.txt
repos=$repos_dir/in-repos.txt
later=$run/created-after-wrap/sub/f.txt
outside=$outside_dir/outside.txt
EOF

ident=(-c user.name=e2e -c user.email=e2e@example.invalid)
git init -q "$work/repo"
git -C "$work/repo" "${ident[@]}" commit -q --allow-empty -m init

{
  echo '#!/usr/bin/env bash'
  echo 'while IFS== read -r label path; do'
  echo '  [ "$label" = later ] && mkdir -p "$(dirname "$path")" 2>/dev/null'
  echo '  if err=$( { printf "probe\n" > "$path"; } 2>&1 ); then echo "$label OK"'
  echo '  else echo "$label DENIED ${err##*: }"; fi'
  printf 'done <<'"'"'T'"'"'\n'
  cat "$run/targets"
  echo 'T'
  echo 'if out=$(git -C repo -c user.name=e2e -c user.email=e2e@example.invalid commit -q --allow-empty -m probe 2>&1); then echo "git-commit-in-cwd OK"'
  echo 'else echo "git-commit-in-cwd DENIED ${out##*: }"; fi'
} >"$work/probe.sh"

python3 - "$HOME/.claude/settings.json" "${SANDBOX_ENABLE:-1}" <<'PY'
import json, sys
path, enable = sys.argv[1:3]
settings = json.load(open(path, encoding="utf-8"))
settings.setdefault("sandbox", {})["enabled"] = enable == "1"
# Off means the unsandboxed setup, where a bare Bash allow lets commands run.
if enable != "1" and "Bash" not in settings["permissions"]["allow"]:
    settings["permissions"]["allow"].append("Bash")
open(path, "w", encoding="utf-8").write(json.dumps(settings, indent=2) + "\n")
PY
echo "sandbox_enable=${SANDBOX_ENABLE:-1}" >"$run/mode"
