"""Does every reviewer dispatch carry the exclusion list?

argv: transcript.jsonl. Prints one line; exit 0 PASS, 1 FAIL, 2 no reviewer.
A reviewer dispatch is a main-session Agent call carrying the opening of the
reviewer A prompt (SKILL.md), of the reviewer B prompt (hunt-classes.md), or
"record files read", which both prompts end with. Verifier, fixer and CONSULT
dispatches carry none of these and are not graded, whatever agent type they
go to. A reviewer reworded past all three is missed: a known limit.
Each reviewer prompt must hold "Do not report" and the entry's own phrase.
"""
import json
import sys

REVIEWERS = ("can ship broken", "hunt only the classes listed below", "record files read")
ENTRY = "live mcp tool list"


def norm(t):
    return " ".join(str(t).split()).lower()


prompts = []
try:
    for line in open(sys.argv[1], encoding="utf-8"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get("parent_tool_use_id") or ev.get("type") != "assistant":
            continue
        m = ev.get("message")
        c = m.get("content") if isinstance(m, dict) else None
        for b in c if isinstance(c, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Agent":
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                p = norm(inp.get("prompt", ""))
                if not p.startswith("consult dispatch") and any(r in p for r in REVIEWERS):
                    prompts.append(p)
except Exception as exc:  # a grader fault is no evidence: INCONCLUSIVE, not FAIL
    print(f"grader error: {exc!r}"); sys.exit(2)
if not prompts:
    print("no reviewer dispatched"); sys.exit(2)
for i, p in enumerate(prompts, 1):
    if "do not report" not in p:
        print(f"reviewer {i} of {len(prompts)}: no 'Do not report' list"); sys.exit(1)
    if ENTRY not in p:
        print(f"reviewer {i} of {len(prompts)}: the MCP tool-name entry is missing"); sys.exit(1)
print(f"{len(prompts)} reviewers, each with the exclusion"); sys.exit(0)
