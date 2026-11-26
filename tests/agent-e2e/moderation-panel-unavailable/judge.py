"""Judge for moderation-panel-unavailable (see ../_lib/producer_fix_judge.py).

usage: judge.py RUN
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_lib'))
try:
    import producer_fix_judge
except Exception as exc:  # a grader that cannot load is no verdict, never a FAIL
    print(f'INCONCLUSIVE: grader cannot load: {exc.__class__.__name__}: {exc}')
    sys.exit(2)

CONTEXT = """You grade one fix to a Python moderation service. A panel of three
models (claude-haiku-4-5 through the Anthropic Messages API; gpt-4o-mini and
gpt-4.1-mini through OpenAI chat completions) is told "Respond with exactly one
word: keep or remove", and _parse_vote() strips and lowercases the reply and
maps it to keep / remove / unavailable. gpt-4.1-mini started answering
"Verdict: keep", "**keep**", "Keep\\n\\nThe comment", so its vote is
"unavailable" and clean comments are held. An assistant was asked to fix it.
Below are the files as they are now and the assistant's final message."""

FILES = ['moderation/panel.py', 'tests/test_parse_vote.py']

try:
    sys.exit(producer_fix_judge.main(sys.argv[1], CONTEXT, FILES))
except Exception as exc:  # a grader crash is no verdict: never let it read as FAIL (exit 1)
    print(f'INCONCLUSIVE: grader error: {exc.__class__.__name__}: {exc}')
    sys.exit(2)
