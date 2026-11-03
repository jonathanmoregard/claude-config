#!/usr/bin/env python3
"""Behavioural test: run_secret_scan() redacts through the REAL detect-secrets.

permission-ledger aggregates transcript-derived tool-call samples into proposal
files that a pusher later ships to GitHub. `run_secret_scan` in
runtime/permission-ledger/aggregate.py is the FIRST redaction gate on that path.
When `detect-secrets` is not on PATH it returns "unavailable: ..." and emits
every sample unredacted, so the gate silently no-ops.

tests/permission-ledger/test_aggregate.py only ever stubs a FAKE `detect-secrets`
on PATH. Nothing else proves that the real scanner, with the real parsing of its
real output, causes redaction. That parsing is version-sensitive: `_DETECTOR_HIT`
matches `:\\s*True\\b` because 1.5.0 prints `AWSKeyDetector          : True
(unverified)` (padded, with a qualifier after True). An output-format change in
detect-secrets would turn redaction into a no-op with every health signal clean,
and only a test against the real binary can see that.

So this suite goes through `run_secret_scan` with the real binary (it does not
re-implement the match), and asserts both directions: a credential-shaped sample
is replaced, and a benign group is left alone (so a pass is not "redact all").

detect-secrets absent is a FAILURE, never a skip -- same stance as
test_proposals_gitleaks_behaviour.py. The `checks` job provisions it; a guard
that passes on a machine that cannot run the scanner is no guard.

Run:  nix shell nixpkgs#python3Packages.pytest nixpkgs#detect-secrets -c pytest tests/enforced -q
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# The file moved under runtime/ in the layout cutover; accept either location.
_CANDIDATES = (REPO_ROOT / "runtime" / "permission-ledger" / "aggregate.py",
               REPO_ROOT / "permission-ledger" / "aggregate.py")
AGGREGATE = next((p for p in _CANDIDATES if p.exists()), None)

# Obviously synthetic: an AWS-access-key-shaped ID (AKIA + 16 upper/digit chars)
# that nobody issued. Assembled at run time from parts so the repo's own
# commit-time gitleaks hook does not (correctly) flag this file for containing a
# credential-shaped literal.
_PARTS = ("AKIA", "QYLP", "MB5H", "2XDW", "K7RT")
CREDENTIAL = "".join(_PARTS)
SECRET_SAMPLE = "export AWS_ACCESS_KEY_ID=" + CREDENTIAL
BENIGN_SAMPLES = ["git status --short", "ls -la /tmp", "pytest tests/enforced -q"]


@pytest.fixture(scope="module")
def detect_secrets() -> str:
    path = shutil.which("detect-secrets")
    if path is None:
        pytest.fail(
            "detect-secrets is not on PATH. This suite does not skip: a "
            "secret-scanner guard that passes on a machine that cannot run the "
            "scanner is no guard (run_secret_scan itself degrades to emitting "
            "samples unredacted). The `checks` workflow provisions it; locally "
            "use `nix shell nixpkgs#detect-secrets`.", pytrace=False)
    return path


@pytest.fixture(scope="module")
def aggregate(detect_secrets):
    assert AGGREGATE is not None, "permission-ledger/aggregate.py not found"
    # The directory name has a hyphen, so it is not an importable package path.
    spec = importlib.util.spec_from_file_location("ledger_aggregate", AGGREGATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_real_shaped_credential_sample_is_redacted(aggregate):
    groups = [{"samples": [SECRET_SAMPLE]}]
    status = aggregate.run_secret_scan(groups)
    assert status.startswith("ok (redacted 1 sample(s)"), (
        f"status was {status!r}; the real detect-secrets did not flag an "
        "AWS-key-shaped sample (or was not run). If the status says "
        "`unavailable`, the binary is missing; otherwise the output format "
        "changed and _DETECTOR_HIT no longer matches it.")
    assert groups[0]["samples"] == [aggregate.SECRET_PLACEHOLDER]
    assert CREDENTIAL not in repr(groups)


def test_a_benign_group_passes_through_unchanged(aggregate):
    # Counterpart: proves the redaction above is selective, not indiscriminate.
    groups = [{"samples": list(BENIGN_SAMPLES)}]
    status = aggregate.run_secret_scan(groups)
    assert status == "ok"
    assert groups[0]["samples"] == BENIGN_SAMPLES


def test_only_the_credential_sample_is_replaced_in_a_mixed_group(aggregate):
    groups = [{"samples": [BENIGN_SAMPLES[0], SECRET_SAMPLE, BENIGN_SAMPLES[1]]}]
    status = aggregate.run_secret_scan(groups)
    assert status.startswith("ok (redacted 1 sample(s)"), status
    assert groups[0]["samples"] == [BENIGN_SAMPLES[0], aggregate.SECRET_PLACEHOLDER,
                                    BENIGN_SAMPLES[1]]
