"""Judge for parse-agent-output-triage: how does the edited triage.sh get the
category out of claude's answer? A no-tools judge returns an enum through
--json-schema (see ../_lib/structured_judge.py); this script compares fields.

usage: judge.py TRIAGE_SH
Prints "PASS ..." / "FAIL ..." and exits 0 / 1; exits 2 when the judge fails.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_lib'))
from structured_judge import Inconclusive, judge  # noqa: E402

METHODS = ['schema_enforced', 'json_unenforced', 'text_matching', 'unchanged', 'other']

INSTRUCTIONS = """You grade one shell script. It calls `claude -p` to classify a support
ticket as a bug report or a feature request, then must set CATEGORY to "bug" or
"feature" from Claude's answer. Report HOW the script gets CATEGORY out of the
answer.

method:
- "schema_enforced": the claude call passes --json-schema (or otherwise forces
  a tool call / structured output with a schema constraining the category), and
  CATEGORY is read from the structured field (e.g. jq .structured_output.category).
- "json_unenforced": the script asks for JSON in the prompt text or uses
  --output-format json without --json-schema, then parses JSON from the answer.
- "text_matching": grep, regex, case patterns, string comparison, sed/awk, or
  "answer only with one word" and compare the text.
- "unchanged": the TODO is still there and CATEGORY is never set from the answer.
- "other": anything else.
quote: the line(s) that call claude and set CATEGORY, verbatim.

=== THE SCRIPT ===
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
    path = sys.argv[1]
    if not os.path.exists(path):
        print('FAIL: scripts/triage.sh is gone')
        return 1
    with open(path, encoding='utf-8', errors='replace') as fh:
        script = fh.read()
    try:
        v = judge(INSTRUCTIONS + script, SCHEMA)
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
