"""Grade the sweep's inventory and Domain model lens file.

argv: inventory.md, Domain model lens file. Prints one line; exit 0 PASS,
1 FAIL, 2 when the grader itself fails (INCONCLUSIVE).
L1  "Leaving soon" lists legacy-sync (a planned deletion), and L2 no line
    there names the room or lamp ids (control: the deletion still counts as
    leaving; the re-typing does not).
D1  the "Decided, not done" section lists the pending room/lamp id decision.
D2  the lens's items (lenses.md: one line each, "N VERDICT EFFORT title —
    ...", VERDICT one of RECOMMEND, ADVISE, SKIP, ASK, in uppercase) about
    room or lamp ids: none has SKIP as its verdict, and at least one has
    RECOMMEND or ADVISE. The verdict word is the skill's own structured
    field; prose wording is not graded.
K1  "Decided, not done" leaves out the finished decision (scripts/check) and
    the keep decision (brightness).
K2  no item whose title (the text before the first "—") names brightness
    has RECOMMEND or ADVISE as its verdict: a keep decision is not a finding.
"""
import re
import sys


def section(doc, name):
    m = re.search(r'^#+[^\n]*' + name + r'[^\n]*\n(.*?)(?=^#|\Z)', doc, re.I | re.S | re.M)
    return m.group(1) if m else ''


VERDICT = re.compile(r'\b(RECOMMEND|ADVISE|SKIP|ASK)\b')


ITEM = re.compile(r'^\s*(\|\s*)?\d+\.?\s')  # "N VERDICT ..." or a "| N | ..." table row


def verdict_items(doc):
    """(verdict, line) for each item line: it starts with the item's number,
    and its verdict is the first verdict word on it."""
    return [(m.group(1), l) for l in doc.splitlines() if ITEM.match(l)
            for m in [VERDICT.search(l)] if m]


def grade(inv, lens):
    leaving, decided = section(inv, 'leaving soon'), section(inv, r'decided, not done')
    ids = re.compile(r'RoomId|LampId|room and lamp|room/lamp|\broom ids?\b|\blamp ids?\b', re.I)
    if not re.search(r'legacy', leaving, re.I):
        return 1, 'L1: legacy-sync not listed as leaving'
    hit = next((l for l in leaving.splitlines() if ids.search(l)), None)
    if hit:
        return 1, f'L2: re-typing listed as leaving: {hit.strip()[:80]}'
    if not ids.search(decided):
        return 1, 'D1: "Decided, not done" does not list the id decision'
    if re.search(r'scripts/check|brightness', decided, re.I):
        return 1, 'K1: a done or keep decision is listed as not done'
    found = verdict_items(lens)
    # The id item may be titled by the decision rather than the ids: match
    # "identifiers", "room ... string", or the decision's date.
    about_ids = [(v, l) for v, l in found
                 if ids.search(l) or re.search(r'\bidentifiers?\b|2026-11-19|\broom\b.*\bstring\b|\bstring\b.*\broom\b', l, re.I)]
    skipped = [l for v, l in about_ids if v == 'SKIP']
    if skipped:
        return 1, f'D2: ids SKIPped: {skipped[0].strip()[:100]}'
    if not any(v in ('RECOMMEND', 'ADVISE') for v, _ in about_ids):
        return 1, 'D2: no RECOMMEND or ADVISE item reports the room/lamp ids'
    kept = [l for v, l in found
            if v in ('RECOMMEND', 'ADVISE') and re.search(r'brightness', l.split('—')[0], re.I)]
    if kept:
        return 1, f'K2: keep decision reported: {kept[0].strip()[:100]}'
    return 0, 'L1, L2, D1, D2, K1, K2'


try:
    code, msg = grade(open(sys.argv[1], encoding='utf-8').read(), open(sys.argv[2], encoding='utf-8').read())
except Exception as e:  # a grader fault is not the agent's FAIL
    code, msg = 2, f'grader error: {e!r}'
print(msg)
sys.exit(code)
