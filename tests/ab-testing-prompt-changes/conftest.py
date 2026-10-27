"""Lets pytest run a suite written as a standalone script.

`test_ab_harness.py` has its own `main()` that hands each test a fresh
temp directory as `root` (`python3 test_ab_harness.py bug1`). Under
`pytest tests/` that parameter is a fixture request instead, so declare it.
`_root` is the same thing under the name the tests that ignore it use.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def _root(tmp_path: Path) -> Path:
    return tmp_path
