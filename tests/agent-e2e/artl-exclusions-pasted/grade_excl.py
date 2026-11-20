"""Does every reviewer dispatch carry the exclusion list?

argv: transcript.jsonl. Prints one line; exit 0 PASS, 1 FAIL, 2 no reviewer.
A reviewer dispatch is a main-session Agent call whose prompt reads as a
review (the markers artl-two-blind-reviewers uses) and is not a CONSULT.
Each must hold "Do not report" and a phrase from each exclusion entry.
"""
import json
import sys

MARKERS = ("falsify", "review", "finding", "defect", "hunt", "ship broken")
ENTRIES = {"tool list": ("tool list", "no real tool"), "sandbox": ("sandbox",)}


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
            if not p.startswith("consult dispatch") and any(m in p for m in MARKERS):
                prompts.append(p)
if not prompts:
    print("no reviewer dispatched"); sys.exit(2)
for i, p in enumerate(prompts, 1):
    if "do not report" not in p:
        print(f"reviewer {i} of {len(prompts)}: no 'Do not report' list"); sys.exit(1)
    for name, phrases in ENTRIES.items():
        if not any(ph in p for ph in phrases):
            print(f"reviewer {i} of {len(prompts)}: entry '{name}' missing"); sys.exit(1)
print(f"{len(prompts)} reviewers, each with both exclusions"); sys.exit(0)
