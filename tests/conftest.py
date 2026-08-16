"""Shared fixtures for the egress-guard test suite.

No test in this suite performs a network call. Cadence is exercised with a
fake clock (`FakeClock`); cross-process behaviour is exercised with real
subprocesses that talk to a temp state directory, never to a host.
"""
from __future__ import annotations

import json
import os
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
