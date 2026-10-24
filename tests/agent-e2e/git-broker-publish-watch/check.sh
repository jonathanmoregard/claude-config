#!/usr/bin/env bash
# Control (no broker): the sandboxed agent cannot push, so it reports a failure
# and no branch reaches GitHub. Candidate: the branch and a draft PR exist on
# GitHub, the host watch for that PR finished with exit 0, and the agent
# reported wait-for's summary line for it. The grader then closes the scratch PR
# and deletes its branch, so a run leaves nothing open.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
arm=$(basename "$(dirname "$RUN")")
final=$(tr -d '\r' <"$RUN/final.txt" | grep -E '^BROKER_E2E_' | tail -n 1)
branch=$(cat "$RUN/work/branch.txt")
repo=jonathanmoregard/claude-config
# The grader runs with the harness's throwaway HOME; gh finds the host token
# from the real one.
ghr() { HOME=/home/jonathan gh "$@"; }

pr=$(ghr pr list -R "$repo" --head "$branch" --state all --json number --jq '.[0].number // empty')
cleanup() {
  [[ -n $pr ]] && ghr pr close "$pr" -R "$repo" --delete-branch >/dev/null 2>&1
  ghr api -X DELETE "repos/$repo/git/refs/heads/$branch" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [[ $arm == old ]]; then
  [[ -z $pr ]] || fail "control opened PR #$pr"
  pushed=$(HOME=/home/jonathan git ls-remote "https://github.com/$repo" "refs/heads/$branch")
  [[ -z $pushed ]] || fail "control pushed $branch"
  [[ $final == BROKER_E2E_FAIL* ]] && pass "control could not publish: $final"
  fail "control answered: $final"
fi

[[ $final =~ ^BROKER_E2E_PASS\ ([0-9]+)\ (.*)$ ]] || fail "candidate answered: $final"
said=${BASH_REMATCH[1]}
line=${BASH_REMATCH[2]}
[[ -n $pr ]] || fail "no PR exists for $branch"
[[ $said == "$pr" ]] || fail "agent named PR #$said, GitHub has #$pr"
view=$(ghr pr view "$pr" -R "$repo" --json isDraft,baseRefName,files \
  --jq '"\(.isDraft) \(.baseRefName) \([.files[].path] | join(","))"')
[[ $view == "true master e2e-git-broker-probe.txt" ]] || fail "PR #$pr is '$view'"
status=/run/user/$(id -u)/git-broker/watch/jonathanmoregard/claude-config/pr-$pr.json
[[ -f $status ]] || fail "no host watch status for PR #$pr"
watched=$(jq -r '"\(.state) \(.exit) \(.line)"' "$status")
[[ $watched == "done 0 "* ]] || fail "host watch for PR #$pr: $watched"
[[ $line == *"PR #$pr"* ]] || fail "reported line is not wait-for's: $line"
grep -q 'wait-for pr' "$RUN/commands.txt" || fail "agent never ran wait-for"
pass "branch pushed, draft PR #$pr opened and settled via the host watch: ${watched#done 0 }"
