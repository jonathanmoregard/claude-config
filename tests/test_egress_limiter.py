"""Tests for the cross-process floor-cadence egress limiter.

The headline case is the first two tests: a faithful reimplementation of the
prototype's per-caller throttle is shown to collapse the aggregate cadence to
the fastest participant's delay, and the limiter is required not to.

No network. Cadence uses a fake clock; cross-process cases use subprocesses
against a temp state dir.
"""
from __future__ import annotations

import inspect
import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from conftest import REPO_ROOT, FakeClock  # noqa: E402

from egress_guard.limiter import (  # noqa: E402
    BudgetExhausted,
    CooldownActive,
    EgressGuard,
    EgressGuardError,
    PolicyError,
    WaitTooLong,
)

RESOURCE = "forum.example.test"


def make_guard(state_dir, policy_path, clock) -> EgressGuard:
    return EgressGuard(
        state_dir=state_dir,
        policy_path=policy_path,
        time_fn=clock.time,
        sleep_fn=clock.sleep,
    )


def gaps(times: list[float]) -> list[float]:
    return [round(b - a, 6) for a, b in zip(times, times[1:])]


# ---------------------------------------------------------------------------
# 1. The confirmed live bug
# ---------------------------------------------------------------------------


def _prototype_throttle(shared: dict, delay: float, clock: FakeClock) -> float:
    """Faithful reimplementation of the prototype's `Client._throttle`.

    Reference only (`ea_gql.py`): under a shared lock it waits the CALLER'S
    `delay` since any other process's timestamp, then re-stamps. That is the
    bug — a fast participant re-stamps early and everyone else's floor is
    silently reset to the fast one's delay.
    """
    last = shared.get("last", 0.0)
    wait = delay - (clock.time() - last)
    if wait > 0:
        clock.sleep(wait)
    shared["last"] = clock.time()
    return clock.time()


def test_prototype_caller_delay_algorithm_reproduces_the_incident(clock):
    """Characterization: the prototype's algorithm DOES collapse to 0.4s gaps.

    Pins the scenario as discriminating. If the limiter ever regresses to
    caller-delay semantics, the next test starts failing for this reason.
    """
    shared: dict = {}
    stamps = []
    for i in range(8):
        delay = 5.0 if i % 2 == 0 else 0.4
        stamps.append(_prototype_throttle(shared, delay, clock))

    observed = gaps(stamps)
    assert min(observed) == pytest.approx(0.4), observed
    assert any(g < 1.0 for g in observed), observed
    # ~1 req/s aggregate instead of the declared 1 req / 5s.
    span = stamps[-1] - stamps[0]
    assert span < 5.0 * (len(stamps) - 1) / 2


def test_mixed_delay_floor_never_collapses_to_caller_delay(
    state_dir, policy_file, clock
):
    """THE fix: 5.0s participant + 0.4s participant => every gap >= 5.0s.

    Two independent guard instances share one state dir, standing in for two
    processes. The 0.4s caller must inherit the strictest declared cadence.
    """
    policy = policy_file(defaults={"min_interval_s": 0.1})
    slow = make_guard(state_dir, policy, clock)
    fast = make_guard(state_dir, policy, clock)

    slow.declare(RESOURCE, min_interval_s=5.0)

    stamps = []
    for i in range(8):
        g = slow if i % 2 == 0 else fast
        declared = 5.0 if i % 2 == 0 else 0.4
        stamps.append(g.acquire(RESOURCE, min_interval_s=declared).granted_at)

    observed = gaps(stamps)
    assert min(observed) >= 5.0, observed
    assert all(g == pytest.approx(5.0) for g in observed), observed


def test_fast_client_cannot_lower_a_floor_declared_earlier(
    state_dir, policy_file, clock
):
    policy = policy_file(defaults={"min_interval_s": 0.1})
    guard = make_guard(state_dir, policy, clock)

    guard.declare(RESOURCE, min_interval_s=5.0)
    assert guard.declare(RESOURCE, min_interval_s=0.4) == pytest.approx(5.0)
    assert guard.status(RESOURCE)["floor_s"] == pytest.approx(5.0)

    guard.acquire(RESOURCE, min_interval_s=0.4)
    assert guard.status(RESOURCE)["floor_s"] == pytest.approx(5.0)


