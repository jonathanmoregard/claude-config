"""Regression check for judge.py against hand-written answers with a known
verdict. Runs the real judge model, so it is not part of pytest; run it after
changing judge.py or ../_lib/structured_judge.py:

    python3 -I tests/agent-e2e/codex-render-location/judge_selftest.py

Exits 0 when every case gets its expected exit code (0 PASS, 1 FAIL).
"""
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
CASES = [
    (0, 'Make the change under codex/ in claude-config.'),
    (0, 'Edit the renderer in ~/Repos/claude-config, under codex/ (codex/scripts/render_codex.py). '
        'Do not touch ai-client-config; it is retired.'),
    (1, 'Edit ~/Repos/claude-config, directory runtime/codex/.'),
    (1, 'This lives in claude-config. The renderer output lands in codex/, but the change itself '
        'goes in runtime/hooks/.'),
    (1, 'Edit the ai-client-config repository, in clients/codex/.'),
    (1, 'Edit ~/.claude/codex/scripts/render_codex.py, the codex/ directory of your Claude config.'),
    (1, 'Edit the renderer under ~/.claude/codex/.'),
]


def run(case: tuple[int, str]) -> tuple[int, str, int, str]:
    want, answer = case
    with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False) as fh:
        fh.write(answer + '\n')
    try:
        proc = subprocess.run([sys.executable, '-I', os.path.join(HERE, 'judge.py'), fh.name],
                              capture_output=True, text=True, timeout=600)
    finally:
        os.unlink(fh.name)
    return want, answer, proc.returncode, proc.stdout.strip()


def main() -> int:
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, CASES))
    bad = 0
    for want, answer, got, out in results:
        ok = got == want
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} want={want} got={got} :: {answer[:70]} :: {out[:120]}")
    return 1 if bad else 0


sys.exit(main())
