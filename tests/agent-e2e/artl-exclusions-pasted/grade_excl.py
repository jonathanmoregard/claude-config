"""Does every reviewer dispatch carry the exclusion list?

argv: transcript.jsonl. Prints one line; exit 0 PASS, 1 FAIL, 2 no reviewer.
A reviewer dispatch is a main-session Agent call carrying the opening of the
reviewer A prompt (SKILL.md) or of the reviewer B prompt (hunt-classes.md).
Verifier, fixer and CONSULT dispatches carry neither and are not graded.
Each reviewer prompt must hold "Do not report" and the entry's own phrase.
"""
import json
import sys

REVIEWERS = ("can ship broken", "hunt only the classes listed below")
ENTRY = "live mcp tool list"


def norm(t):
    return " ".join(str(t).split()).lower()


prompts = []
for line in open(sys.argv[1], encoding="utf-8"):
    try:
        ev = json.loads(line)
    except ValueError:
        continue
    if ev.get("parent_tool_use_id") or ev.get("type") != "assistant":
        continue
    c = (ev.get("message") or {}).get("content")
    for b in c if isinstance(c, list) else []:
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Agent":
            p = norm((b.get("input") or {}).get("prompt", ""))
            if any(m in p for m in REVIEWERS):
                prompts.append(p)
if not prompts:
    print("no reviewer dispatched"); sys.exit(2)
for i, p in enumerate(prompts, 1):
    if "do not report" not in p:
        print(f"reviewer {i} of {len(prompts)}: no 'Do not report' list"); sys.exit(1)
    if ENTRY not in p:
        print(f"reviewer {i} of {len(prompts)}: the MCP tool-name entry is missing"); sys.exit(1)
print(f"{len(prompts)} reviewers, each with the exclusion"); sys.exit(0)
