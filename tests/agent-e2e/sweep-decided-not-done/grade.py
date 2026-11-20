"""Grade the sweep's inventory and Domain model lens file.

argv: inventory.md, Domain model lens file. Prints one line; exit 0 PASS,
1 FAIL, 2 when the grader itself fails (INCONCLUSIVE).
L1  "Leaving soon" lists legacy-sync (a planned deletion), and L2 no line
    there names the re-typing (control: the deletion still counts as leaving).
D1  the "Decided, not done" section lists the pending room/lamp id decision.
D2  in the lens file, no line about room or lamp ids is skipped or set aside
    as already decided, and at least one carries RECOMMEND, ADVISE or
    "decided": the lens reports the decided work instead of dropping it.
K1  "Decided, not done" leaves out the finished decision (scripts/check) and
    the keep decision (brightness).
K2  the lens recommends nothing about brightness: a keep decision is not a
    finding.
"""
import re
import sys


def section(doc, name):
    m = re.search(r'^#+[^\n]*' + name + r'[^\n]*\n(.*?)(?=^#|\Z)', doc, re.I | re.S | re.M)
    return m.group(1) if m else ''


def grade(inv, lens):
    leaving, decided = section(inv, 'leaving soon'), section(inv, r'decided, not done')
    ids = re.compile(r'RoomId|LampId|room and lamp|room/lamp|\broom ids?\b|\blamp ids?\b', re.I)
    if not re.search(r'legacy', leaving, re.I):
        return 1, 'L1: legacy-sync not listed as leaving'
    hit = next((l for l in leaving.splitlines() if not re.search('legacy', l, re.I) and ids.search(l)), None)
    if hit:
        return 1, f'L2: re-typing listed as leaving: {hit.strip()[:80]}'
    if not ids.search(decided):
        return 1, 'D1: "Decided, not done" does not list the id decision'
    if re.search(r'scripts/check|brightness', decided, re.I):
        return 1, 'K1: a done or keep decision is listed as not done'
    lines = [l for l in lens.splitlines()
             if ids.search(l) or re.search(r'\broom\b.*\bstring\b|\bstring\b.*\broom\b', l, re.I)]
    dropped = [l for l in lines if re.search(r'\bskip(ped|s)?\b|already decided', l, re.I)]
    if dropped:
        return 1, f'D2: ids set aside: {dropped[0].strip()[:100]}'
    if not any(re.search(r'RECOMMEND|ADVISE|decided', l, re.I) for l in lines):
        return 1, 'D2: no item reports the room/lamp ids'
    kept = [l for l in lens.splitlines()
            if re.search(r'brightness', l, re.I) and re.search(r'RECOMMEND|ADVISE', l) and not ids.search(l)]
    if kept:
        return 1, f'K2: keep decision reported: {kept[0].strip()[:100]}'
    return 0, 'L1, L2, D1, D2, K1, K2'


try:
    code, msg = grade(open(sys.argv[1], encoding='utf-8').read(), open(sys.argv[2], encoding='utf-8').read())
except Exception as e:  # a grader fault is not the agent's FAIL
    code, msg = 2, f'grader error: {e!r}'
print(msg)
sys.exit(code)
