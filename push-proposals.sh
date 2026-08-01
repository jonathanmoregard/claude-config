#!/usr/bin/env bash
set -euo pipefail
cd ~/.claude

# Intake gate first: every ungated pending proposal gets an independent
# scorer verdict (sharp|duplicate|rot + cited evidence) annotated into its
# frontmatter before anything ships for human review. Nonzero gate exit
# means the batch is in an unknown state — do not push it half-gated.
GATE="$HOME/.claude/scripts/proposal-intake-gate.py"
if [[ -f "$GATE" ]]; then
  if ! python3 "$GATE"; then
    echo "proposal-intake-gate failed — push aborted." >&2
    echo "Inspect the output above, fix (or rerun python3 $GATE), then retry." >&2
    exit 1
  fi
else
  echo "warning: $GATE not installed — pushing ungated proposals" >&2
fi

# The spine: legacy RSI dir (rsi/ in the unified folder symlinks to it) plus
# the unified proposals folder (gate annotations, gate-log.jsonl, other subdirs).
git add recursive-self-improvement/proposals/ proposals/
if git diff --cached --quiet -- recursive-self-improvement/proposals/ proposals/; then
  echo "No proposal changes to commit"
  exit 0
fi
git commit -m "improvement proposals: $(date +%Y-%m-%d)" -- recursive-self-improvement/proposals/ proposals/
git push
