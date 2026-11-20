"""Self-test for grade.py: a passing sweep output, and one mutation per known
false PASS from review. Run: python3 grade_selftest.py (exit 0 = all hold)."""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
INV = 'Tier: personal (README: "my own lighting setup").\n'
ITEMS = {
    'guard': """1. **RECOMMEND S — Guard: one signature test in the existing suite** — `test/remote.test.js:3`.
   - Change: a test using the TypeScript compiler API fails on primitives in core exports and on casts to domain types outside parsers.
   - RED: 14 sites today.""",
    'sentinel': """2. **RECOMMEND S — Category 4, sentinels** — `src/core/remote.ts:6`.
   - Today: `lastPreset: number;  // -1 when none seen yet`.
   - Change: `lastPreset: Preset | 'none-seen'`.""",
    'preset': """3. **RECOMMEND S — Category 1, closed sets** — `src/core/remote.ts:5`.
   - Today: `lastPreset: 3` reads as a preset; the README says "five presets, coldest to warmest".
   - Change: `type Preset = 'coldest' | 'cold' | 'neutral' | 'warm' | 'warmest'`; `type LampLight = { kind: 'off' } | { kind: 'on' }`.""",
    'adapter': """4. **RECOMMEND M — Category 6, adapter vocabulary** — `src/core/lighting.ts:3`.
   - Today: `Plan {service, entity_id}`; the README says "the core works out the lighting state".
   - Change: the core returns new state; `ha.ts` diffs it and owns every entity and service name.""",
    'ids': """5. **RECOMMEND S — Category 3, ids** — `src/core/lighting.ts:5`.
   - Change: `type RoomId = string & { readonly __brand: 'RoomId' }`, likewise `LampId`.""",
    'brightness': """6. **RECOMMEND S — Category 2, bounded numbers** — `src/core/lighting.ts:5`.
   - Change: branded `Brightness` built by `parseBrightness`; drop the '' check once RoomId exists.""",
    'vocab': """8. **ASK S — Category 7, vocabulary table.**

   | # | Term | Replaces | Source |
   |---|---|---|---|
   | 7 | Lighting state | `Plan[]` | `README.md:6` |""",
}
VENUES = """1. ADVISE S — A real test for send — `src/adapter/ha.ts:5`.
2. ADVISE S — Alerting, restore rehearsal, growth measurement: n/a (personal tier)
"""


def grade(items, venues=VENUES, inv=INV):
    with tempfile.TemporaryDirectory() as root:
        d = os.path.join(root, '2026-01-01')
        os.makedirs(d)
        open(os.path.join(d, 'inventory.md'), 'w').write(inv)
        open(os.path.join(d, '3-domain-model.md'), 'w').write('# Lens 3\n\n' + '\n\n'.join(items.values()) + '\n')
        if venues is not None:
            open(os.path.join(d, '7-venues.md'), 'w').write('# Lens 7\n\n' + venues)
        return subprocess.run([sys.executable, os.path.join(HERE, 'grade.py'), root, ''],
                              capture_output=True, text=True).stdout.strip()


def without(*keys, **repl):
    out = {k: v for k, v in ITEMS.items() if k not in keys}
    out.update(repl)
    return out


CASES = [  # (name, output, the one check that must fail; None = must PASS)
    ('baseline', grade(ITEMS), None),
    ('template tier line', grade(ITEMS, inv='Tier: production | personal.\n'), 'T:'),
    ('bare numeric preset union', grade(without('preset', preset=ITEMS['preset'].replace(
        "'coldest' | 'cold' | 'neutral' | 'warm' | 'warmest'", '0 | 1 | 2 | 3 | 4'))), 'P1:'),
    ('sentinel only quoted', grade(without('sentinel')), 'P2:'),
    ('ids skipped as ceremony', grade(without('ids', ids=ITEMS['ids'].replace('RECOMMEND', 'SKIP (ceremony)'))), 'P3:'),
    ('ids rejected by an ADVISE', grade(without('ids', ids=ITEMS['ids'].replace('RECOMMEND', 'ADVISE') + ' RoomId would be ceremony.')), 'P3:'),
    ('adapter word only in a quote', grade(without('adapter', adapter=ITEMS['adapter'].split('\n   - Change')[0])), 'P4:'),
    ('Plan only beside a README quote', grade(without('vocab')), 'P5:'),
    ('no guard item', grade(without('guard')), 'G:'),
    ('Venues missing', grade(ITEMS, venues=None), 'X:Venues neither'),
    ('Venues marks nothing n/a', grade(ITEMS, venues='1. ADVISE S — A real test for send.\n'), 'X:Venues marks'),
    ('alerting in a change body', grade(ITEMS, venues=VENUES + '3. **RECOMMEND S — Know when send fails**\n   - Change: push an alert to the phone.\n'), 'X:production'),
    ('alerting in a title', grade(ITEMS, venues=VENUES + '3. RECOMMEND S — Test send — add an alert when callService fails\n'), 'X:production'),
]
bad = 0
for name, out, must in CASES:
    ok = out.startswith('PASS') if must is None else (out.startswith('missing/bad: ' + must) and out.count(':') == 2)
    bad += not ok
    print(('ok   ' if ok else 'FAIL ') + name + ': ' + out)
sys.exit(1 if bad else 0)
