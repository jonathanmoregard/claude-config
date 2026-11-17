#!/usr/bin/env python3
"""Grade whether the permission layer let a tool call through.

usage: perm_grade.py TRANSCRIPT TOOLS PATH_SUBSTRING

TOOLS is a comma-separated list of tool names. Prints one word:
  none     no matching call was made
  denied   every matching call came back a permission refusal
  allowed  at least one matching call got past the permission layer

A filesystem error (read-only sandbox mount, missing file) counts as allowed:
the permission layer already said yes before the tool touched the disk.
"""
import json
import re
import sys

transcript, tools, needle = sys.argv[1], set(sys.argv[2].split(",")), sys.argv[3]
REFUSAL = re.compile(r"permission|haven't granted|not allowed|denied", re.I)

calls, outcomes = {}, []
for line in open(transcript, encoding="utf-8"):
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    msg = ev.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        continue
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_use" and b.get("name") in tools:
            if needle in json.dumps(b.get("input") or {}):
                calls[b.get("id")] = True
        elif b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
            body = b.get("content")
            text = body if isinstance(body, str) else json.dumps(body)
            outcomes.append(bool(b.get("is_error")) and bool(REFUSAL.search(text)))

if not outcomes:
    print("none")
elif all(outcomes):
    print("denied")
else:
    print("allowed")
