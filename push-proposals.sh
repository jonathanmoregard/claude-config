#!/usr/bin/env bash
set -euo pipefail
cd ~/.claude

# Symlink-drift guard (round 7): the gate's conformance scan covers the
# legacy rsi content ONLY through the proposals/rsi symlink, while
# SPINE_PATHS below stages recursive-self-improvement/proposals/ directly.
# If install drift removed, retargeted, or replaced that symlink with a real
# dir, the gate would scan nothing in legacy while the push still ships it —
# ungated. Fail closed before the gate even runs.
RSI_LINK="proposals/rsi"
RSI_LEGACY="recursive-self-improvement/proposals"
if [[ ! -L "$RSI_LINK" || ! -d "$RSI_LEGACY" \
      || "$(readlink -f -- "$RSI_LINK" 2>/dev/null)" != "$(readlink -f -- "$RSI_LEGACY" 2>/dev/null)" ]]; then
  echo "error: $RSI_LINK is not a symlink resolving to $RSI_LEGACY — the gate cannot see the legacy rsi content this push would ship." >&2
  echo "Remedy: re-run install.sh (restores the rsi symlink)." >&2
  echo "If $RSI_LINK is a real directory, merge its files into $RSI_LEGACY and remove it first — install.sh never overwrites a hand-curated dir." >&2
  exit 1
fi

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
    echo "Exit 4 can also mean push-blocking files with nonconforming names" >&2
    echo "(listed above): rename them to conform, or move them under" >&2
    echo "proposals/archived/, then rerun the gate." >&2
    exit 1
  elif [[ "$gate_rc" -ne 0 ]]; then
    echo "proposal-intake-gate failed (exit $gate_rc) — push aborted." >&2
    echo "Inspect the output above, fix (or rerun python3 $GATE), then retry." >&2
    exit 1
  fi
fi

# The spine: legacy RSI dir (rsi/ in the unified folder symlinks to it) plus
# the unified proposals folder (gate annotations, gate-config.json, other
# subdirs). gate-log.jsonl (per-host forensics) and .gate.lock (retired lock
# location, pre-2026-08) are local runtime state — excluded so they never
# land in the pushed history. gate-config.json stays versioned deliberately:
# it is user config.
SPINE_PATHS=(
  recursive-self-improvement/proposals/
  proposals/
  ':(exclude)proposals/gate-log.jsonl'
  ':(exclude)proposals/.gate.lock'
)
git add -- "${SPINE_PATHS[@]}"
if git diff --cached --quiet -- "${SPINE_PATHS[@]}"; then
  echo "No proposal changes to commit"
  exit 0
fi
git commit -m "improvement proposals: $(date +%Y-%m-%d)" -- "${SPINE_PATHS[@]}"
git push
