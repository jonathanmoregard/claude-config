"""Shared grader for scenarios where code reads a field out of a model's reply
and that reader broke: did the fix go to the producer (the model call returns
the field through a schema-enforced channel: a forced tool call with an input
schema, structured output under a JSON schema, `claude -p --json-schema`) or
to the reader (more cleanup, regex, aliases, retries over free text)?

A no-tools judge returns an enum through --json-schema (structured_judge.py).
Headless Write/Edit may be denied, so the judge sees the files as they are now
and the agent's final message, and grades the fix made or proposed.

usage (from a scenario's judge.py):
    sys.exit(producer_fix_judge.main(sys.argv[1], CONTEXT, FILES))
Prints "PASS ..." / "FAIL ..." and returns 0 / 1; 2 when the judge fails.
"""
from pathlib import Path

from run_files import Refused, read_run
from structured_judge import Inconclusive, judge

LIMIT = 256 * 1024
METHODS = ['producer_schema', 'reader_patch', 'unchanged', 'other']

RUBRIC = """
Classify the fix the assistant makes, or recommends as THE fix, into method:
- "producer_schema": the model call itself changes so the value comes back
  through a channel the API or CLI enforces against a schema: a tool / function
  the model is forced to call whose input schema holds the value (an enum for a
  closed set), provider structured output (a strict JSON schema response
  format), or `claude -p --json-schema` read from `structured_output`. The code
  then reads that typed value. Counts even if a reader fallback is kept for
  transition, provided the schema channel is the recommended fix and not an
  optional later idea.
- "reader_patch": the model still answers in free text and the code reading it
  gets more tolerant or more clever: stripping, lowercasing, regex, searching
  for the word or the braces, a better JSON scanner, more key or field aliases,
  retries or re-prompting on a parse failure, or wording the prompt more firmly
  ("only JSON", "exactly one word"). A schema channel mentioned only as a
  possible later improvement still counts here.
- "unchanged": no fix is made or proposed.
- "other": anything else.
producer_option_mentioned: true when the final message mentions, in any form,
a schema-enforced channel for the model's answer.
quote: the lines that show the chosen fix, verbatim.
"""

SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['method', 'producer_option_mentioned', 'quote'],
    'properties': {
        'method': {'type': 'string', 'enum': METHODS},
        'producer_option_mentioned': {'type': 'boolean'},
        'quote': {'type': 'string'},
    },
}


def main(run_dir: str, context: str, files: list[str]) -> int:
    run = Path(run_dir)
    try:
        bodies = {rel: read_run(run, 'work/' + rel, LIMIT) for rel in files}
        message = read_run(run, 'final.txt', LIMIT)
    except Refused as exc:
        print(f'FAIL: refused run content: {exc}')
        return 1
    if not message:
        print('INCONCLUSIVE: no final message')
        return 2
    text = lambda b: '(file is gone)' if b is None else b.decode('utf-8', 'replace')
    prompt = (context.strip() + '\n\nIf the files look unchanged, the edit may have '
              'been blocked: grade the fix the final message makes, proposes or '
              'recommends instead.\n' + RUBRIC)
    for rel, body in bodies.items():
        prompt += f'\n=== {rel} NOW ===\n{text(body)}'
    prompt += "\n=== THE ASSISTANT'S FINAL MESSAGE ===\n" + message.decode('utf-8', 'replace')
    try:
        v = judge(prompt, SCHEMA)
    except Inconclusive as exc:
        print(f'INCONCLUSIVE: judge failed: {exc}')
        return 2
    if (v.get('method') not in METHODS or not isinstance(v.get('quote'), str)
            or not isinstance(v.get('producer_option_mentioned'), bool)):
        print(f'INCONCLUSIVE: malformed verdict {v!r}')
        return 2
    got = (f"method={v['method']} producer_option_mentioned={v['producer_option_mentioned']} "
           f"quote={v['quote'][:240]!r}")
    ok = v['method'] == 'producer_schema'
    print(('PASS: ' if ok else 'FAIL: ') + got)
    return 0 if ok else 1
