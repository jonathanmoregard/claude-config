"""Grade one close-out-review run by what each reviewer could possibly have
seen, not by how the dispatches were packaged.

Reports, in order:

  status     pass | one-reviewer | late | leaked | no-hunt | nosub
  n          reviewer dispatches from the main session
  shape      one-block | concurrent | late -- how the wave was formed
  hunt       some reviewer prompt carries a class from hunt-classes.md
  classes    the class ids matched, for the summary line

WHAT IS GRADED, AND WHY IT IS NOT THE TOOL-USE BLOCK. The skill wants the
second reviewer's prompt fixed before the first reviewer's findings exist, so
the second cannot be anchored on them. One tool-use block guarantees that, and
the skill prefers it -- but `Agent` dispatches run ASYNCHRONOUSLY, so a second
dispatch in the very next message is equally blind: the first reviewer is still
mid-run and has reported nothing. Measured over ten runs, that is what happens
every time, and grading the block would have failed all ten for a packaging
detail while the property held.

So concurrency is read off the transcript directly: a dispatch is blind when it
precedes the last turn of every reviewer dispatched before it. A dispatch that
lands AFTER an earlier reviewer's final turn is `late` -- the findings existed,
so the prompt is anchorable -- and `leaked` when that prompt also carries the
earlier reviewer's content. Both fail; the distinction is for the summary line.

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

PREAMBLE = "hunt only the classes"

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


def text_of(ev):
    parts = []
    for b in blocks(ev):
        if b.get("type") == "text":
            parts.append(b.get("text", ""))
        elif b.get("type") == "tool_result":
            parts.append(json.dumps(b.get("content")))
    c = content(ev)
    if isinstance(c, str):
        parts.append(c)
    return " ".join(parts)


def read(path):
    """(dispatches, subagent_last_line, subagent_text) from the transcript.

    dispatches: [(line, tool_use_id, prompt, same_block_group)] in order.
    """
    dispatches, last, texts = [], {}, {}
    for i, line in enumerate(open(path, encoding="utf-8")):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        parent = ev.get("parent_tool_use_id")
        if parent:
            last[parent] = i
            texts[parent] = texts.get(parent, "") + " " + text_of(ev)
            continue
        if ev.get("type") != "assistant":
            continue
        for b in blocks(ev):
            if b.get("type") != "tool_use" or b.get("name") != "Agent":
                continue
            prompt = str((b.get("input") or {}).get("prompt", ""))
            if norm(prompt).startswith("consult dispatch"):
                continue
            if any(m in norm(prompt) for m in REVIEW_MARKERS):
                dispatches.append((i, b.get("id"), prompt))
    return dispatches, last, texts


def overlaps(earlier_text, prompt):
    """Does the prompt carry the earlier reviewer's specific content?"""
    e, p = norm(earlier_text), norm(prompt)
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
    dispatches, last, texts = read(path)
    n = len(dispatches)

    markers = class_markers()
    seen = set()
    joined = norm(" ".join(p for _, _, p in dispatches))
    for cid, ms in markers.items():
        if any(m in joined for m in ms) or re.search(rf"\b{re.escape(cid)}\b", joined):
            seen.add(cid)
    hunt = bool(seen) or PREAMBLE in joined

    # Per dispatch: could it have seen any reviewer dispatched before it?
    late = leaked = False
    lines = [d[0] for d in dispatches]
    one_block = n >= 2 and len(set(lines)) == 1
    for idx, (line, _tid, prompt) in enumerate(dispatches):
        for prev_line, prev_tid, _ in dispatches[:idx]:
            if prev_line == line:
                continue  # same tool-use block: nothing to have seen
            prev_end = last.get(prev_tid)
            if prev_end is not None and line > prev_end:
                late = True
                if overlaps(texts.get(prev_tid, ""), prompt):
                    leaked = True

    if n == 0:
        status = "nosub"
    elif n < 2:
        status = "one-reviewer"
    elif leaked:
        status = "leaked"
    elif late:
        status = "late"
    elif not hunt:
        status = "no-hunt"
    else:
        status = "pass"

    shape = "one-block" if one_block else ("late" if late else "concurrent")
    print(f"{status} {n} {shape} {str(hunt).lower()} {','.join(sorted(seen)) or '-'}")


if __name__ == "__main__":
    main()
