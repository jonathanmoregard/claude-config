"""Blast-radius declaration and preflight gating.

A resource whose policy demands a declaration is refused until someone has
stated how much traffic this is and what pushback looks like. A resource going
past a threshold of grants is refused until bounded-sample invariant checks
have been recorded — the failure mode being guarded against is a correctness
bug that goes unnoticed on a small run and multiplies external effect on a
large one.
"""

from __future__ import annotations

import pytest

from egress_guard.limiter import EgressGuard, PreflightRequired, Undeclared


def guard(state_dir, policy) -> EgressGuard:
    return EgressGuard(state_dir=state_dir, policy_path=policy)


def test_undeclared_resource_is_refused(state_dir, policy_file):
    policy = policy_file(defaults={"min_interval_s": 0.0, "require_declaration": True})
    with pytest.raises(Undeclared) as exc:
        guard(state_dir, policy).acquire("host.test")
    assert "blast-radius" in str(exc.value)


def test_declaration_unlocks_the_resource(state_dir, policy_file):
    policy = policy_file(defaults={"min_interval_s": 0.0, "require_declaration": True})
    g = guard(state_dir, policy)
    g.declare(
        "host.test",
        0.0,
        expected_requests=600,
        expected_cost=650.0,
        ceiling_requests=900,
        pushback="403 interstitial or 429",
    )
    assert g.acquire("host.test").resource == "host.test"

    declared = g.status("host.test")["declaration"]
    assert declared["expected_requests"] == 600
    assert declared["ceiling_requests"] == 900
    assert "403" in declared["pushback"]


def test_declaration_is_not_required_by_default(state_dir, policy_file):
    """The gate stays usable for ad-hoc work; declaration is opt-in per resource."""
    policy = policy_file(defaults={"min_interval_s": 0.0})
    assert guard(state_dir, policy).acquire("host.test").resource == "host.test"


def test_preflight_required_past_the_threshold(state_dir, policy_file):
    policy = policy_file(
        defaults={"min_interval_s": 0.0, "preflight_after_requests": 3}
    )
    g = guard(state_dir, policy)
    for _ in range(3):
        g.acquire("host.test")
    with pytest.raises(PreflightRequired) as exc:
        g.acquire("host.test")
    assert "bounded sample" in str(exc.value)


def test_recorded_preflight_unlocks_the_wider_run(state_dir, policy_file):
    policy = policy_file(
        defaults={"min_interval_s": 0.0, "preflight_after_requests": 3}
    )
    g = guard(state_dir, policy)
    for _ in range(3):
        g.acquire("host.test")
    g.record_preflight(
        "host.test",
        checks=[
            {"name": "short page mid-window is not end-of-data", "passed": True},
            {"name": "offset cap triggers a split", "passed": True},
            {"name": "server total over-counts filtered rows", "passed": True},
            {"name": "empty window terminates", "passed": True},
            {"name": "boundary duplicates are deduped", "passed": True},
        ],
        sample_requests=12,
    )
    assert g.acquire("host.test").resource == "host.test"
    assert g.status("host.test")["preflight"]["sample_requests"] == 12


def test_failing_preflight_is_refused_not_stored(state_dir, policy_file):
    policy = policy_file(
        defaults={"min_interval_s": 0.0, "preflight_after_requests": 1}
    )
    g = guard(state_dir, policy)
    with pytest.raises(ValueError) as exc:
        g.record_preflight(
            "host.test",
            checks=[
                {"name": "short page mid-window is not end-of-data", "passed": False},
                {"name": "empty window terminates", "passed": True},
            ],
            sample_requests=5,
        )
    assert "short page" in str(exc.value)
    assert g.status("host.test")["preflight"] is None


def test_empty_preflight_is_refused(state_dir, policy_file):
    policy = policy_file(defaults={"min_interval_s": 0.0})
    with pytest.raises(ValueError):
        guard(state_dir, policy).record_preflight("host.test", checks=[], sample_requests=0)


def test_declaration_still_ratchets_the_floor(state_dir, policy_file):
    """Declaring a blast radius must not weaken the cadence guarantee."""
    policy = policy_file(defaults={"min_interval_s": 0.0})
    g = guard(state_dir, policy)
    g.declare("host.test", 9.0, expected_requests=10)
    assert g.declare("host.test", 0.1, expected_requests=20) == pytest.approx(9.0)
    assert g.status("host.test")["floor_s"] == pytest.approx(9.0)
    # The later declaration's own fields still land.
    assert g.status("host.test")["declaration"]["expected_requests"] == 20
