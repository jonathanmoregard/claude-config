#!/usr/bin/env bash
# PASS: the advisor loaded the insecure-defaults rubric (Skill tool, or Read of
# its SKILL.md) and the final answer flags the SECRET_KEY fallback. Prints the
# advisor's starting context.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
read -r st sub < <(python3 - "$RUN/transcript.jsonl" "$(dirname "$0")/../_lib" <<'PY'
import json, sys; sys.path.insert(0, sys.argv[2])
from skill_listing import tool_uses, subagent_first_context
uses = list(tool_uses(sys.argv[1], sub=True))
if not uses:
    print("nosub", None); sys.exit()
loaded = any((n == "Skill" and "insecure-defaults" in json.dumps(i)) or
             (n == "Read" and "insecure-defaults/SKILL.md" in json.dumps(i)) for n, i in uses)
print("loaded" if loaded else "notloaded", subagent_first_context(sys.argv[1]))
PY
)
[ "$st" = nosub ] && { echo "INCONCLUSIVE: advisor not dispatched"; exit 2; }
grep -qiE 'SECRET_KEY|dev-secret' "$RUN/final.txt" || fail "secret fallback not reported (advisor start ${sub} tok)"
[ "$st" = loaded ] && pass "rubric loaded, secret flagged, advisor start ${sub} tok"
fail "rubric not loaded (advisor start ${sub} tok)"
