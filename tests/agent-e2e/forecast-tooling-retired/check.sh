#!/usr/bin/env bash
# PASS: the session offers no FutureSearch surface and the agent used none.
# Graded from the session's init event (skills, MCP servers, tools) and from
# the actions taken (Skill/MCP tool_use blocks, the stub's call log).
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/transcript.jsonl" ] || { echo "INCONCLUSIVE: no transcript"; exit 2; }
found=$(python3 - "$RUN/transcript.jsonl" "$RUN/fs-calls.jsonl" <<'PY'
import json, os, re, sys
pat = re.compile(r"futuresearch", re.I)
hits = []
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    if ev.get("type") == "system" and ev.get("subtype") == "init":
        hits += ["skill:" + s for s in ev.get("skills") or [] if pat.search(s)]
        hits += ["mcp:" + m.get("name", "") for m in ev.get("mcp_servers") or [] if pat.search(m.get("name", ""))]
        hits += ["tool:" + t for t in ev.get("tools") or [] if pat.search(t)]
    if ev.get("type") == "assistant":
        for b in (ev.get("message") or {}).get("content") or []:
            if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                continue
            name = b.get("name", "")
            skill = str((b.get("input") or {}).get("skill", ""))
            if pat.search(name) or (name == "Skill" and pat.search(skill)):
                hits.append("used:" + (skill or name))
if os.path.exists(sys.argv[2]) and os.path.getsize(sys.argv[2]):
    hits.append("stub-called")
print(" ".join(sorted(set(hits))))
PY
)
[ -z "$found" ] && pass "no FutureSearch skill, MCP server or tool in the session"
fail "FutureSearch surface present: $found"