def test_slower_declaration_raises_the_floor_for_everyone(
    state_dir, policy_file, clock
):
    policy = policy_file(defaults={"min_interval_s": 0.1})
    fast = make_guard(state_dir, policy, clock)
    careful = make_guard(state_dir, policy, clock)

    fast.acquire(RESOURCE, min_interval_s=0.4)
    careful.declare(RESOURCE, min_interval_s=30.0)

    before = clock.now
    fast.acquire(RESOURCE, min_interval_s=0.4)
    assert clock.now - before == pytest.approx(30.0)


def test_policy_minimum_is_a_hard_lower_bound(state_dir, policy_file, clock):
    policy = policy_file(resources={RESOURCE: {"min_interval_s": 10.0}})
    guard = make_guard(state_dir, policy, clock)

    assert guard.declare(RESOURCE, min_interval_s=0.1) == pytest.approx(10.0)
    guard.acquire(RESOURCE, min_interval_s=0.1)
    before = clock.now
    guard.acquire(RESOURCE, min_interval_s=0.1)
    assert clock.now - before == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# 2. Arbitrary resources: auto-registration + policy overrides
# ---------------------------------------------------------------------------


def test_unknown_resource_auto_registers_with_conservative_default(
    state_dir, policy_file, clock
):
    policy = policy_file(defaults={"min_interval_s": 7.5, "max_requests": 3})
    guard = make_guard(state_dir, policy, clock)

    st = guard.status("never-seen-before.example")
    assert st["registered"] is False
    assert st["floor_s"] == pytest.approx(7.5)

    guard.acquire("never-seen-before.example")
    st = guard.status("never-seen-before.example")
    assert st["registered"] is True
    assert st["floor_s"] == pytest.approx(7.5)
    assert st["max_requests"] == 3


def test_per_resource_policy_override_applies(state_dir, policy_file, clock):
    policy = policy_file(
        defaults={"min_interval_s": 5.0, "max_requests": 10},
        resources={RESOURCE: {"min_interval_s": 2.0, "max_requests": 999}},
    )
    guard = make_guard(state_dir, policy, clock)

    assert guard.status(RESOURCE)["floor_s"] == pytest.approx(2.0)
    assert guard.status(RESOURCE)["max_requests"] == 999
    assert guard.status("other.example")["floor_s"] == pytest.approx(5.0)
    assert guard.status("other.example")["max_requests"] == 10


