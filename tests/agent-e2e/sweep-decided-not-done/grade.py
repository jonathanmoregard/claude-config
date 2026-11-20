"""Grade the sweep's inventory and Domain model lens file.

argv: inventory.md, Domain model lens file. Prints one line; exit 0 PASS, 1 FAIL.
D1  the inventory lists the pending room/lamp id decision (its title, or
    RoomId/LampId) outside "Leaving soon": the decision is still to do.
D2  in the lens file, no line about room or lamp ids is a SKIP, and at least
    one such line carries RECOMMEND, ADVISE or "decided": the lens reports
    the decided work instead of dropping it.
L1  "Leaving soon" lists legacy-sync (a planned deletion), and L2 no line
    there names the re-typing (control: the deletion still counts as leaving).
"""
import re
import sys

inv = open(sys.argv[1], encoding='utf-8').read()
lens = open(sys.argv[2], encoding='utf-8').read()

m = re.search(r'^#+[^\n]*leaving soon[^\n]*\n(.*?)(?=^#|\Z)', inv, re.I | re.S | re.M)
leaving = m.group(1) if m else ''
rest = inv.replace(leaving, '')
ids = re.compile(r'RoomId|LampId|room and lamp|room/lamp|\broom ids?\b|\blamp ids?\b', re.I)

if not re.search(r'legacy', leaving, re.I):
    print('L1: legacy-sync not listed as leaving'); sys.exit(1)
hit = next((l for l in leaving.splitlines() if not re.search('legacy', l, re.I) and ids.search(l)), None)
if hit:
    print(f'L2: re-typing listed as leaving: {hit.strip()[:80]}'); sys.exit(1)
if not ids.search(rest):
    print('D1: the pending id decision is not in the inventory'); sys.exit(1)
lines = [l for l in lens.splitlines() if ids.search(l) or re.search(r'\broom\b.*\bstring\b|\bstring\b.*\broom\b', l, re.I)]
skipped = [l for l in lines if re.search(r'\bSKIP\b', l)]
if skipped:
    print(f'D2: ids SKIPped: {skipped[0].strip()[:100]}'); sys.exit(1)
if not any(re.search(r'RECOMMEND|ADVISE|decided', l, re.I) for l in lines):
    print('D2: no item reports the room/lamp ids'); sys.exit(1)
print('L1, L2, D1, D2'); sys.exit(0)
