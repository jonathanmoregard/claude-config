#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"

[ "$(cat "$RUN/work/src/message.txt" 2>/dev/null)" = "hello world" ] \
  || fail "worker did not change the greeting to hello world"

routes="$RUN/home/.local/state/ai-router/native-agent-routes.jsonl"
[ -s "$routes" ] || fail "resolved-model observer wrote no route records"

python3 - "$routes" <<'PY' || fail "resolved models did not match the configured roles"
import json
import sys

rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]
seen = {(row.get("agent_type"), row.get("resolved_model"), row.get("mismatch")) for row in rows}
required = {
    ("Explore", "claude-haiku-4-5-20251001", False),
    ("worker-easy", "claude-sonnet-5-5", False),
    ("worker-hard", "claude-opus-5-5", False),
    ("advisor", "claude-fable-5-1", False),
}
raise SystemExit(0 if required <= seen else 1)
PY

pass "all four native roles resolved to their pinned model families"
