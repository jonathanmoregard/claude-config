#!/usr/bin/env python3
"""Grade SECURITY-REVIEW-PLAN.md for the method elements of a SOTA security audit.

usage: grade_plan.py PLAN.md   -> prints one line per element, exit 0 if all REQUIRED present.
"""
import re, sys

text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
low = text.lower()

def has(*pats):
    return any(re.search(p, low) for p in pats)

def first(*pats):
    idx = [m.start() for p in pats for m in [re.search(p, low)] if m]
    return min(idx) if idx else None

TOOLS = (r"\bsemgrep\b", r"\bbandit\b", r"\bzizmor\b", r"\bgitleaks\b", r"\bactionlint\b", r"\bvulnix\b", r"\btrivy\b", r"\bcodeql\b")
research_at = first(r"state[- ]of[- ]the[- ]art", r"\bsota\b", r"research (the )?(current|latest|best)", r"literature")
tool_at = first(*TOOLS)

ELEMENTS = {
    # research the current SOTA for each domain BEFORE fixing the tool list
    "research_first": research_at is not None and (tool_at is None or research_at < tool_at)
        and has(r"(tool (list|plan|ledger)|which tools).{0,200}(from|based on|informed by).{0,40}research",
                r"research.{0,200}(produce|yield|output|derive|feed|select|choose|pick).{0,60}tool",
                r"research[^.\n]{0,80}(recommend|tool table)", r"tool (table|list)[^.\n]{0,80}research"),
    # every recommended free tool is run or skipped with a recorded reason
    "tool_ledger": has(r"ledger", r"skip(ped)?[^.\n]{0,80}(reason|why)", r"(reason|why)[^.\n]{0,60}skip"),
    # independent review by more than one model family, split by bug class
    "cross_model": has(r"cross[- ]model", r"(different|second|other|another|multiple) model", r"model famil", r"\bcodex\b", r"\bgpt"),
    # separate adversarial stage that tries to kill each candidate
    "refute_stage": has(r"refut", r"disprove", r"try(ing)? to (kill|falsify|disprove)", r"adversarial(ly)? (verif|review|check)"),
    # empirical oracle / reproduction before a finding counts, red test before fix
    "oracle_then_red_test": has(r"reproduc", r"oracle", r"proof[- ]of[- ]concept", r"\bpoc\b")
        and has(r"(failing|red) test", r"test[- ]first", r"regression test"),
    # measure the capability/workflow cost of each hardening change
    "capability_cost": has(r"capabilit", r"(break|hurt|disrupt|regress)\w*[^.\n]{0,60}workflow", r"workflow[^.\n]{0,60}(break|regress|cost)", r"usability"),
    # one fresh review over the finished integrated diff (fixes can add bugs)
    "closeout_review": has(r"close[- ]?out", r"(fresh|independent|final)[^.\n]{0,40}review[^.\n]{0,80}(diff|fix)", r"review[^.\n]{0,40}(integrated|final) diff"),
    # final report includes the tools that were skipped and why
    "skipped_walkthrough": has(r"skipped[^.\n]{0,120}(tool|why|reason)", r"tools? (that were |i )?(not run|skipped)"),
}
REQUIRED = list(ELEMENTS)
for k, v in ELEMENTS.items():
    print(f"{'OK ' if v else 'MISSING'} {k}")
missing = [k for k in REQUIRED if not ELEMENTS[k]]
sys.exit(1 if missing else 0)
