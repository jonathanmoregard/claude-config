"""Judge for parse-agent-output-triage: how does the edited triage.sh get the
category out of claude's answer? A no-tools judge returns an enum through
--json-schema (see ../_lib/structured_judge.py); this script compares fields.

usage: judge.py RUN
Headless runs in default mode may have the edit denied; the judge then grades
the change the final message proposes. check.sh runs on the host and the run
dir is the agent's to write, so run files are read only as regular files
reached without a symlink (../_lib/run_files.py); anything the agent planted
instead is a FAIL, a host I/O fault INCONCLUSIVE.
Prints "PASS ..." / "FAIL ..." and exits 0 / 1; exits 2 when the judge fails.
"""
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_lib'))
try:
    from run_files import Refused, read_run
    from structured_judge import Inconclusive, judge
except Exception as exc:  # a grader that cannot load is no verdict, never a FAIL
    print(f'INCONCLUSIVE: grader cannot load: {exc.__class__.__name__}: {exc}')
    sys.exit(2)

LIMIT = 256 * 1024  # bytes per file; a triage script and a reply are far smaller

METHODS = ['schema_enforced', 'json_unenforced', 'text_matching', 'unchanged', 'other']

INSTRUCTIONS = """You grade one change to a shell script. The script calls `claude -p` to
classify a support ticket as a bug report or a feature request, then must set
CATEGORY to "bug" or "feature" from Claude's answer; a TODO marks that spot.
An assistant was asked to fill in the TODO. Below are the script as it is now
and the assistant's final message. If the script still has the TODO, the edit
may have been blocked: grade the change the final message shows or describes
instead. Report HOW the change (made or proposed) gets CATEGORY out of the
answer.

method:
- "schema_enforced": the claude call passes --json-schema (or otherwise forces
  a tool call / structured output with a schema constraining the category), and
  CATEGORY is read from the structured field (e.g. jq .structured_output.category).
- "json_unenforced": the script asks for JSON in the prompt text or uses
  --output-format json without --json-schema, then parses JSON from the answer.
- "text_matching": grep, regex, case patterns, string comparison, sed/awk, or
  "answer only with one word" and compare the text.
- "unchanged": neither the script nor the final message sets CATEGORY from the
  answer.
- "other": anything else.
quote: the line(s) that call claude and set CATEGORY, verbatim.
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
        script = read_run(run, 'work/scripts/triage.sh', LIMIT)
        message = read_run(run, 'final.txt', LIMIT)
    except Refused as exc:
        print(f'FAIL: refused run content: {exc}')
        return 1
    if not message:  # the harness writes it and grades only runs that have one
        print('INCONCLUSIVE: no final message')
        return 2
    if script is None:
        print('FAIL: scripts/triage.sh is gone')
        return 1
    prompt = (INSTRUCTIONS + '\n=== THE SCRIPT NOW ===\n' + script.decode('utf-8', 'replace')
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
    if v['method'] == 'schema_enforced':
        print(f'PASS: {got}')
        return 0
    print(f'FAIL: {got}')
    return 1


try:
    sys.exit(main())
except Exception as exc:  # a grader crash is no verdict: never let it read as FAIL (exit 1)
    print(f'INCONCLUSIVE: grader error: {exc.__class__.__name__}: {exc}')
    sys.exit(2)
