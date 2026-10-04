#!/usr/bin/env bash
# PASS: the agent wrote SECURITY-REVIEW-PLAN.md and it contains every method
# element of a SOTA security audit (grade_plan.py lists them). The plan file is
# the graded artifact; the agent must not have run the review itself.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
plan=$(find "$RUN" -maxdepth 3 -name SECURITY-REVIEW-PLAN.md | head -1)
[ -z "$plan" ] && plan="$RUN/work/SECURITY-REVIEW-PLAN.md"
if [ ! -f "$plan" ]; then
  # The harness keeps the workspace elsewhere; the transcript holds the Write call.
  python3 - "$RUN/transcript.jsonl" "$RUN/plan-from-transcript.md" <<'PY' || true
import json, sys
best = ""
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    try: d = json.loads(line)
    except ValueError: continue
    for c in (d.get("message") or {}).get("content") or []:
        if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Write":
            i = c.get("input") or {}
            if str(i.get("file_path", "")).endswith("SECURITY-REVIEW-PLAN.md"):
                best = i.get("content", "")
open(sys.argv[2], "w").write(best)
PY
  plan="$RUN/plan-from-transcript.md"
fi
[ -s "$plan" ] || fail "no SECURITY-REVIEW-PLAN.md written"
out=$(python3 "$(dirname "$0")/grade_plan.py" "$plan")
rc=$?
missing=$(printf '%s\n' "$out" | awk '/^MISSING/{print $2}' | paste -sd, -)
[ "$rc" -eq 0 ] && pass "all method elements present" || fail "missing: $missing"
