#!/usr/bin/env python3
"""Every test file that can run as a script runs every test it defines.

`python3 tests/test_x.py` runs only the calls in its __main__ block, so a test
added without a call there passes under pytest and silently never runs as a
script. The block must also come last: a call to a test defined below it
raises NameError before the rest run.
"""
from __future__ import annotations

import ast
from pathlib import Path


TESTS = Path(__file__).resolve().parent


def is_main_guard(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
        and len(node.test.comparators) == 1
        and isinstance(node.test.comparators[0], ast.Constant)
        and node.test.comparators[0].value == "__main__"
    )


def runner_problems(path: Path) -> list[str]:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    guards = [index for index, node in enumerate(module.body) if is_main_guard(node)]
    if not guards:
        return []
    problems: list[str] = []
    if guards != [len(module.body) - 1]:
        problems.append(f"{path.name}: the __main__ block must be the last statement")
    defined = {
        node.name
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }
    called = {
        node.func.id
        for guard in guards
        for node in ast.walk(module.body[guard])
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    problems.extend(
        f"{path.name}: __main__ does not run {name}" for name in sorted(defined - called)
    )
    return problems


def test_every_script_runner_runs_every_test_it_defines() -> None:
    problems = [
        problem for path in sorted(TESTS.glob("test_*.py")) for problem in runner_problems(path)
    ]
    assert not problems, "\n".join(problems)


if __name__ == "__main__":
    test_every_script_runner_runs_every_test_it_defines()
    print("runner list tests OK")
