#!/usr/bin/env bash
source "$(dirname "$0")/../_lib/checks.sh" "$1"

# Graded on tool results, not prose: did the Skill call for
# researching-prior-art succeed, and where did the brainstorming link resolve?
verdict=$(python3 - "$RUN/transcript.jsonl" <<'PY'
import json, sys
skill_ids, skill_ok, resolved = set(), False, ""
readlink_ids = set()
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        event = json.loads(line)
    except ValueError:
        continue
    for part in (event.get("message") or {}).get("content") or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "tool_use":
            args = part.get("input") or {}
            if part.get("name") == "Skill" and "researching-prior-art" in str(args.get("skill", "")):
                skill_ids.add(part.get("id"))
            if part.get("name") == "Bash" and "readlink" in str(args.get("command", "")):
                readlink_ids.add(part.get("id"))
        if part.get("type") == "tool_result":
            body = part.get("content")
            text = body if isinstance(body, str) else json.dumps(body)
            if part.get("tool_use_id") in skill_ids and not part.get("is_error"):
                skill_ok = True
            if part.get("tool_use_id") in readlink_ids:
                resolved = text
print(f"{int(skill_ok)}|{'vendor/superpowers' in resolved}|{'Repos/superpowers' in resolved}")
PY
)
IFS='|' read -r skill_ok vendored live <<<"$verdict"
[ "$skill_ok" = 1 ] || fail "researching-prior-art skill did not load"
[ "$vendored" = True ] || fail "brainstorming does not resolve into the pinned vendor copy"
[ "$live" = False ] || fail "brainstorming still resolves into ~/Repos/superpowers"
pass "skills load from the pinned submodule, researching-prior-art available"
