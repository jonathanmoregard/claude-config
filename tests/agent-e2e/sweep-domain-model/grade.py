"""Item-level grader for sweep-domain-model; see check.sh for the checks.

A lens file is split into items: an item starts at a top-level numbered line
("1. ...", "**1** ...", "| 1 | RECOMMEND ...") and runs until the next one or a
heading, so wrapped lines and nested bullets stay with their item. The verdict
is the first verdict word on the item's first line. File paths are removed
before matching, so "src/core/lighting.ts" never counts as a domain word.
"""
import glob
import os
import re
import sys

root = sys.argv[1]
final_txt = sys.argv[2] if len(sys.argv) > 2 else ''
inv = glob.glob(os.path.join(root, '*', 'inventory.md'))
lens_files = [f for f in glob.glob(os.path.join(root, '*', '*.md')) if not f.endswith('inventory.md')]

ITEM_START = re.compile(r'^\s{0,3}(?:[-*]\s+)?\**\d+[.)]?\**\s|^\|\s*\d+\s*\|\s*\**(?:RECOMMEND|ADVISE|SKIP|ASK)')
VERDICT = re.compile(r'\b(RECOMMEND|ADVISE|SKIP|ASK)\b')
PATH = re.compile(r'(?:[\w.~-]*/)+[\w.-]+|\b[\w-]+\.(?:ts|js|mjs|json|md)\b(?::[\d,]+)?')
COMMENT = re.compile(r'//[^`\n]*')


def items_of(text):
    out, cur = [], None
    for ln in text.splitlines():
        if ln.lstrip().startswith('#'):
            cur = None
        elif ITEM_START.match(ln):
            cur = [ln]
            out.append(cur)
        elif cur is not None:
            cur.append(ln)
    result = []
    for lines in out:
        m = VERDICT.search(lines[0])
        body = COMMENT.sub(' ', PATH.sub(' ', '\n'.join(lines)))
        result.append({'verdict': m.group(1) if m else None, 'title': PATH.sub(' ', lines[0]), 'text': body})
    return result


items = [it for f in lens_files for it in items_of(open(f, encoding='utf-8').read())]
live = [it for it in items if it['verdict'] not in (None, 'SKIP')]


def any_live(*patterns, flags=re.I):
    return any(all(re.search(p, it['text'], flags) for p in patterns) for it in live)


missing = []
inv_text = open(inv[0], encoding='utf-8').read() if inv else ''
if not re.search(r'^\W*Tier\W*\s*personal\b', inv_text, re.I | re.M):
    missing.append('T:inventory not personal')
# P1: a labelled union for the preset, e.g. 'coldest' | 'cold' | ...
labelled_union = r"['\"](?:coldest|cold|neutral|warm|warmest)['\"]\s*\|\s*['\"]\w+['\"]"
if not any_live(r'preset', labelled_union):
    missing.append('P1:no labelled preset union')
# P2: lastPreset's -1 or room === '' named as a sentinel or given an explicit case
if not any_live(r"lastPreset|room\s*===?\s*''|''", r"sentinel|explicit|['\"][\w-]*none[\w-]*['\"]|\|\s*(?:undefined|null)\b|kind:"):
    missing.append('P2:planted sentinel not named')
if not any_live(r'RoomId|LampId|\bbrand'):
    missing.append('P3:no un-skipped id types')
if not any_live(r'input_number|light\.turn_on|entity_id|service call|entity name|service name', r'\badapter\b'):
    missing.append('P4:HA names in core not given to the adapter')
if not any_live(r'\bPlan\b', r'lighting state|\bLighting[A-Z]\w*|\w+Lighting\b'):
    missing.append('P5:Plan not renamed from README words')
venues_ran = any(re.search(r'venue', os.path.basename(f), re.I) for f in lens_files)
venues_skipped = bool(re.search(r'venues?[^\n]{0,80}skipped|skipped[^\n]{0,80}venues?', final_txt, re.I))
if not (venues_ran or venues_skipped):
    missing.append('X:Venues neither ran nor reported skipped')
prod = r'alert|observab|metric|tracing|restore rehears|leak check|growth'
if any(re.search(prod, it['title'], re.I) and not re.search(r'n/a', it['title'], re.I) for it in live):
    missing.append('X:production machinery proposed')
if not items:
    missing.append('no items parsed')

print('PASS T, P1-P5, X hold' if not missing else 'missing/bad: ' + ' '.join(missing))
