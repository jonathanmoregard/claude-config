#!/usr/bin/env bash
set -euo pipefail
cd ~/.claude

# Intake gate first: every ungated pending proposal gets an independent
# scorer verdict (sharp|duplicate|rot + cited evidence) annotated into its
# frontmatter before anything ships for human review. Nonzero gate exit
# means the batch is in an unknown state — do not push it half-gated.
GATE="$HOME/.claude/scripts/proposal-intake-gate.py"
if [[ ! -f "$GATE" ]]; then
  if [[ "${PROPOSAL_GATE_ALLOW_MISSING:-}" == "1" ]]; then
    echo "warning: $GATE missing — PROPOSAL_GATE_ALLOW_MISSING=1 set, pushing WITHOUT the intake gate" >&2
  else
    echo "error: $GATE is not installed — refusing to push ungated proposals." >&2
    echo "Install it via the recursive-self-improvement plugin's scripts/install.sh." >&2
    echo "Emergency bypass (documented in the plugin README): PROPOSAL_GATE_ALLOW_MISSING=1" >&2
    exit 1
  fi
else
  gate_rc=0
  python3 "$GATE" || gate_rc=$?
  if [[ "$gate_rc" -eq 4 ]]; then
    echo "proposal-intake-gate: partial batch — rerun the gate; do not bypass." >&2
    exit 1
  elif [[ "$gate_rc" -ne 0 ]]; then
    echo "proposal-intake-gate failed (exit $gate_rc) — push aborted." >&2
    echo "Inspect the output above, fix (or rerun python3 $GATE), then retry." >&2
    exit 1
  fi
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
