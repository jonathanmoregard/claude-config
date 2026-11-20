"""Grade the inventory's "Leaving soon" section.

argv: the assurance-sweep output dir. Exit 0 PASS, 1 FAIL (reason on stdout).
L1: the section names legacy-sync (scheduled for deletion).
L2: the section names none of the re-typing targets (room/lamp ids, the
    preset), which stay in the code and must be read by the lenses.
"""
import glob
import re
import sys

files = glob.glob(sys.argv[1] + '/*/inventory.md')
if not files:
    print('no inventory.md'); sys.exit(1)
text = open(files[0], encoding='utf-8').read()
m = re.search(r'^#+[^\n]*leaving soon[^\n]*\n(.*?)(?=^#|\Z)', text, re.I | re.S | re.M)
if not m:
    print('no Leaving soon section'); sys.exit(1)
sec = m.group(1)
if not re.search(r'legacy', sec, re.I):
    print('L1: legacy-sync not listed as leaving'); sys.exit(1)
# Line by line, skipping the legacy-sync line itself (it may say "lamp").
target = re.compile(r'RoomId|LampId|\bpresets?\b|\broom|re-?typ|string ids?|id types?|plain strings?', re.I)
hit = next((t.group(0) for line in sec.splitlines() if not re.search(r'legacy', line, re.I)
            for t in [target.search(line)] if t), None)
if hit:
    print(f'L2: re-typing target listed as leaving ({hit!r})'); sys.exit(1)
print('L1, L2'); sys.exit(0)
