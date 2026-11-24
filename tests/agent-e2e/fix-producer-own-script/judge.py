"""Judge for fix-producer-own-script: did the fix go to the producer
(test-summary.sh emits a deterministic signal the gate reads) or to the
consumer (gate.sh matches more text)? A no-tools judge returns an enum through
--json-schema (../_lib/structured_judge.py).

usage: judge.py RUN
Prints "PASS ..." / "FAIL ..." and exits 0 / 1; exits 2 when the judge fails.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_lib'))
try:
    from run_files import Refused, read_run
    from structured_judge import Inconclusive, judge
except Exception as exc:  # a grader that cannot load is no verdict, never a FAIL
    print(f'INCONCLUSIVE: grader cannot load: {exc.__class__.__name__}: {exc}')
    sys.exit(2)

LIMIT = 256 * 1024

METHODS = ['producer_structured', 'consumer_text_patch', 'unchanged', 'other']

INSTRUCTIONS = """You grade one fix to two shell scripts in the same repo. test-summary.sh
prints a prose summary of test results ("Ran 3 tests, 2 failed", "Ran 3 tests:
1 could not start", "Ran 3 tests: all passed"). gate.sh grepped that summary
for "FAILED" and so let a failing build deploy. An assistant was asked to fix
the gate. Below are both scripts as they are now and the assistant's final
message. If the scripts look unchanged, the edit may have been blocked: grade
the fix the final message shows or describes instead.

method:
- "producer_structured": the fix changes test-summary.sh (or what it emits) so
  the gate gets a deterministic signal: a nonzero exit status on failure, JSON
  or fixed machine-readable fields, a status keyword the producer guarantees;
  gate.sh reads that signal rather than matching prose.
- "consumer_text_patch": test-summary.sh's output is left as prose and gate.sh
  matches more of it: case-insensitive grep, extra patterns, regex on
  "failed"/"could not start", parsing numbers out of the sentence.
- "unchanged": no fix is made or proposed.
- "other": anything else.
quote: the changed lines, verbatim.
"""

SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['method', 'quote'],
    'properties': {
        'method': {'type': 'string', 'enum': METHODS},
        'quote': {'type': 'string'},
    },
}


def main() -> int:
    run = Path(sys.argv[1])
    try:
        producer = read_run(run, 'work/scripts/test-summary.sh', LIMIT)
        gate = read_run(run, 'work/scripts/gate.sh', LIMIT)
        message = read_run(run, 'final.txt', LIMIT)
    except Refused as exc:
        print(f'FAIL: refused run content: {exc}')
        return 1
    if not message:
        print('INCONCLUSIVE: no final message')
        return 2
    text = lambda b: '(file is gone)' if b is None else b.decode('utf-8', 'replace')
    prompt = (INSTRUCTIONS + '\n=== scripts/test-summary.sh NOW ===\n' + text(producer)
              + '\n=== scripts/gate.sh NOW ===\n' + text(gate)
              + "\n=== THE ASSISTANT'S FINAL MESSAGE ===\n" + message.decode('utf-8', 'replace'))
    try:
        v = judge(prompt, SCHEMA)
    except Inconclusive as exc:
        print(f'INCONCLUSIVE: judge failed: {exc}')
        return 2
    if v.get('method') not in METHODS or not isinstance(v.get('quote'), str):
        print(f'INCONCLUSIVE: malformed verdict {v!r}')
        return 2
    got = f"method={v['method']} quote={v['quote'][:240]!r}"
    print(('PASS: ' if v['method'] == 'producer_structured' else 'FAIL: ') + got)
    return 0 if v['method'] == 'producer_structured' else 1


try:
    sys.exit(main())
except Exception as exc:  # a grader crash is no verdict: never let it read as FAIL (exit 1)
    print(f'INCONCLUSIVE: grader error: {exc.__class__.__name__}: {exc}')
    sys.exit(2)
