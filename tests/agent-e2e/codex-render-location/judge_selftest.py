"""Regression check for judge.py against hand-written answers with a known
verdict: each case pins the repo and directory fields the judge must return.
Runs the real judge model, so it is not part of pytest; run it after changing
judge.py or ../_lib/structured_judge.py:

    python3 -I tests/agent-e2e/codex-render-location/judge_selftest.py

Exits 0 when every case gets its expected fields; 2 when the judge fails.
"""
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_lib'))
from judge import grade, passes  # noqa: E402
from structured_judge import Inconclusive  # noqa: E402

# (expected repo, expected directory, answer)
CASES = [
    ('claude-config', 'codex', 'Make the change under codex/ in claude-config.'),
    ('claude-config', 'codex',
     'Edit the renderer in ~/Repos/claude-config, under codex/ (codex/scripts/render_codex.py). '
     'Do not touch ai-client-config; it is retired.'),
    ('claude-config', 'runtime', 'Edit ~/Repos/claude-config, directory runtime/codex/.'),
    ('claude-config', 'runtime',
     'This lives in claude-config. The renderer output lands in codex/, but the change itself '
     'goes in runtime/hooks/.'),
    ('ai-client-config', 'other', 'Edit the ai-client-config repository, in clients/codex/.'),
    ('live-claude-dir', 'codex',
     'Edit ~/.claude/codex/scripts/render_codex.py, the codex/ directory of your Claude config.'),
    ('live-claude-dir', 'codex', 'Edit the renderer under ~/.claude/codex/.'),
]


def run(case):
    repo, directory, answer = case
    try:
        return case, grade(answer + '\n')
    except Inconclusive as exc:
        return case, exc


def main() -> int:
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, CASES))
    bad = failed = 0
    for (repo, directory, answer), v in results:
        if isinstance(v, Inconclusive):
            failed += 1
            print(f'ERR judge failed: {v} :: {answer[:70]}')
            continue
        ok = (v['repo'], v['directory']) == (repo, directory)
        ok = ok and passes(v) == ((repo, directory) == ('claude-config', 'codex'))
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} want={repo}/{directory} got={v['repo']}/{v['directory']} :: {answer[:70]}")
    return 2 if failed else (1 if bad else 0)


sys.exit(main())
