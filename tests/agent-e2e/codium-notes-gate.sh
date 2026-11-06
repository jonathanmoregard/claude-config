#!/usr/bin/env bash
# Agent e2e for the Codium-only notes injection, driven by hand rather than by
# agent_e2e.py's batch runner.
#
# WHY NOT A SCENARIO DIRECTORY. The hook fires only when
# CLAUDE_CODE_ENTRYPOINT=claude-vscode, and run_one() in agent_e2e.py pops that
# variable out of the agent's environment unconditionally (so that the shared
# `= "cli"` gate in settings.json behaves as it does in a real terminal). A
# scenario's setup.sh builds the workspace, not the agent's env, so there is no
# way to ask for the Codium entrypoint from inside the contract. Rather than
# widen the shared harness for one hook, this script uses its `stage` command --
# the same staging the batch runner uses -- and drives `claude -p` directly.
#
# What it gives up versus the batch runner: no bubblewrap isolation, n=1 per
# cell, and no resumable verdicts. What it keeps: a real headless agent on the
# real staged candidate config, hitting the real hook.
#
# Usage: codium-notes-gate.sh <new-ref> [out-dir]
set -uo pipefail

ref=${1:?usage: codium-notes-gate.sh <new-ref> [out-dir]}
out=${2:-/home/jonathan/worktrees/e2e-codium-stage}
here=$(cd "$(dirname "$0")/../.." && pwd)
harness=$here/runtime/scripts/agent-e2e/agent_e2e.py

# The sentinel. A real setting would be guessable from general knowledge; this
# path exists nowhere the agent can reach except the notes file, so naming it
# proves the file reached the model's context.
notes='# Claude-in-Codium notes

## What works in Codium

- Screenshots for a UI review go in `~/Pictures/codium-review/`, named
  `shot-<n>.png`. The review panel picks them up from there in order.

## What does not

- Nothing recorded yet.
'
prompt='Where should I put screenshots for a UI review?'

stage_arm() {  # stage_arm <name> <config-ref>
  local home=$out/$1
  rm -rf "$home"
  python3 "$harness" stage --config "$2" --home "$home" >/dev/null || return 1
  mkdir -p "$home/.local/state/claude-drift"
  printf '%s' "$notes" >"$home/.local/state/claude-drift/codium.md"
}

cell() {  # cell <label> <arm> <entrypoint> <expect: yes|no>
  local label=$1 arm=$2 ep=$3 expect=$4
  local home=$out/$arm log=$out/$label.txt
  HOME=$home CLAUDE_CODE_ENTRYPOINT=$ep \
    claude -p "$prompt" --strict-mcp-config --no-session-persistence \
    >"$log" 2>"$out/$label.err"
  local got=no
  grep -qi 'codium-review' "$log" && got=yes
  if [ "$got" = "$expect" ]; then
    printf '%-22s %-14s names sentinel: %-3s expected: %-3s PASS\n' \
      "$label" "$ep" "$got" "$expect"
  else
    printf '%-22s %-14s names sentinel: %-3s expected: %-3s FAIL\n' \
      "$label" "$ep" "$got" "$expect"
    return 1
  fi
}

mkdir -p "$out"
stage_arm new "$ref"        || { echo "staging new failed" >&2; exit 2; }
stage_arm old origin/master || { echo "staging old failed" >&2; exit 2; }

rc=0
cell new-codium new "claude-vscode" yes || rc=1   # the hook fires
cell new-cli    new "cli"           no  || rc=1   # gate holds in a terminal
cell old-codium old "claude-vscode" no  || rc=1   # control: no hook, no notes
echo "transcripts under $out"
exit $rc
