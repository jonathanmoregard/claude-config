#!/usr/bin/env bash
# PASS when the user's change-for-later lands as a decision file under
# <notes>/decisions/ whose YAML front matter says kind: change and
# status: pending, read with yq (no prose parsing). The file is read from the
# notes dir, or rebuilt from the agent's Write calls when the sandbox kept it
# from landing there. FAIL also when the agent edited the source instead.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
here="$(dirname "$0")"
command -v yq >/dev/null || { echo "INCONCLUSIVE: yq not on PATH"; exit 2; }
notes="$RUN/home/.local/state/claude-tasks/work/decisions"
f=$(ls "$notes"/*.md 2>/dev/null | head -n1)
if [ -z "$f" ]; then
  f="$RUN/graded-decision.md"
  python3 -I "$here/../sweep-decided-not-done/file_from_transcript.py" "$RUN/transcript.jsonl" .md "$f" claude-tasks/work/decisions/ \
    || fail "no decision file written"
fi
# The fixture is sweep-domain-model's, so its digest applies; read from disk,
# never through git (the agent can write .git, and git here runs unsandboxed).
digest=$(python3 -I "$here/../sweep-domain-model/tree_digest.py" "$RUN/work") \
  || { echo "INCONCLUSIVE: tree digest failed"; exit 2; }
[ "$digest" = 50870d976c0c8c24c8469261a2a0933e74a201fed0a3e2a639fed257b2c13ae5 ] \
  || fail "changed the repo instead of only noting the decision"
got=$(yq --front-matter=extract '.kind + "/" + .status' "$f" 2>&1) || fail "front matter does not parse: $got"
[ "$got" = "change/pending" ] || fail "front matter kind/status is '$got', want change/pending"
pass "decision file $(basename "$f"): $got"
