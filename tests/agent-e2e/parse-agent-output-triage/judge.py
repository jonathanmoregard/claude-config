"""Judge for parse-agent-output-triage: how does the edited triage.sh get the
category out of claude's answer? A no-tools judge returns an enum through
--json-schema (see ../_lib/structured_judge.py); this script compares fields.

usage: judge.py TRIAGE_SH FINAL_TXT
Headless runs in default mode may have the edit denied; the judge then grades
the change the final message proposes.
Prints "PASS ..." / "FAIL ..." and exits 0 / 1; exits 2 when the judge fails.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_lib'))
from structured_judge import Inconclusive, judge  # noqa: E402

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
    path, final = sys.argv[1], sys.argv[2]
    if not os.path.exists(path):
        print('FAIL: scripts/triage.sh is gone')
        return 1
    with open(path, encoding='utf-8', errors='replace') as fh:
        script = fh.read()
    message = ''
    if os.path.exists(final):
        with open(final, encoding='utf-8', errors='replace') as fh:
            message = fh.read()
    prompt = (INSTRUCTIONS + '\n=== THE SCRIPT NOW ===\n' + script
              + "\n=== THE ASSISTANT'S FINAL MESSAGE ===\n" + message)
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


sys.exit(main())
