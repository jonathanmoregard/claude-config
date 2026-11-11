#!/usr/bin/env bash
# PASS: every advisor (subagent) API call ran on the pinned claude-fable-5-2.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
models=$(python3 - "$RUN/transcript.jsonl" <<'PY'
import json, sys
seen = set()
for line in open(sys.argv[1], encoding="utf-8"):
    try: e = json.loads(line)
    except ValueError: continue
    if e.get("type") == "assistant" and e.get("parent_tool_use_id"):
        seen.add((e.get("message") or {}).get("model"))
print(",".join(sorted(map(str, seen))))
PY
)
[ -n "$models" ] || { echo "INCONCLUSIVE: advisor not dispatched"; exit 2; }
[ "$models" = claude-fable-5-2 ] && pass "advisor ran on $models"
fail "advisor ran on $models"
