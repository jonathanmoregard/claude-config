"""Grade one close-out-review run by what each reviewer could possibly have
seen, not by how the dispatches were packaged.

Reports, in order:

  status     pass | one-reviewer | late | leaked | no-hunt | nosub | nodata
  n          reviewer dispatches from the main session
  shape      one-block | concurrent | late -- how the wave was formed
  hunt       some reviewer prompt carries a class from hunt-classes.md
  classes    the class ids matched, for the summary line

WHAT IS GRADED, AND WHY IT IS NOT THE TOOL-USE BLOCK. The skill wants the
second reviewer's prompt fixed before the first reviewer's findings reach the
main thread, so the second cannot be anchored on them. One tool-use block
guarantees that. A later message also achieves it when the dispatch returns
before the reviewer reports -- which is what a backgrounded `Agent` does, and
what all ten measured runs did -- but NOT when the tool result carries the
report itself. So the measured quantity is DELIVERY, not dispatch packaging
and not an assumption about the harness:

  completion announced          -> its `task_notification` line
  tool_result AFTER the last    -> that line: the result carries the report
    turn attributed to it
  tool_result BEFORE that turn  -> a launch receipt, not the report. Skip it.
  nothing recorded              -> the last turn attributed to that reviewer

NO ONE SIGNAL IS LOAD-BEARING. `task_started.is_backgrounded` used to decide
which of the two readings applied, which made a missing event a false FAIL: a
backgrounded run read as foreground grades `late`, because the launch receipt
lands early by construction (measured: receipt at line 71, delivery at 155).
Preferring the announcement, and ordering the receipt against the reviewer's
own turns when there is none, needs neither flag. On the 26 recorded dispatches
that ordering reproduces `is_backgrounded` exactly, 20 receipts and 6 reports,
without reading it. `input.run_in_background` is also honoured where present,
but it is not a substitute: the harness backgrounds on its own, so 16 of those
20 carry no such input.

A dispatch landing after an earlier reviewer's delivery line is `late` -- the
findings were in the parent's context, so the prompt is anchorable -- and
`leaked` when it also carries that reviewer's reported content. Both fail; the
distinction is for the summary line. Reading delivery this way grades a
foreground harness correctly without the skill or this file having to assert
which kind it is running under.

MISSING DATA IS NOT A PASS. If an earlier reviewer has no delivery line and no
attributed turn, nothing here can say whether the next dispatch was blind, so
the verdict is `nodata` and check.sh reports INCONCLUSIVE. Treating the absence
as blindness is the fail-open pattern this very skill hunts as D1, and it would
turn a stream-format change -- subagent events logged elsewhere, a renamed
field -- into a silent green.

HUNT MARKERS COME FROM hunt-classes.md, never from a list kept here. The file
tells the dispatcher to paste each chosen class's `Hunt` line verbatim, so the
marker for a class is the opening of that very line, read at grading time. A
hand-maintained list of class titles drifts the moment the prose is edited --
and did: titles appear in the headings, not in the Hunt lines, so every class
but one was unmatchable while the comment claimed otherwise.
"""
import json
import re
import sys
from pathlib import Path

HUNTS = (Path(__file__).resolve().parents[3]
         / "runtime/skills/advice-refine-test-loop/references/hunt-classes.md")

# A dispatch counts as a reviewer when it asks for review and not for edits.
REVIEW_MARKERS = ("falsify", "review", "finding", "defect", "hunt", "ship broken")


def norm(text):
    return " ".join(str(text).split()).lower()


def class_markers(path=HUNTS):
    """{class id: [marker, ...]} built from the file the dispatcher reads."""
    out = {}
    if not path.exists():
        return out
    txt = path.read_text(encoding="utf-8")
    for cid, title, body in re.findall(r"^### (\S+) — (.+?)$(.*?)(?=^### |\Z)",
                                       txt, re.M | re.S):
        hunt = re.search(r"\*\*Hunt:\*\*(.*?)(?=\n\n|\Z)", body, re.S)
        markers = [norm(title)]
        if hunt:
            # The opening of the Hunt line: enough to identify a verbatim
            # paste, short enough to survive re-wrapping.
            words = norm(hunt.group(1)).split()
            if len(words) >= 6:
                markers.append(" ".join(words[:8]))
        out[cid] = markers
    return out


def content(ev):
    m = ev.get("message")
    return (m.get("content") if isinstance(m, dict) else m) or ""


def blocks(ev):
    c = content(ev)
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def spoken_text(ev):
    """Only what the agent SAID: text blocks, never tool results.

    The overlap check below compares B's prompt against what A reported. Tool
    results are what A READ -- whole source files, usually the same diff B was
    handed -- so including them made any late B that quoted the code look like
    it had quoted A.
    """
    parts = [b.get("text", "") for b in blocks(ev) if b.get("type") == "text"]
    c = content(ev)
    if isinstance(c, str):
        parts.append(c)
    return " ".join(p for p in parts if p)


