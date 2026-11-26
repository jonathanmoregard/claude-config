"""Judge for eval-judge-extra-data (see ../_lib/producer_fix_judge.py).

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

CONTEXT = """You grade one fix to a Python eval harness. evals/judge.py runs an
LLM judge as `claude -p` (plain text output) with a prompt asking it to
"Return ONLY a JSON object" of rubric scores, then parse_judge_output() pulls
the object out of stdout with a greedy first-brace-to-last-brace regex and
json.loads it.
The judge sometimes appended a note containing braces after the object, the
greedy match ran past it, and json.loads raised "Extra data", aborting the
run. An assistant was asked to fix it. Below are the files as they are now and
the assistant's final message."""

FILES = ['evals/judge.py', 'evals/rubric.py']

try:
    sys.exit(producer_fix_judge.main(sys.argv[1], CONTEXT, FILES))
except Exception as exc:  # a grader crash is no verdict: never let it read as FAIL (exit 1)
    print(f'INCONCLUSIVE: grader error: {exc.__class__.__name__}: {exc}')
    sys.exit(2)
