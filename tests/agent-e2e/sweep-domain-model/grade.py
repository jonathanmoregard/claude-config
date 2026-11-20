"""Item-level grader for sweep-domain-model; see check.sh for the checks.

A lens file is split into items: an item starts at a top-level numbered line
("1. ...", "**1** ...", "| 1 | RECOMMEND ...") and runs until the next one or a
heading, so wrapped lines and nested bullets stay with their item. The verdict
is the first verdict word on the item's first line.

Proposals are graded from an item's change text only, never from what it
quotes: the "Change:" bullet, the change segment of a one-line item
("N VERDICT EFFORT title — where — today — change — RED"), and table rows (the
vocabulary table). File paths and code comments are removed first, so
"src/core/lighting.ts" or "// -1 when none seen yet" never count.
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
PATH = re.compile(r'(?<![\w/])(?!n/a\b)(?:[\w.~-]*/)+[\w.-]+|\b[\w-]+\.(?:ts|js|mjs|json|md)\b(?::[\d,]+)?')
COMMENT = re.compile(r'//[^`\n]*')
CHANGE_BULLET = re.compile(r'^\s*[-*]\s+\**(?:Change|Fix|Proposal|Target)\b', re.I)
OTHER_BULLET = re.compile(r'^\s*[-*]\s+\**(?:Today|RED|Sites?|Where|Why|Limit|Effort)\b', re.I)


def clean(text):
    return COMMENT.sub(' ', PATH.sub(' ', text))


def change_of(lines):
    out, inside = [], False
    for ln in lines:
        if CHANGE_BULLET.match(ln):
            inside = True
        elif OTHER_BULLET.match(ln):
            inside = False
        if inside or ln.lstrip().startswith('|'):
            out.append(ln)
    segs = re.split(r'\s+—\s+', lines[0])
    if len(segs) >= 5:  # the one-line shape: change is the segment before RED
        out += [s for s in segs[3:] if not s.lstrip().startswith('RED')]
    return '\n'.join(out)


def items_of(text):
    groups, cur = [], None
    for ln in text.splitlines():
        if ln.lstrip().startswith('#'):
            cur = None
        elif ITEM_START.match(ln):
            cur = [ln]
            groups.append(cur)
        elif cur is not None:
            cur.append(ln)
    result = []
    for lines in groups:
        m = VERDICT.search(lines[0])
        result.append({'verdict': m.group(1) if m else None, 'raw': '\n'.join(lines),
                       'text': clean('\n'.join(lines)), 'title': lines[0],
                       'change': clean(change_of(lines)), 'change_raw': COMMENT.sub(' ', change_of(lines))})
    return result


items = [it for f in lens_files for it in items_of(open(f, encoding='utf-8').read())]
# proposals: not SKIP, and not rejected in passing as ceremony
live = [it for it in items if it['verdict'] not in (None, 'SKIP') and not re.search(r'ceremony', it['raw'], re.I)]


def proposes(*patterns, flags=re.I, field='change'):
    return any(all(re.search(p, it[field], flags) for p in patterns) for it in live)


missing = []
inv_text = open(inv[0], encoding='utf-8').read() if inv else ''
if not re.search(r'^\W*Tier\W*\s*personal\b', inv_text, re.I | re.M):
    missing.append('T:inventory not personal')
# P1: a labelled union for the preset, e.g. 'coldest' | 'cold' | ...
if not proposes(r'preset', r"['\"](?:coldest|cold|neutral|warm|warmest)['\"]\s*\|\s*['\"]\w+['\"]"):
    missing.append('P1:no labelled preset union')
# P2: lastPreset's -1 or room '' given an explicit case in the change
if not proposes(r"lastPreset|\broom\b|''", r"['\"][\w-]*(?:none|never|unseen)[\w-]*['\"]|\|\s*(?:undefined|null)\b|kind:"):
    missing.append('P2:planted sentinel not given an explicit case')
if not proposes(r'type\s+(?:Room|Lamp)Id\b|\b(?:Room|Lamp)Id\s*=|parse(?:Room|Lamp)Id|brand\w*\W+(?:Room|Lamp)Id', flags=0):
    missing.append('P3:no per-kind id types proposed')
# P4: the change hands entity/service names to the adapter
if not proposes(r'entit|service|input_number|light\.',
                r'(?:adapter|ha\.ts)[^\n]{0,80}\b(?:own|hold|keep|map|diff|translat)|\b(?:move|belong|push|live)\w*[^\n]{0,40}(?:adapter|ha\.ts)',
                field='change_raw'):
    missing.append('P4:HA names in core not given to the adapter')
# P5: one change line or vocabulary row puts the README's words in place of Plan
if not any(re.search(r'\bPlan\b', ln) and re.search(r'lighting state|\bLighting[A-Z]\w*|\w+Lighting\b', ln, re.I)
           for it in live for ln in it['change'].splitlines()):
    missing.append('P5:Plan not renamed from README words')
# G: the lens's own guard, a type-checker test that also rejects casts
if not any(re.search(r'guard|signature|lint|compiler', it['title'], re.I)
           and re.search(r'compiler|type.?checker|\btsc\b', it['text'], re.I)
           and re.search(r'\bcasts?\b|assertion', it['text'], re.I) for it in live):
    missing.append('G:no guard item')
venues = [f for f in lens_files if re.search(r'venue', os.path.basename(f), re.I)]
venues_skipped = bool(re.search(r'venues?[^\n]{0,80}skipped|skipped[^\n]{0,80}venues?', final_txt, re.I))
if not (venues or venues_skipped):
    missing.append('X:Venues neither ran nor reported skipped')
if venues and not any(re.search(r'n/a|not applicable', open(f, encoding='utf-8').read(), re.I) for f in venues):
    missing.append('X:Venues marks nothing n/a')
prod = r'alert|observab|metric|tracing|restore rehears|leak check|growth'
excused = r'n/a|not applicable|\bnot?\b|\bnone\b|without|\bskip'
if any(re.search(prod, ln, re.I) and not re.search(excused, ln, re.I) for it in live for ln in it['raw'].splitlines()):
    missing.append('X:production machinery proposed')
if not items:
    missing.append('no items parsed')

print('PASS T, P1-P5, G, X hold' if not missing else 'missing/bad: ' + ' '.join(missing))
