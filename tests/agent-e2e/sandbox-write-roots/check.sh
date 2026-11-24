#!/usr/bin/env bash
# Grades from the files on disk, never from what the agent reports.
#
# Sandbox on: the writes inside the declared roots must land and the one
# outside every root must not. Sandbox off (the -off control): the outside
# write must land, which proves the harness itself permits that target, so
# its absence in the sandboxed scenario is the sandbox's doing.
set -uo pipefail

run=$1
targets=$run/targets
[ -f "$targets" ] || { echo "no targets file: setup did not run"; exit 2; }

declare -A path
while IFS== read -r label p; do path[$label]=$p; done <"$targets"

# Read once, before the cleanup below deletes the outside target.
declare -A seen
for label in "${!path[@]}"; do
  [ -e "${path[$label]}" ] && seen[$label]=written || seen[$label]=absent
done
state() { echo "${seen[$1]}"; }

commits=$(git -C "$run/work/repo" rev-list --count HEAD 2>/dev/null || echo 0)
git_state=$([ "$commits" -ge 2 ] && echo written || echo absent)
summary="ctl=$(state ctl) worktrees=$(state worktrees) repos=$(state repos)"
summary+=" later=$(state later) outside=$(state outside) git-commit-in-cwd=$git_state"

rm -rf "$(dirname "${path[outside]}")"

if [ "$(state ctl)" != written ]; then
  echo "INCONCLUSIVE: the control write in the cwd is missing, so probe.sh never ran: $summary"
  exit 2
fi

if grep -qx 'sandbox_enable=0' "$run/mode"; then
  if [ "$(state outside)" = written ]; then
    echo "PASS: sandbox off, nothing is enforced, outside target is writable: $summary"
    exit 0
  fi
  echo "FAIL: sandbox off yet the outside write was refused, so the harness blocks it: $summary"
  exit 1
fi

fail=""
for label in worktrees repos later; do
  [ "$(state "$label")" = written ] || fail+=" $label-refused"
done
[ "$(state outside)" = absent ] || fail+=" outside-written"
if [ -n "$fail" ]; then
  echo "FAIL:$fail: $summary"
  exit 1
fi
echo "PASS: $summary"
