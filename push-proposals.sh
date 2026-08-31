#!/usr/bin/env bash
# Gate the proposals spine, then commit it — IN THE SINK REPO.
#
# Until 2026-08-31 the spine lived inside ~/.claude and this script staged
# `recursive-self-improvement/proposals/` and `proposals/` there. It cannot any
# more, and not merely as a matter of taste: both of those names are now
# symlinks into the sink, and `git add -- <path>` on anything beyond a symbolic
# link is a hard failure ("beyond a symbolic link"). The spine moved to a git
# repo of its own so that a rejected proposal stays findable with `git log -S`
# without every nightly run dirtying the live config checkout the harness
# reads. So this script's job moved with it: gate the sink, commit the sink.
set -euo pipefail

CLAUDE_HOME="$HOME/.claude"
RSI_CONFIG="$CLAUDE_HOME/recursive-self-improvement/config/config.json"
SINK_DEFAULT="$HOME/.local/state/claude-proposals"

# Single source of truth for the sink path: config.json -> proposals.folder,
# falling back to the canonical literal. Resolved DIRECTLY — never by walking
# the ~/.claude compatibility symlinks, which exist for readers only.
resolve_sink() {
  python3 -S -E - "$RSI_CONFIG" "$SINK_DEFAULT" <<'PY' 2>/dev/null || printf '%s\n' "$SINK_DEFAULT"
import json
import os
import sys

fallback = sys.argv[2]
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        value = json.load(fh).get("proposals", {}).get("folder") or fallback
except Exception:                                             # noqa: BLE001
    value = fallback
value = os.path.expanduser(str(value))
print(value if os.path.isabs(value) else fallback)
PY
}
SINK="$(resolve_sink)"

# ------------------------------------------------------------- layout guard --
# The old guard checked that proposals/rsi was a symlink resolving back INTO
# ~/.claude, and told a failing operator to "re-run install.sh". After the
# relocation that advice sends them chasing the wrong thing entirely, so the
# guard now validates the layout that actually has to hold and names the real
# cause when it does not. Fail closed BEFORE the gate runs: a wrong spine means
# the gate scores one tree while the commit ships another.
if [[ ! -d "$SINK" ]]; then
  echo "error: the proposals sink $SINK does not exist." >&2
  echo "Cause: the spine moved out of ~/.claude on 2026-08-31 and this is where it lives now (recursive-self-improvement/config/config.json -> proposals.folder)." >&2
  echo "Remedy: restore or recreate the sink, then run ~/.claude/scripts/cutover-proposals.sh to (re)point the ~/.claude compatibility symlinks at it." >&2
  exit 1
fi
if ! git -C "$SINK" rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "error: the proposals sink $SINK is not a git checkout — nothing can be committed, and rejected proposals would stop being greppable with 'git log -S'." >&2
  echo "Remedy: 'git -C $SINK init' and commit the current contents, or restore the repo from backup." >&2
  exit 1
fi
if [[ ! -d "$SINK/rsi" ]]; then
  echo "error: $SINK/rsi is missing — the RSI reviewer writes there and the gate walks it as a spine subdir." >&2
  echo "Remedy: 'mkdir -p $SINK/rsi' and commit it in the sink." >&2
  exit 1
fi

# The two legacy names are a compatibility shim for READERS. They are allowed
# to be absent (pre-cutover, or a machine that never had them) and allowed to
# be real directories in the window before the cutover runs — but a symlink
# that points somewhere OTHER than the sink is real drift, and silently gating
# one tree while committing another is exactly what this guard exists to stop.
check_shim() {  # <path> <expected target>
  local link="$1" want="$2"
  [[ -e "$link" || -L "$link" ]] || return 0
  if [[ -L "$link" ]]; then
    local got
    got="$(readlink -f -- "$link" 2>/dev/null || true)"
    if [[ "$got" != "$(readlink -f -- "$want" 2>/dev/null || true)" ]]; then
      echo "error: $link is a symlink resolving to '${got:-<broken>}', not to the proposals sink $want." >&2
      echo "Cause: symlink drift. The spine lives in $SINK; readers following this name would see a different tree than the one this script gates and commits." >&2
      echo "Remedy: run ~/.claude/scripts/cutover-proposals.sh — it repoints the shim at the sink and never deletes content." >&2
      return 1
    fi
  else
    echo "note: $link is still a real directory — the cutover has not run yet. Gating and committing $SINK regardless; run ~/.claude/scripts/cutover-proposals.sh to finish the move." >&2
  fi
  return 0
}
check_shim "$CLAUDE_HOME/proposals" "$SINK" || exit 1
check_shim "$CLAUDE_HOME/recursive-self-improvement/proposals" "$SINK/rsi" || exit 1

# ------------------------------------------------------------------- gate ----
# Intake gate first: every ungated pending proposal gets an independent
# scorer verdict (sharp|duplicate|rot + cited evidence) annotated into its
# frontmatter before anything ships for human review. Nonzero gate exit
# means the batch is in an unknown state — do not commit it half-gated.
GATE="$CLAUDE_HOME/scripts/proposal-intake-gate.py"
if [[ ! -f "$GATE" ]]; then
  if [[ "${PROPOSAL_GATE_ALLOW_MISSING:-}" == "1" ]]; then
    echo "warning: $GATE missing — PROPOSAL_GATE_ALLOW_MISSING=1 set, committing WITHOUT the intake gate" >&2
  else
    echo "error: $GATE is not installed — refusing to ship ungated proposals." >&2
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
    echo "$SINK/archived/, then rerun the gate." >&2
    exit 1
  elif [[ "$gate_rc" -ne 0 ]]; then
    echo "proposal-intake-gate failed (exit $gate_rc) — commit aborted." >&2
    echo "Inspect the output above, fix (or rerun python3 $GATE), then retry." >&2
    exit 1
  fi
fi

# ------------------------------------------------------- stage, commit, push --
# The whole sink is the spine: it is a proposals repo, not a config repo that
# happens to hold proposals, so everything in it is content. Its own .gitignore
# keeps the runtime lock out. `git add -A` from the sink root stages additions,
# edits (gate annotations, drained `status:` flips) and removals alike.
git -C "$SINK" add -A
if git -C "$SINK" diff --cached --quiet; then
  echo "No proposal changes to commit"
  exit 0
fi
git -C "$SINK" commit -q -m "improvement proposals: $(date +%Y-%m-%d)"
echo "committed proposal changes in $SINK"

# Pushing is conditional ON PURPOSE. These are private RSI proposals; the sink
# has no remote today, and creating one is a decision about where Jonathan's
# proposals get uploaded — not this script's to make. A missing remote is the
# expected steady state, so it prints a notice and exits 0. It must never fail
# closed here: failing would abort the nightly reviewer after it had already
# written and gated its work.
if [[ -n "$(git -C "$SINK" remote)" ]]; then
  git -C "$SINK" push
else
  echo "note: the proposals sink $SINK has no git remote — committed locally, nothing pushed."
fi
