"""Line-level grader for sweep-domain-model; see check.sh for the checks."""
import glob
import os
import re
import sys

root = sys.argv[1]
inv = glob.glob(os.path.join(root, '*', 'inventory.md'))
lens_files = [f for f in glob.glob(os.path.join(root, '*', '*.md')) if not f.endswith('inventory.md')]
lines = [ln for f in lens_files for ln in open(f, encoding='utf-8')]
missing = []


def any_line(*patterns, unless=None):
    for ln in lines:
        if all(re.search(p, ln, re.I) for p in patterns) and not (unless and re.search(unless, ln, re.I)):
            return True
    return False


inv_text = open(inv[0], encoding='utf-8').read() if inv else ''
if not re.search(r'tier[^\n]{0,40}personal', inv_text, re.I):
    missing.append('T:inventory not personal')
if not any_line(r'preset', r'coldest|warmest|\blabel'):
    missing.append('P1:no labelled preset type')
if not any_line(r'lastPreset|room\s*===?\s*\'\'|empty string|\'\'', r'sentinel|explicit|none|no preset'):
    missing.append('P2:planted sentinel not named')
if not any_line(r'RoomId|LampId|brand', unless=r'\bSKIP\b|ceremony'):
    missing.append('P3:no un-skipped id types')
if not any_line(r'input_number|light\.turn_on|entity_id|service call|entity name', r'adapter|core'):
    missing.append('P4:HA names in core not named')
if not any_line(r'\bPlan\b', r'lighting ?state|[A-Z]?\w*Lighting\b'):
    missing.append('P5:Plan not renamed from README words')
if not any(re.search(r'venue', os.path.basename(f), re.I) for f in lens_files):
    missing.append('X:no Venues lens file')
prod = r'alert|observab|metric|tracing|restore rehears|leak check|growth'
if any(re.search(r'\bRECOMMEND\b', ln) and re.search(prod, ln, re.I) for ln in lines):
    missing.append('X:production machinery recommended')

print('PASS T, P1-P5, X hold' if not missing else 'missing/bad: ' + ' '.join(missing))
