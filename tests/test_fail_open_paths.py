"""Regression tests for ways the guard could silently stop guarding.

Each of these was a live fail-open: the limit still looked configured, the call
still succeeded, and nothing reported that the ceiling had stopped applying.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conftest import REPO_ROOT
from egress_guard.limiter import (
    BudgetExhausted,
    CooldownActive,
    EgressGuard,
    PolicyError,
)


def guard(state_dir, policy, **kw) -> EgressGuard:
    return EgressGuard(state_dir=state_dir, policy_path=policy, **kw)


# -- non-finite inputs -----------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_cost_is_refused(state_dir, policy_file, bad):
    """NaN loses every comparison, so it would sail past the cost ceiling."""
    policy = policy_file(defaults={"min_interval_s": 0.0, "max_cost": 1.0})
    g = guard(state_dir, policy)
    with pytest.raises(ValueError):
        g.acquire("host.test", cost=bad)
    # And the ceiling still works afterwards — nothing was persisted.
    g.acquire("host.test", cost=1.0)
    with pytest.raises(BudgetExhausted):
        g.acquire("host.test", cost=1.0)


def test_non_finite_min_interval_is_refused(state_dir, policy_file):
    policy = policy_file(defaults={"min_interval_s": 0.0})
    with pytest.raises(ValueError):
        guard(state_dir, policy).acquire("host.test", min_interval_s=float("nan"))


def test_non_finite_policy_value_fails_closed(state_dir, tmp_path):
    """`json.load` parses the bare token NaN; a NaN limit is an absent limit."""
    path = tmp_path / "policy.json"
    path.write_text(
        '{"defaults": {"min_interval_s": NaN, "window_s": 3600, "max_requests": 10, '
        '"max_cost": 10, "max_wait_s": 30, "block_cooldown_s": 86400}, "resources": {}}',
        encoding="utf-8",
    )
    with pytest.raises(PolicyError) as exc:
        guard(state_dir, str(path)).acquire("host.test")
    assert "finite" in str(exc.value)


# -- the ratchet must survive state loss -----------------------------------


def test_corrupt_state_does_not_lower_a_ratcheted_floor(state_dir, policy_file):
    """Losing history must never make us faster than a participant demanded."""
    policy = policy_file(defaults={"min_interval_s": 1.0})
    g = guard(state_dir, policy)
    g.declare("host.test", 30.0)
    assert g.status("host.test")["floor_s"] == pytest.approx(30.0)

    with open(g.state_path("host.test"), "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")

    # The counters are gone, but the floor a slow participant declared is not.
    assert g.status("host.test")["floor_s"] == pytest.approx(30.0)


def test_deleted_state_does_not_lower_a_ratcheted_floor(state_dir, policy_file):
    policy = policy_file(defaults={"min_interval_s": 1.0})
    g = guard(state_dir, policy)
    g.declare("host.test", 25.0)
    os.unlink(g.state_path("host.test"))
    assert g.status("host.test")["floor_s"] == pytest.approx(25.0)


# -- pushback while a call is parked ---------------------------------------


def test_block_recorded_while_parked_cancels_the_grant(state_dir, policy_file):
    """A slot reserved before pushback must not still fire afterwards."""
    policy = policy_file(defaults={"min_interval_s": 5.0, "max_wait_s": 30.0})
    g = guard(state_dir, policy)
    g.acquire("host.test")  # takes the first slot, no wait

    # Second acquire will park for ~5s; block lands while it waits.
    blocker = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys,time;sys.path.insert(0,%r)\n"
                "from egress_guard.limiter import EgressGuard\n"
                "time.sleep(1.0)\n"
                "EgressGuard(state_dir=%r,policy_path=%r).record_block("
                "'host.test', reason='bot challenge')\n"
            )
            % (REPO_ROOT, state_dir, policy),
        ],
        cwd=REPO_ROOT,
    )
    try:
        with pytest.raises(CooldownActive) as exc:
            g.acquire("host.test")
        assert "bot challenge" in str(exc.value)
    finally:
        blocker.wait(timeout=30)


# -- cross-process ratchet propagation -------------------------------------


def test_declared_floor_propagates_to_other_processes(state_dir, policy_file):
    """A stricter declaration in one process must slow every other process.

    The policy floor is deliberately tiny here, so this passes only if the
    DECLARED interval is what spaces the requests.
    """
    policy = policy_file(defaults={"min_interval_s": 0.01, "max_wait_s": 60.0})
    guard(state_dir, policy).declare("host.test", 2.0)

    code = (
        "import sys;sys.path.insert(0,%r)\n"
        "from egress_guard.limiter import EgressGuard\n"
        "g=EgressGuard(state_dir=%r,policy_path=%r)\n"
        "for _ in range(2):\n"
        "    print(g.acquire('host.test', min_interval_s=0.01).slot_at, flush=True)\n"
    ) % (REPO_ROOT, state_dir, policy)

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code], cwd=REPO_ROOT, stdout=subprocess.PIPE, text=True
        )
        for _ in range(2)
    ]
    slots = sorted(float(line) for p in procs for line in p.communicate()[0].split())
    gaps = [b - a for a, b in zip(slots, slots[1:])]
    assert gaps, "expected multiple slots"
    assert min(gaps) >= 2.0 - 1e-6, f"declared floor did not propagate: {gaps}"


def test_registered_server_carries_no_policy_or_state_override():
    """The env vars that could point the gate at a permissive policy stay unset."""
    path = os.path.join(REPO_ROOT, "mcp-servers.json")
    with open(path, "r", encoding="utf-8") as fh:
        registry = json.load(fh)
    entry = registry["egress-guard"]
    assert entry["env"] == {}, "a registered override is a standing bypass"
    assert any("egress-guard/server.py" in a for a in entry["args"])


def test_shipped_policy_documents_every_honoured_key():
    """A key the limiter honours but the policy file never mentions is invisible."""
    path = os.path.join(REPO_ROOT, "mcps", "egress-guard", "egress-policy.json")
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    readme = " ".join(doc.get("_README") or [])
    for key in ("require_declaration", "preflight_after_requests"):
        assert key in readme, f"{key} is honoured by the limiter but undocumented"