def test_policy_file_is_reread_so_edits_take_effect(state_dir, policy_file, clock):
    policy = policy_file(defaults={"min_interval_s": 1.0})
    guard = make_guard(state_dir, policy, clock)
    assert guard.status("edit.example")["floor_s"] == pytest.approx(1.0)

    with open(policy, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["resources"]["edit.example"] = {"min_interval_s": 42.0}
    with open(policy, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)

    assert guard.status("edit.example")["floor_s"] == pytest.approx(42.0)


def test_malformed_policy_file_fails_closed(state_dir, tmp_path, clock):
    bad = tmp_path / "bad-policy.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    guard = make_guard(state_dir, str(bad), clock)

    with pytest.raises(PolicyError):
        guard.acquire(RESOURCE)


def test_missing_policy_file_uses_builtin_conservative_defaults(
    state_dir, tmp_path, clock
):
    missing = str(tmp_path / "no-such-policy.json")
    guard = make_guard(state_dir, missing, clock)

    st = guard.status(RESOURCE)
    assert st["floor_s"] >= 5.0
    assert st["max_requests"] <= 1000


# ---------------------------------------------------------------------------
# 3. Cooldown: refuse, never sleep
# ---------------------------------------------------------------------------


def test_cooldown_refuses_with_time_remaining(state_dir, policy_file, clock):
    guard = make_guard(state_dir, policy_file(), clock)
    guard.record_block(RESOURCE, reason="cloudflare challenge", cooldown_s=900.0)

    clock.now += 300.0
    with pytest.raises(CooldownActive) as exc:
        guard.acquire(RESOURCE)

    assert exc.value.resource == RESOURCE
    assert exc.value.remaining_s == pytest.approx(600.0)
    assert "cloudflare" in str(exc.value).lower()


def test_cooldown_refusal_does_not_sleep(state_dir, policy_file, clock):
    guard = make_guard(state_dir, policy_file(), clock)
    guard.record_block(RESOURCE, reason="blocked", cooldown_s=900.0)

    before = clock.now
    with pytest.raises(CooldownActive):
        guard.acquire(RESOURCE)
    assert clock.slept == []
    assert clock.now == before


def test_cooldown_expires_and_then_grants(state_dir, policy_file, clock):
    guard = make_guard(state_dir, policy_file(), clock)
    guard.record_block(RESOURCE, reason="blocked", cooldown_s=60.0)

    clock.now += 61.0
    grant = guard.acquire(RESOURCE)
    assert grant.resource == RESOURCE


def test_cooldown_only_extends_never_shortens(state_dir, policy_file, clock):
    guard = make_guard(state_dir, policy_file(), clock)
    guard.record_block(RESOURCE, reason="first", cooldown_s=3600.0)
    guard.record_block(RESOURCE, reason="second", cooldown_s=5.0)

    with pytest.raises(CooldownActive) as exc:
        guard.acquire(RESOURCE)
    assert exc.value.remaining_s == pytest.approx(3600.0)


def test_cooldown_is_visible_to_a_separate_guard_instance(
    state_dir, policy_file, clock
):
    policy = policy_file()
    reporter = make_guard(state_dir, policy, clock)
    other = make_guard(state_dir, policy, clock)

    reporter.record_block(RESOURCE, reason="blocked", cooldown_s=120.0)
    with pytest.raises(CooldownActive):
        other.acquire(RESOURCE)


def test_record_block_defaults_to_a_next_day_retry(state_dir, policy_file, clock):
    guard = make_guard(state_dir, policy_file(), clock)
    guard.record_block(RESOURCE, reason="bot challenge")

    st = guard.status(RESOURCE)
    assert st["cooldown_remaining_s"] == pytest.approx(86400.0)


# ---------------------------------------------------------------------------
# 4. Cost-weighted rolling-window budget: raise, never sleep, never warn
# ---------------------------------------------------------------------------


def test_request_budget_exhaustion_raises(state_dir, policy_file, clock):
    policy = policy_file(defaults={"min_interval_s": 0.0, "max_requests": 3})
    guard = make_guard(state_dir, policy, clock)

    for _ in range(3):
        guard.acquire(RESOURCE, cost=1.0)

    with pytest.raises(BudgetExhausted) as exc:
        guard.acquire(RESOURCE, cost=1.0)
    assert exc.value.kind == "requests"
    assert exc.value.limit == 3
    assert exc.value.retry_after_s > 0


def test_cost_budget_exhaustion_raises(state_dir, policy_file, clock):
    policy = policy_file(
        defaults={"min_interval_s": 0.0, "max_requests": 100000, "max_cost": 100.0}
    )
    guard = make_guard(state_dir, policy, clock)

    guard.acquire(RESOURCE, cost=100.0)
    with pytest.raises(BudgetExhausted) as exc:
        guard.acquire(RESOURCE, cost=1.0)
    assert exc.value.kind == "cost"
    assert exc.value.limit == pytest.approx(100.0)


def test_budget_exhaustion_never_sleeps(state_dir, policy_file, clock):
    policy = policy_file(defaults={"min_interval_s": 0.0, "max_requests": 1})
    guard = make_guard(state_dir, policy, clock)
    guard.acquire(RESOURCE)

    clock.slept.clear()
    before = clock.now
    with pytest.raises(BudgetExhausted):
        guard.acquire(RESOURCE)
    assert clock.slept == []
    assert clock.now == before


def test_a_single_request_over_the_cost_ceiling_is_refused_outright(
    state_dir, policy_file, clock
):
    policy = policy_file(defaults={"min_interval_s": 0.0, "max_cost": 50.0})
    guard = make_guard(state_dir, policy, clock)

    with pytest.raises(BudgetExhausted) as exc:
        guard.acquire(RESOURCE, cost=100.0)
    assert exc.value.kind == "cost"


def test_cost_weighting_makes_a_big_query_debit_proportionally(
    state_dir, policy_file, clock
):
    """`limit: 5000` (cost 100) must debit ~100x a `limit: 50` query (cost 1)."""
    policy = policy_file(
        defaults={"min_interval_s": 0.0, "max_requests": 100000, "max_cost": 100.0}
    )
    guard = make_guard(state_dir, policy, clock)

    # 100 small queries fit exactly inside the ceiling one big query fills alone.
    for _ in range(100):
        guard.acquire("small.example", cost=1.0)
    with pytest.raises(BudgetExhausted):
        guard.acquire("small.example", cost=1.0)

    guard.acquire("big.example", cost=100.0)
    with pytest.raises(BudgetExhausted):
        guard.acquire("big.example", cost=1.0)


def test_budget_window_rolls_off(state_dir, policy_file, clock):
    policy = policy_file(
        defaults={"min_interval_s": 0.0, "max_requests": 2, "window_s": 60.0}
    )
    guard = make_guard(state_dir, policy, clock)

    guard.acquire(RESOURCE)
    guard.acquire(RESOURCE)
    with pytest.raises(BudgetExhausted):
        guard.acquire(RESOURCE)

    clock.now += 61.0
    guard.acquire(RESOURCE)  # window rolled; must not raise


def test_budget_is_shared_across_guard_instances(state_dir, policy_file, clock):
    policy = policy_file(defaults={"min_interval_s": 0.0, "max_requests": 2})
    a = make_guard(state_dir, policy, clock)
    b = make_guard(state_dir, policy, clock)

    a.acquire(RESOURCE)
    b.acquire(RESOURCE)
    with pytest.raises(BudgetExhausted):
        a.acquire(RESOURCE)


# ---------------------------------------------------------------------------
# 5. Robustness: corrupt state, stale reservations, no bypass
# ---------------------------------------------------------------------------


def test_corrupt_state_file_fails_safe(state_dir, policy_file, clock):
    """Corrupt history must cost a full floor AND keep a ratcheted floor.

    The declared interval is deliberately well above the policy minimum: if the
    ratchet were lost with the rest of the state, this waits 4s instead of 12s
    and the assertion catches it. A version of this test that declared nothing
    above the policy floor passed even while the ratchet was being dropped.
    """
    policy = policy_file(defaults={"min_interval_s": 4.0})
    guard = make_guard(state_dir, policy, clock)
    guard.declare(RESOURCE, 12.0)
    guard.acquire(RESOURCE)

    path = guard.state_path(RESOURCE)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ truncated mid-write")

    before = clock.now
    guard.acquire(RESOURCE)
    # Unreadable history => assume a request just went out and wait a full floor,
    # and that floor is still the declared 12s, not the 4s policy minimum.
    assert clock.now - before == pytest.approx(12.0)
    assert guard.status(RESOURCE)["floor_s"] == pytest.approx(12.0)


def test_absurd_future_reservation_is_clamped(state_dir, policy_file, clock):
    policy = policy_file(defaults={"min_interval_s": 2.0})
    guard = make_guard(state_dir, policy, clock)
    guard.acquire(RESOURCE)

    path = guard.state_path(RESOURCE)
    with open(path, "r", encoding="utf-8") as fh:
        st = json.load(fh)
    st["last_slot"] = clock.now + 1e9  # clock skew / corrupt writer
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(st, fh)

    before = clock.now
    guard.acquire(RESOURCE)
    assert clock.now - before <= 2.0 + 1e-6


def test_wait_longer_than_max_refuses_without_reserving(
    state_dir, policy_file, clock
):
    policy = policy_file(defaults={"min_interval_s": 100.0, "max_wait_s": 10.0})
    guard = make_guard(state_dir, policy, clock)
    first = guard.acquire(RESOURCE)

    with pytest.raises(WaitTooLong) as exc:
        guard.acquire(RESOURCE)
    assert exc.value.retry_after_s == pytest.approx(100.0)
    assert clock.slept == []

    # The refusal must not have burned a slot: after the floor elapses the
    # next caller is granted exactly one floor after the first grant.
    clock.now += 100.0
    second = guard.acquire(RESOURCE)
    assert second.granted_at - first.granted_at == pytest.approx(100.0)


def test_no_override_parameter_on_the_api():
    """Overriding means editing the policy file. There is no bypass knob."""
    banned = {
        "force", "override", "bypass", "skip", "skip_floor", "ignore",
        "ignore_floor", "no_wait", "nowait", "allow", "unsafe", "emergency",
        "urgent", "priority",
    }
    for name in ("acquire", "declare", "record_block", "status"):
        params = set(inspect.signature(getattr(EgressGuard, name)).parameters)
        assert not (params & banned), (name, params & banned)

    src = inspect.getsource(EgressGuard)
    for env_var in ("EGRESS_GUARD_FORCE", "EGRESS_GUARD_BYPASS", "EGRESS_GUARD_DISABLE"):
        assert env_var not in src


def test_errors_share_a_common_base():
    for exc in (CooldownActive, BudgetExhausted, WaitTooLong, PolicyError):
        assert issubclass(exc, EgressGuardError)
    assert issubclass(EgressGuardError, RuntimeError)


# ---------------------------------------------------------------------------
# 6. Cross-process behaviour (real subprocesses, real clock, no network)
# ---------------------------------------------------------------------------

_CHILD_ACQUIRE = textwrap.dedent(
    """
    import json, sys
    sys.path.insert(0, {repo!r})
    from egress_guard.limiter import EgressGuard

    guard = EgressGuard(state_dir={state!r}, policy_path={policy!r})
    out = []
    for _ in range({n}):
        g = guard.acquire({resource!r}, min_interval_s={declared!r})
        out.append([g.slot_at, g.granted_at])
    print(json.dumps(out))
    """
)


def _run_child(state_dir, policy, n, declared, resource):
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CHILD_ACQUIRE.format(
                repo=REPO_ROOT,
                state=state_dir,
                policy=policy,
                n=n,
                declared=declared,
                resource=resource,
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_concurrent_processes_share_one_cadence(state_dir, policy_file):
    """Three processes, one slow participant, one aggregate floor.

    The policy minimum is 200x smaller than the declared interval, so the
    cadence asserted below can only come from the DECLARATION propagating
    across processes. With every child declaring at or below the policy
    minimum, this test passed even when declarations were ignored entirely.

    The declaration is made BEFORE any child starts, which is the contract: a
    declaration cannot retroactively slow a request that was already granted,
    so declare before first contact or the policy floor is the only protection.
    """
    floor = 2.0
    policy = policy_file(defaults={"min_interval_s": 0.01, "max_wait_s": 60.0})
    EgressGuard(state_dir=state_dir, policy_path=policy).declare(
        "concurrent.example", floor
    )

    procs = [
        _run_child(state_dir, policy, 4, floor, "concurrent.example"),
        _run_child(state_dir, policy, 4, 0.0, "concurrent.example"),
        _run_child(state_dir, policy, 4, 0.001, "concurrent.example"),
    ]
    slots: list[float] = []
    granted: list[float] = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        for slot_at, granted_at in json.loads(out):
            slots.append(slot_at)
            granted.append(granted_at)

    slots.sort()
    granted.sort()
    assert len(slots) == 12
    # Reserved slots are exactly the aggregate cadence: no participant's own
    # (faster) declared interval leaks into the shared schedule.
    assert min(gaps(slots)) >= floor - 1e-9, gaps(slots)
    # And the wall clock agrees: 12 requests cannot finish faster than 11 floors.
    assert granted[-1] - granted[0] >= floor * 11 * 0.9


def test_killed_lock_holder_does_not_wedge_the_limiter(state_dir, policy_file):
    """flock is released by the kernel on death; a SIGKILLed holder must not wedge."""
    policy = policy_file(defaults={"min_interval_s": 0.0})
    child_src = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {REPO_ROOT!r})
        from egress_guard.limiter import EgressGuard
        guard = EgressGuard(state_dir={state_dir!r}, policy_path={policy!r})
        with guard.locked("stale.example"):
            print("LOCKED", flush=True)
            time.sleep(300)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_src],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "LOCKED"
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)

        guard = EgressGuard(state_dir=state_dir, policy_path=policy)
        started = time.monotonic()
        guard.acquire("stale.example")
        assert time.monotonic() - started < 5.0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_reservation_left_by_a_crashed_process_expires(state_dir, policy_file, clock):
    """A dead process's reservation costs at most one floor, not a permanent wedge."""
    policy = policy_file(defaults={"min_interval_s": 3.0})
    guard = make_guard(state_dir, policy, clock)
    guard.acquire(RESOURCE)  # reserves now; the "process" then dies

    clock.now += 3.0
    before = clock.now
    guard.acquire(RESOURCE)
    assert clock.now - before == pytest.approx(0.0)
