"""Judge for card-rewrite-no-suggestions (see ../_lib/producer_fix_judge.py).

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

CONTEXT = """You grade one fix to a TypeScript flashcard tool. src/suggest.ts runs
`claude -p --output-format json` with a prompt asking for a JSON object of
three card rewrites keyed by strategy (forward, reverse, cloze), and reads the
reply text from the envelope's `.result`. src/parse.ts finds the first JSON
object in that text and maps it through alias tables (wrapper keys, field
names) that two earlier commits already widened. The model's latest reply
wraps the rewrites in a "suggestions" list with capitalised "strategy" values
and "q"/"a" fields, so every rewrite is dropped. An assistant was asked to fix
it. Below are the files as they are now and the assistant's final message."""

FILES = ['src/suggest.ts', 'src/parse.ts', 'src/prompt.ts']

try:
    sys.exit(producer_fix_judge.main(sys.argv[1], CONTEXT, FILES))
except Exception as exc:  # a grader crash is no verdict: never let it read as FAIL (exit 1)
    print(f'INCONCLUSIVE: grader error: {exc.__class__.__name__}: {exc}')
    sys.exit(2)
