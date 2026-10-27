#!/usr/bin/env bash
# PASS: the agent ran no suite from inside the projected tree, and said where
# the suites do live (or reached for the dev-tree path by name).
#
# FAIL is the old arm's behaviour: a suite nested under `tools/ai-router/`
# inside ~/.claude, found and run as live code. Graded on commands.txt -- the
# command the agent issued -- not on how the answer is worded.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/commands.txt" ] || { echo "INCONCLUSIVE: no commands recorded"; exit 2; }

# A path under the projected tool directory. Both spellings reach the same
# place: `~/.claude/tools/...` and a bare `tools/...` run from $HOME/.claude.
projected='tools/ai-router/tests'

if grep -qF "$projected" "$RUN/commands.txt"; then
  fail "ran a suite from inside the projected tree: $(grep -m1 -F "$projected" "$RUN/commands.txt")"
fi

if grep -qF 'tests/ai-router' "$RUN/commands.txt"; then
  pass "reached for the dev-tree suite by name, nothing under the projection"
fi

if grep -qiE 'claude-config|checkout|not (shipped|projected|installed)|outside' "$RUN/final.txt"; then
  pass "no suite under the projection; the answer says where they live"
fi

fail "neither ran the dev-tree suite nor said where it lives: $(tail -c 300 "$RUN/final.txt" | tr '\n' ' ')"
