"""Shared fixtures for the egress-guard test suite, and the script-suite hook.

No test in this suite performs a network call. Cadence is exercised with a
fake clock (`FakeClock`); cross-process behaviour is exercised with real
subprocesses that talk to a temp state directory, never to a host.

The second half of this file is unrelated to egress-guard and applies to the
whole `tests/` tree: it teaches pytest to run this repository's self-running
script suites. See the comment above `pytest_pycollect_makemodule`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class FakeClock:
    """Deterministic clock whose `sleep` advances time instead of blocking.

    `slept` records every positive sleep the code under test asked for, so a
    test can assert that a refusal path (cooldown, budget) never slept.
    """

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = float(start)
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self.slept.append(float(seconds))
        self.now += float(seconds)

    @property
    def total_slept(self) -> float:
        return sum(self.slept)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# Permissive-by-default test policy: floors and budgets are set per test so
# that an unrelated limit never masks the behaviour under test.
TEST_DEFAULTS = {
    "min_interval_s": 0.1,
    "window_s": 3600.0,
    "max_requests": 100000,
    "max_cost": 1e9,
    "max_wait_s": 3600.0,
    "block_cooldown_s": 86400.0,
}


def write_policy(path, defaults=None, resources=None) -> str:
    """Write a policy file and return its path as a str."""
    doc = {
        "defaults": dict(TEST_DEFAULTS, **(defaults or {})),
        "resources": resources or {},
    }
    path = str(path)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
    return path


@pytest.fixture
def policy_file(tmp_path):
    """Factory: `policy_file(defaults=..., resources=...)` -> path str."""

    def _write(defaults=None, resources=None):
        return write_policy(tmp_path / "policy.json", defaults, resources)

    return _write


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return str(d)


# ---------------------------------------------------------------------------
# Script suites
# ---------------------------------------------------------------------------
# This repository has two kinds of python test file:
#
#   * pytest modules, which define `test_*` functions;
#   * self-running scripts, which run every check at IMPORT time and whose
#     exit status is the verdict. `tests/smoke.sh` has always invoked these as
#     `python3 tests/<module>.py`, by discovery rather than by a list.
#
# Pointing pytest at the tree used to mishandle the second kind in three
# different ways, all of them silent or worse: a module that merely collects
# nothing reports in a way that reads as success; one that ends in a
# module-level `sys.exit(0)` raises `INTERNALERROR` during collection and
# aborts the ENTIRE run, taking every other suite's result with it; and one
# that shells out at import time runs its whole body just to be collected.
#
# Those modules now stop themselves at the top with
# `pytest.skip("script suite: ...", allow_module_level=True)`, so importing one
# does no work. This hook then gives each of them back as one real test item
# that runs the file the way it is meant to be run -- as a script, in a
# subprocess -- and fails with the script's own FAIL lines.
#
# The point is that CI needs no list of these files and no second step. The
# alternative, a hand-maintained enumeration in the workflow, is the exact
# thing `.github/workflows/checks.yml` and `tests/smoke.sh` each argue against
# at length: a list goes stale the first time somebody adds a suite, and the
# failure mode of a stale list is silence.
#
# A `live suite:` gate is deliberately NOT matched here. Those modules drive a
# real paid CLI, and they must stay skipped under pytest while `smoke.sh`
# continues to run them standalone.
SCRIPT_SUITE_SENTINEL = 'pytest.skip("script suite:'

# Well under the `checks` job's own timeout-minutes, so a hung suite is
# reported as a failed test rather than as a killed job. The slowest script
# suite in the tree runs in about three minutes.
SCRIPT_SUITE_TIMEOUT_S = 900


def pytest_pycollect_makemodule(module_path, parent):
    """Collect a gated script suite as a ScriptSuite instead of importing it."""
    try:
        text = module_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if SCRIPT_SUITE_SENTINEL in text:
        return ScriptSuite.from_parent(parent, path=module_path)
    return None


class ScriptSuite(pytest.File):
    def collect(self):
        yield ScriptRun.from_parent(self, name="script")


class ScriptFailed(Exception):
    """Carries the script's own output, so the report is the script's report."""


class ScriptRun(pytest.Item):
    def runtest(self):
        proc = subprocess.run(
            [sys.executable, str(self.path)],
            capture_output=True, text=True, timeout=SCRIPT_SUITE_TIMEOUT_S,
        )
        if proc.returncode != 0:
            # These suites do not agree on a failure prefix -- `FAIL`, `FAIL:`
            # and `[FAIL]` all occur -- so match the bare word and keep a tail
            # of raw output for whatever this misses.
            fails = [ln for ln in proc.stdout.splitlines() if "FAIL" in ln]
            raise ScriptFailed(
                "exit %d; %d line(s) mentioning FAIL:\n%s\n--- tail ---\n%s"
                % (proc.returncode, len(fails), "\n".join(fails[:40]),
                   (proc.stdout + proc.stderr)[-2000:]))

    def repr_failure(self, excinfo, style=None):
        if isinstance(excinfo.value, ScriptFailed):
            return str(excinfo.value)
        return super().repr_failure(excinfo)

    def reportinfo(self):
        return self.path, None, "%s (run as script)" % self.path.name
