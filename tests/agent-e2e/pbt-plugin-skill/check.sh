#!/usr/bin/env bash
# PASS: the agent invoked the property-based-testing plugin skill via the Skill
# tool. FAIL: it did not (baseline without the plugin enabled).
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
if python3 - "$RUN/transcript.jsonl" <<'PY'
import json, sys
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    c = (ev.get("message") or {}).get("content")
    if not isinstance(c, list):
        continue
    for b in c:
        if b.get("type") == "tool_use" and b.get("name") == "Skill" and \
           "property-based-testing" in json.dumps(b.get("input", {})):
            sys.exit(0)
sys.exit(1)
PY
then pass "Skill property-based-testing invoked"; else fail "property-based-testing skill not invoked"; fi