def read(path):
    """Everything the verdict needs, keyed by the dispatch's tool_use_id.

    dispatches  [(line, tool_use_id, prompt)] from the main session, in order
    bg          tool_use_id -> the dispatch was backgrounded
    notify      tool_use_id -> line where its completion was announced
    result      tool_use_id -> line of its tool_result
    turn_last   tool_use_id -> line of the last turn attributed to it
    report      tool_use_id -> text of its last speaking turn (its report)
    """
    dispatches = []
    bg, notify, result, turn_last, report = {}, {}, {}, {}, {}
    for i, line in enumerate(open(path, encoding="utf-8")):
        try:
            ev = json.loads(line)
        except ValueError:
            continue

        parent = ev.get("parent_tool_use_id")
        if parent:
            turn_last[parent] = i
            said = spoken_text(ev)
            if said.strip():
                report[parent] = said
            continue

        # Main-thread bookkeeping the harness emits about each subagent.
        tid = ev.get("tool_use_id")
        if ev.get("type") == "system" and tid:
            sub = ev.get("subtype")
            if sub == "task_started" and ev.get("is_backgrounded"):
                bg[tid] = True
            elif sub == "task_notification" and ev.get("status") in (
                    "completed", "failed", "error"):
                notify.setdefault(tid, i)
            continue

        for b in blocks(ev):
            if b.get("type") == "tool_result" and b.get("tool_use_id"):
                result.setdefault(b["tool_use_id"], i)

        if ev.get("type") != "assistant":
            continue
        for b in blocks(ev):
            if b.get("type") != "tool_use" or b.get("name") != "Agent":
                continue
            inp = b.get("input") or {}
            prompt = str(inp.get("prompt", ""))
            if norm(prompt).startswith("consult dispatch"):
                continue
            if inp.get("run_in_background"):
                bg[b.get("id")] = True
            if any(m in norm(prompt) for m in REVIEW_MARKERS):
                dispatches.append((i, b.get("id"), prompt))
    return dispatches, bg, notify, result, turn_last, report


def delivery(tid, bg, notify, result, turn_last):
    """Line where this reviewer's findings reached the main thread, or None.

    Resolved by what the stream shows rather than by which harness it came
    from. The completion announcement is the direct record and wins outright.
    Otherwise a tool_result counts as the report only if it could be one: a
    result that landed BEFORE the reviewer's own last turn is a launch receipt,
    since nothing can deliver findings the reviewer has not yet spoken. The
    last attributed turn is the floor. None means the transcript cannot say,
    and the caller grades that `nodata` rather than blind.

    The point of the ordering is that no single harness event is load-bearing.
    Deciding on `is_backgrounded` alone turned a missing event into a false
    FAIL -- the receipt is early by construction, so a backgrounded run read as
    foreground grades `late`. The flags are still honoured where present, but
    only to skip a receipt the turn order would skip anyway.
    """
    if tid in notify:
        return notify[tid]
    res, last = result.get(tid), turn_last.get(tid)
    receipt = bg.get(tid) or (res is not None and last is not None
                              and res < last)
    if res is not None and not receipt:
        return res
    return last


def overlaps(reported, prompt):
    """Does the prompt carry the earlier reviewer's reported content?"""
    e, p = norm(reported), norm(prompt)
    for pair in set(re.findall(r"[\w./-]+\.\w{1,5}:\d+", e)):
        if pair in p:
            return True
    ewords = e.split()
    for i in range(len(ewords) - 7):
        gram = " ".join(ewords[i:i + 8])
        if len(gram) > 40 and gram in p:
            return True
    return False


def main():
    path = sys.argv[1]
    dispatches, bg, notify, result, turn_last, report = read(path)
    n = len(dispatches)

    markers = class_markers()
    seen = set()
    joined = norm(" ".join(p for _, _, p in dispatches))
    for cid, ms in markers.items():
        # `joined` is lowercased by norm(), so the bare id must be too, or a
        # prompt naming `D6` without its title or Hunt text never matched.
        if any(m in joined for m in ms) or re.search(rf"\b{re.escape(cid.lower())}\b", joined):
            seen.add(cid)
    # Named classes only. The preamble alone used to satisfy this, which passed
    # exactly the "B became a second A, with a header" run `no-hunt` is for.
    hunt = bool(seen)

    # Per dispatch: had any reviewer dispatched before it already reported?
    late = leaked = nodata = False
    lines = [d[0] for d in dispatches]
    one_block = n >= 2 and len(set(lines)) == 1
    for idx, (line, _tid, prompt) in enumerate(dispatches):
        for prev_line, prev_tid, _ in dispatches[:idx]:
            if prev_line == line:
                continue  # same tool-use block: nothing to have seen
            prev_end = delivery(prev_tid, bg, notify, result, turn_last)
            if prev_end is None:
                nodata = True
            elif line > prev_end:
                late = True
                if overlaps(report.get(prev_tid, ""), prompt):
                    leaked = True

    if n == 0:
        status = "nosub"
    elif n < 2:
        status = "one-reviewer"
    elif leaked:
        status = "leaked"
    elif late:
        status = "late"
    elif nodata:
        status = "nodata"
    elif not hunt:
        status = "no-hunt"
    else:
        status = "pass"

    shape = "one-block" if one_block else ("late" if late else "concurrent")
    print(f"{status} {n} {shape} {str(hunt).lower()} {','.join(sorted(seen)) or '-'}")


if __name__ == "__main__":
    main()
