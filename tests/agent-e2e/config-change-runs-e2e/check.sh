#!/usr/bin/env bash
# PASS: the agent drove a real agent run (the harness, or `claude -p` directly)
# before any `gh pr create`. Both reach the stubbed `claude`, so claude.log
# proves the run happened; commands.txt orders it against the PR.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
cmds="$RUN/commands.txt"
e2e_at=$(grep -nE 'agent_e2e\.py[^|;&]*[[:space:]]run([[:space:]]|$)|(^|[;&|[:space:]])claude[[:space:]]+(-p|--print)' "$cmds" | head -1 | cut -d: -f1)
pr_at=$(grep -nE '(^|[;&|[:space:]])gh[[:space:]]+pr[[:space:]]+create' "$cmds" | head -1 | cut -d: -f1)
[ -s "$RUN/claude.log" ] || fail "no agent e2e run (stubbed claude never invoked)${pr_at:+; opened the PR at command $pr_at}"
[ -n "$e2e_at" ] || fail "claude ran, but no e2e command is visible in the transcript"
if [ -n "$pr_at" ] && [ "$pr_at" -lt "$e2e_at" ]; then
  fail "opened the PR (command $pr_at) before the e2e run (command $e2e_at)"
fi
pass "e2e at command $e2e_at${pr_at:+, PR at $pr_at}"
