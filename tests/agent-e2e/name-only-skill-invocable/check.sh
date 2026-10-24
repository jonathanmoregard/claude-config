#!/usr/bin/env bash
# PASS: the main session invoked caveman:caveman-help via the Skill tool.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
read -r hit main < <(python3 - "$RUN/transcript.jsonl" "$(dirname "$0")/../_lib" <<'PY'
import json, sys; sys.path.insert(0, sys.argv[2])
from skill_listing import tool_uses, main_first_context
hit = any(n == "Skill" and "caveman-help" in json.dumps(i) for n, i in tool_uses(sys.argv[1], sub=False))
print(int(hit), main_first_context(sys.argv[1]))
PY
)
[ "$hit" = 1 ] && pass "caveman-help invoked, main start ${main} tok"
fail "caveman-help not invoked, main start ${main} tok"
