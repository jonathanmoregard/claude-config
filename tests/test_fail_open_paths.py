"""Regression tests for ways the guard could silently stop guarding.

Each of these was a live fail-open: the limit still looked configured, the call
still succeeded, and nothing reported that the ceiling had stopped applying.
"""

from __future__ import annotations

import json
import math
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
    WaitTooLong,
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


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_cooldown_is_refused(state_dir, policy_file, bad):
    """NaN cooldown recorded silently as NO cooldown: pushback dropped."""
    policy = policy_file(defaults={"min_interval_s": 0.0})
    g = guard(state_dir, policy)
    with pytest.raises(ValueError):
        g.record_block("host.test", reason="bot challenge", cooldown_s=bad)
    # A real block still refuses afterwards.
    g.record_block("host.test", reason="bot challenge", cooldown_s=3600)
    with pytest.raises(CooldownActive):
        g.acquire("host.test")


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_declared_interval_is_refused(state_dir, policy_file, bad):
    """An inf floor would wedge the resource forever, and persist in the sidecar."""
    policy = policy_file(defaults={"min_interval_s": 0.5})
    g = guard(state_dir, policy)
    with pytest.raises(ValueError):
        g.declare("host.test", bad)
    assert g.status("host.test")["floor_s"] == pytest.approx(0.5)
    assert not os.path.exists(g.floor_path("host.test"))


@pytest.mark.parametrize("truthy", ["false", "no", 0.0, 1, [], {}])
def test_preflight_passed_must_be_a_real_boolean(state_dir, policy_file, truthy):
    """`not check["passed"]` would read the string "false" as a pass."""
    policy = policy_file(defaults={"min_interval_s": 0.0, "preflight_after_requests": 1})
    g = guard(state_dir, policy)
    with pytest.raises(ValueError):
        g.record_preflight(
            "host.test",
            checks=[{"name": "short page is not end-of-data", "passed": truthy}],
            sample_requests=1,
        )
    assert g.status("host.test")["preflight"] is None


def test_zero_cooldown_is_refused(state_dir, policy_file):
    """A zero cooldown persists a reason and refuses nobody."""
    policy = policy_file(defaults={"min_interval_s": 0.0})
    g = guard(state_dir, policy)
    with pytest.raises(ValueError):
        g.record_block("host.test", reason="bot challenge", cooldown_s=0)
    g.acquire("host.test")  # not blocked, and nothing claimed otherwise


@pytest.mark.parametrize(
    "field,value",
    [
        ("preflight_after_requests", 0.5),
        ("max_requests", 10.5),
    ],
)
def test_policy_counts_are_validated(state_dir, policy_file, field, value):
    """A fractional count truncates toward zero, silently disabling the limit."""
    policy = policy_file(defaults={"min_interval_s": 0.0, field: value})
    with pytest.raises(PolicyError):
        guard(state_dir, policy).acquire("host.test")


def test_require_declaration_must_be_boolean(state_dir, policy_file):
    policy = policy_file(defaults={"min_interval_s": 0.0, "require_declaration": "yes"})
    with pytest.raises(PolicyError):
        guard(state_dir, policy).acquire("host.test")


def _poison_state(g, resource, **fields):
    """Rewrite a resource's state file the way an older version or bad flush might."""
    path = g.state_path(resource)
    with open(path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    state.update(fields)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh)


def test_nan_cooldown_in_state_still_refuses(state_dir, policy_file):
    """State is untrusted input: NaN there would make the cooldown check False."""
    policy = policy_file(defaults={"min_interval_s": 0.0})
    g = guard(state_dir, policy)
    g.record_block("host.test", reason="bot challenge", cooldown_s=3600)
    _poison_state(g, "host.test", cooldown_until=float("nan"))

    # A cooldown we cannot read is not an absent cooldown: falling back to 0.0
    # would forget a recorded block and grant.
    status = g.status("host.test")
    assert not math.isnan(status["cooldown_until"])
    with pytest.raises(CooldownActive):
        g.acquire("host.test")


def test_nan_event_cost_in_state_does_not_disable_the_ceiling(state_dir, policy_file):
    policy = policy_file(defaults={"min_interval_s": 0.0, "max_cost": 1.0})
    g = guard(state_dir, policy)
    g.acquire("host.test", cost=0.5)
    _poison_state(g, "host.test", events=[[g.status("host.test")["window_s"], float("nan")]])

    with pytest.raises(BudgetExhausted):
        g.acquire("host.test", cost=5.0)


def test_nan_last_slot_in_state_does_not_grant_immediately(state_dir, policy_file):
    """max(now, NaN + floor) == now would hand out a slot with no wait."""
    policy = policy_file(defaults={"min_interval_s": 30.0, "max_wait_s": 1.0})
    g = guard(state_dir, policy)
    g.acquire("host.test")
    _poison_state(g, "host.test", last_slot=float("nan"))

    with pytest.raises(WaitTooLong):
        g.acquire("host.test")


def test_recorded_block_survives_whole_file_corruption(state_dir, policy_file):
    """Losing the state file must not turn a 24h block into one floor wait."""
    policy = policy_file(defaults={"min_interval_s": 5.0})
    g = guard(state_dir, policy)
    g.record_block("host.test", reason="bot challenge", cooldown_s=86400)

    with open(g.state_path("host.test"), "w", encoding="utf-8") as fh:
        fh.write("{ truncated mid-write")

    with pytest.raises(CooldownActive) as exc:
        g.acquire("host.test")
    assert "bot challenge" in str(exc.value)


def test_recorded_block_survives_state_deletion(state_dir, policy_file):
    policy = policy_file(defaults={"min_interval_s": 5.0})
    g = guard(state_dir, policy)
    g.record_block("host.test", reason="429 from origin", cooldown_s=3600)
    os.unlink(g.state_path("host.test"))
    with pytest.raises(CooldownActive):
        g.acquire("host.test")


def test_shipped_policy_arms_both_gates_for_the_incident_host():
    """A gate that ships disabled is documentation, not a gate."""
    path = os.path.join(REPO_ROOT, "mcps", "egress-guard", "egress-policy.json")
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    assert doc["defaults"]["require_declaration"] is True
    assert doc["defaults"]["preflight_after_requests"] > 0
    for name, override in (doc.get("resources") or {}).items():
        assert override.get("require_declaration", True) is True, name
        assert override.get("preflight_after_requests", 1) > 0, name


@pytest.mark.parametrize("field", ["window_s", "block_cooldown_s"])
def test_zero_disabling_policy_values_are_refused(state_dir, policy_file, field):
    policy = policy_file(defaults={"min_interval_s": 0.0, field: 0})
    with pytest.raises(PolicyError):
        guard(state_dir, policy).acquire("host.test")


def test_package_exports_every_public_name():
    """`from egress_guard import Undeclared` must not ImportError."""
    import egress_guard
    from egress_guard import limiter

    for name in limiter.__all__:
        assert hasattr(egress_guard, name), f"{name} missing from package exports"


def test_sidecar_write_failure_is_not_swallowed(state_dir, policy_file, monkeypatch):
    """A silent sidecar failure means the ratchet backup does not exist."""
    policy = policy_file(defaults={"min_interval_s": 0.5})
    g = guard(state_dir, policy)

    real_replace = os.replace

    def boom(src, dst):
        if str(dst).endswith(".floor"):
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr("egress_guard.limiter.os.replace", boom)
    with pytest.raises(OSError):
        g.declare("host.test", 30.0)


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
