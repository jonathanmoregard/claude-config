"""Judge for fix-producer-verdict-channel: when the reviewer's free-text JSON
verdict stopped reaching the hook because the delivery channel changed, did
the fix go to the producer (the reviewer submits its verdict through a
schema-enforced tool call or structured-output channel the hook reads as a
typed record) or to the consumer (the hook scrapes the verdict text out of the
new channel)? A no-tools judge returns an enum through --json-schema
(../_lib/structured_judge.py).

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

METHODS = ['producer_structured', 'consumer_scrape', 'unchanged', 'other']

INSTRUCTIONS = """You grade one fix in an agent-config repo. A reviewer subagent
(runtime/agents/reviewer.md) was told to end with a JSON verdict as its final
free-text message; a SubagentStop hook (runtime/hooks/record_verdict.py)
json.loads() that text out of the payload's last_assistant_message. The
harness changed: background subagents now end with a SubagentHandback tool
call carrying the text in its `message` input, last_assistant_message is
empty, and nothing is recorded. An assistant was asked to fix it. Below are
both files as they are now and the assistant's final message. If the files
look unchanged, the edit may have been blocked: grade the fix the final
message makes, proposes or recommends instead.

method — classify the fix the assistant makes or recommends as THE fix:
- "producer_structured": the reviewer stops delivering its verdict as free
  text; it submits it through a schema-enforced channel the repo controls — a
  dedicated tool call (e.g. an MCP tool such as submit_verdict whose input
  schema is the verdict), or structured output under a JSON schema — and the
  hook/gate reads that typed record (the tool's input or a file the tool
  writes), not a message's text. Counts even if a temporary read of the new
  channel is kept for transition, provided the producer change is the
  recommended fix, not an optional aside.
- "consumer_scrape": the reviewer still emits free text and the hook reads it
  from wherever it now arrives: the SubagentHandback message, the transcript,
  agent_transcript_path, a PostToolUse payload, a fallback chain of channels,
  or extra text cleanup. A producer-side tool mentioned only as a possible
  later improvement still counts here.
- "unchanged": no fix is made or proposed.
- "other": anything else.
producer_option_mentioned: true when the final message mentions, in any form,
making the reviewer submit the verdict through a tool call or schema-enforced
structured output.
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


def main() -> int:
    run = Path(sys.argv[1])
    try:
        agent = read_run(run, 'work/runtime/agents/reviewer.md', LIMIT)
        hook = read_run(run, 'work/runtime/hooks/record_verdict.py', LIMIT)
        message = read_run(run, 'final.txt', LIMIT)
    except Refused as exc:
        print(f'FAIL: refused run content: {exc}')
        return 1
    if not message:
        print('INCONCLUSIVE: no final message')
        return 2
    text = lambda b: '(file is gone)' if b is None else b.decode('utf-8', 'replace')
    prompt = (INSTRUCTIONS + '\n=== runtime/agents/reviewer.md NOW ===\n' + text(agent)
              + '\n=== runtime/hooks/record_verdict.py NOW ===\n' + text(hook)
              + "\n=== THE ASSISTANT'S FINAL MESSAGE ===\n" + message.decode('utf-8', 'replace'))
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
    print(('PASS: ' if v['method'] == 'producer_structured' else 'FAIL: ') + got)
    return 0 if v['method'] == 'producer_structured' else 1


try:
    sys.exit(main())
except Exception as exc:  # a grader crash is no verdict: never let it read as FAIL (exit 1)
    print(f'INCONCLUSIVE: grader error: {exc.__class__.__name__}: {exc}')
    sys.exit(2)
