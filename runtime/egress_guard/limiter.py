#!/usr/bin/env python3
"""Cross-process floor-cadence limiter with cooldowns and a cost-weighted budget.

Why this exists
---------------
Per-client throttling is not a rate limit. The prototype this replaces held a
`flock` around a shared timestamp file, but each caller waited its OWN declared
delay since that timestamp and then re-stamped. A client constructed at
`delay=0.4` therefore reset the schedule for a client running at `delay=5.0`,
and the observed aggregate collapsed to roughly one request per second.

The invariant here is a FLOOR: the effective cadence for a resource is the
strictest interval any participant has declared, never the caller's own. A
participant can only ever make the shared schedule slower.

Model
-----
Every acquisition takes a SLOT. Under an exclusive lock the guard computes
`slot = max(now, last_slot + floor)`, records it, releases the lock, and only
then sleeps until the slot. The lock is held for microseconds, so a slow or
crashed participant cannot wedge the others, and concurrent processes queue
into distinct slots spaced by exactly the floor.

Three ways a call is refused rather than delayed:

* `CooldownActive` — the resource has an unblock-at timestamp in the future.
  Recovery is a scheduled retry, not an escalating ladder; the guard reports
  time remaining and refuses. It never waits one out.
* `BudgetExhausted` — the rolling window's request or cost ceiling is spent.
  Exhaustion raises. It never sleeps, and never degrades to a warning.
* `WaitTooLong` — the queue is deeper than `max_wait_s`. No slot is reserved,
  so refusing here can only ever delay traffic, never release it early.

There is no override flag and no force parameter. Widening a limit means
editing the policy file.

`EGRESS_GUARD_POLICY` and `EGRESS_GUARD_STATE` select WHICH policy and state
the guard uses; they cannot widen a limit by themselves. They are not a bypass
in the sense that matters, because setting them requires the same access as
editing the policy file itself — but they are the closest thing to one here, so
the shipped server is registered with an empty `env` and a test enforces that.
Pointing a live server at a permissive policy is a deliberate act, not a flag
an impatient caller can reach for mid-run.

State
-----
One JSON file per resource under the state dir, guarded by a sibling `.lock`
via `fcntl.flock`. The kernel releases `flock` when a holder dies, so a killed
process cannot wedge the limiter. Unreadable state is treated as "a request
just went out" (wait a full floor) rather than "no history" (go now).
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

__all__ = [
    "BudgetExhausted",
    "CooldownActive",
    "DEFAULT_POLICY",
    "EgressGuard",
    "EgressGuardError",
    "Grant",
    "PolicyError",
    "PreflightRequired",
    "Undeclared",
    "WaitTooLong",
]


# Conservative built-ins, used when no policy file exists. Deliberately
# slower and smaller than any host would require: an unknown resource is
# something we have not measured, so it gets the cautious treatment.
DEFAULT_POLICY: dict[str, Any] = {
    "defaults": {
        "min_interval_s": 5.0,
        "window_s": 3600.0,
        "max_requests": 120,
        "max_cost": 600.0,
        "max_wait_s": 60.0,
        "block_cooldown_s": 86400.0,
        # Blast radius must be declared before first contact.
        "require_declaration": False,
        # Beyond this many grants in a window, a resource needs a recorded
        # preflight. A correctness bug that goes unnoticed on a bounded sample
        # is what turns one run into a repair pass at full scale.
        "preflight_after_requests": 0,
    },
    "resources": {},
}

# A reservation further ahead than this is not a queue, it is corruption or a
# clock jump. Treat it as "a request just went out" instead of blocking for
# years. Generous enough that a legitimately deep queue is never discarded.
SANITY_HORIZON_S = 86400.0

# How long an unreadable cooldown holds the resource closed. Matches the
# retry-tomorrow default: a block we cannot verify has lapsed has not lapsed.
CORRUPT_COOLDOWN_S = 86400.0

_NUMERIC_FIELDS = (
    "min_interval_s",
    "window_s",
    "max_requests",
    "max_cost",
    "max_wait_s",
    "block_cooldown_s",
    "preflight_after_requests",
)

# Counts, not durations: a fractional value silently truncates toward zero, and
# 0 means "gate off". A policy of 0.5 must be an error, not a disabled gate.
_INTEGER_FIELDS = ("max_requests", "preflight_after_requests")

_BOOLEAN_FIELDS = ("require_declaration",)

# Zero is not "no limit" for these, it is a disabled limit that still reports
# itself as configured.
_POSITIVE_FIELDS = ("window_s", "block_cooldown_s")

# Floor for the server's shutdown drain. The drain must outlast any parked
# acquire, since a parked call has already debited its slot and budget event,
# so the server derives its real timeout from the policy instead of capping it.
MIN_DRAIN_TIMEOUT_S = 900.0

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _default_state_dir() -> str:
    env = os.environ.get("EGRESS_GUARD_STATE")
    if env:
        return env
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return os.path.join(xdg, "egress-guard")
    home = os.path.expanduser("~")
    if home and home != "~":
        return os.path.join(home, ".local", "state", "egress-guard")
    return os.path.join(tempfile.gettempdir(), "egress-guard")


def _default_policy_path() -> str:
    env = os.environ.get("EGRESS_GUARD_POLICY")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(here)
    return os.path.join(repo_root, "mcps", "egress-guard", "egress-policy.json")


class EgressGuardError(RuntimeError):
    """Base class for every refusal this module can issue."""


class PolicyError(EgressGuardError):
    """The policy file exists but cannot be trusted. Fails closed."""


class CooldownActive(EgressGuardError):
    """The resource is in cooldown. Refused, with time remaining."""

    def __init__(self, resource: str, remaining_s: float, until: float, reason: str):
        self.resource = resource
        self.remaining_s = remaining_s
        self.until = until
        self.reason = reason or "unspecified"
        super().__init__(
            f"{resource}: cooldown active, {remaining_s:.0f}s remaining "
            f"(until epoch {until:.0f}) — reason: {self.reason}. "
            "Do not retry sooner, do not change identity, do not route around it. "
            "Schedule the retry for after the cooldown."
        )


class BudgetExhausted(EgressGuardError):
    """The rolling-window ceiling is spent. Raised, never slept off."""

    def __init__(
        self,
        resource: str,
        kind: str,
        used: float,
        limit: float,
        window_s: float,
        retry_after_s: float,
    ):
        self.resource = resource
        self.kind = kind  # "requests" | "cost"
        self.used = used
        self.limit = limit
        self.window_s = window_s
        self.retry_after_s = retry_after_s
        super().__init__(
            f"{resource}: {kind} budget exhausted — {used:g}/{limit:g} in the "
            f"last {window_s:.0f}s. Earliest capacity in {retry_after_s:.0f}s. "
            "Raise the ceiling by editing the egress policy file, not in code."
        )


class WaitTooLong(EgressGuardError):
    """The queue is deeper than max_wait_s. Nothing was reserved."""

    def __init__(self, resource: str, retry_after_s: float, max_wait_s: float):
        self.resource = resource
        self.retry_after_s = retry_after_s
        self.max_wait_s = max_wait_s
        super().__init__(
            f"{resource}: next free slot is {retry_after_s:.1f}s away, over the "
            f"{max_wait_s:.1f}s ceiling for holding a call open. No slot was "
            "reserved; come back later."
        )


class Undeclared(EgressGuardError):
    """No blast radius on file for a resource whose policy demands one."""

    def __init__(self, resource: str):
        self.resource = resource
        super().__init__(
            f"{resource} has no blast-radius declaration. Declare expected volume, "
            "cost, ceiling and what pushback looks like before first contact."
        )


class PreflightRequired(EgressGuardError):
    """Bounded-sample checks have not been recorded for a resource going wide."""

    def __init__(self, resource: str, used: int, threshold: int):
        self.resource, self.used, self.threshold = resource, used, threshold
        super().__init__(
            f"{resource} has had {used} grants (threshold {threshold}) with no recorded "
            "preflight. Prove the invariants on a bounded sample — short pages, "
            "offset caps, over-counted totals, empty windows, duplicates — then "
            "record the result before going wider."
        )


@dataclass(frozen=True)
class Grant:
    """Permission to make exactly one request against `resource`."""

    resource: str
    slot_at: float
    granted_at: float
    waited_s: float
    floor_s: float
    cost: float
    window_s: float
    requests_used: int
    max_requests: int
    cost_used: float
    max_cost: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "slot_at": self.slot_at,
            "granted_at": self.granted_at,
            "waited_s": round(self.waited_s, 3),
            "floor_s": self.floor_s,
            "cost": self.cost,
            "window_s": self.window_s,
            "requests_used": self.requests_used,
            "max_requests": self.max_requests,
            "cost_used": round(self.cost_used, 3),
            "max_cost": self.max_cost,
        }


class EgressGuard:
    """The single chokepoint. One instance per process; state is shared on disk.

    Widening any limit is an edit to the policy file. Nothing on this class
    takes a force, override or bypass argument, by construction.
    """

    def __init__(
        self,
        state_dir: Optional[str] = None,
        policy_path: Optional[str] = None,
        time_fn: Optional[Callable[[], float]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.state_dir = state_dir or _default_state_dir()
        self.policy_path = policy_path or _default_policy_path()
        self._time = time_fn or time.time
        self._sleep = sleep_fn or time.sleep

    # -- policy ----------------------------------------------------------

    def load_policy(self) -> dict[str, Any]:
        """Read the policy file fresh, so an edit takes effect without a restart.

        Missing file → conservative built-ins. Present but malformed → refuse
        everything: a gate that cannot read its own limits must not guess.
        """
        try:
            with open(self.policy_path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            return DEFAULT_POLICY
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            raise PolicyError(
                f"egress policy {self.policy_path} is unreadable ({e}). Refusing "
                "all egress until it parses."
            ) from e
        if not isinstance(doc, dict):
            raise PolicyError(f"egress policy {self.policy_path} is not an object.")
        defaults = doc.get("defaults") or {}
        resources = doc.get("resources") or {}
        if not isinstance(defaults, dict) or not isinstance(resources, dict):
            raise PolicyError(
                f"egress policy {self.policy_path}: 'defaults' and 'resources' "
                "must both be objects."
            )
        return {"defaults": defaults, "resources": resources}

    def resource_policy(self, resource: str) -> dict[str, Any]:
        """Effective limits for `resource`: built-ins < defaults < per-resource."""
        doc = self.load_policy()
        merged = dict(DEFAULT_POLICY["defaults"])
        merged.update(doc.get("defaults") or {})
        override = (doc.get("resources") or {}).get(resource)
        if override is not None:
            if not isinstance(override, dict):
                raise PolicyError(
                    f"egress policy: resources[{resource!r}] must be an object."
                )
            merged.update(override)
        for field in _NUMERIC_FIELDS:
            value = merged.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise PolicyError(
                    f"egress policy: {resource!r}.{field} must be a number "
                    f"(got {value!r})."
                )
            if math.isnan(value) or math.isinf(value):
                # `json.load` happily parses the bare tokens NaN and Infinity,
                # and every comparison against NaN is False — a NaN limit is an
                # absent limit that still looks configured.
                raise PolicyError(
                    f"egress policy: {resource!r}.{field} must be finite "
                    f"(got {value!r})."
                )
            if value < 0:
                raise PolicyError(
                    f"egress policy: {resource!r}.{field} must not be negative."
                )
        for field in _POSITIVE_FIELDS:
            # Zero here reads as "configured" but switches the limit off:
            # window_s=0 prunes every event, so request/cost/preflight ceilings
            # never fire; block_cooldown_s=0 makes the default block path raise
            # instead of recording pushback.
            if merged[field] <= 0:
                raise PolicyError(
                    f"egress policy: {resource!r}.{field} must be greater than zero "
                    f"(got {merged[field]!r}; zero silently disables the limit it "
                    "appears to configure)."
                )
        for field in _INTEGER_FIELDS:
            value = merged[field]
            if float(value) != int(value):
                raise PolicyError(
                    f"egress policy: {resource!r}.{field} is a count and must be a "
                    f"whole number (got {value!r}; fractions truncate toward zero, "
                    "which silently disables the limit)."
                )
        for field in _BOOLEAN_FIELDS:
            if field in merged and not isinstance(merged[field], bool):
                raise PolicyError(
                    f"egress policy: {resource!r}.{field} must be true or false "
                    f"(got {merged[field]!r})."
                )
        merged["max_requests"] = int(merged["max_requests"])
        merged["preflight_after_requests"] = int(merged["preflight_after_requests"])
        return merged

    # -- paths and locking ------------------------------------------------

    def _slug(self, resource: str) -> str:
        cleaned = _SLUG_RE.sub("_", resource).strip("_") or "resource"
        digest = hashlib.sha256(resource.encode("utf-8")).hexdigest()[:10]
        return f"{cleaned[:80]}.{digest}"

    def state_path(self, resource: str) -> str:
        return os.path.join(self.state_dir, self._slug(resource) + ".json")

    def lock_path(self, resource: str) -> str:
        return os.path.join(self.state_dir, self._slug(resource) + ".lock")

    def floor_path(self, resource: str) -> str:
        return os.path.join(self.state_dir, self._slug(resource) + ".floor")

    def block_path(self, resource: str) -> str:
        return os.path.join(self.state_dir, self._slug(resource) + ".block")

    def _sidecar_cooldown(self, resource: str) -> tuple[float, str]:
        """A recorded block, kept apart from the mutable state file.

        Same reasoning as the floor sidecar: losing history must never make us
        less careful. A corrupt state file would otherwise turn a recorded
        24h block into a single floor wait.
        """
        try:
            with open(self.block_path(resource), "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            return _as_float(doc.get("cooldown_until"), 0.0), str(doc.get("reason") or "")
        except (OSError, ValueError, AttributeError):
            return 0.0, ""

    def _extend_sidecar_cooldown(self, resource: str, until: float, reason: str) -> None:
        current, _ = self._sidecar_cooldown(resource)
        if until <= current:
            return
        path = self.block_path(resource)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"cooldown_until": until, "reason": reason}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _sidecar_floor(self, resource: str) -> float:
        """The ratchet, kept apart from the mutable state file.

        Losing history must never make us FASTER. The state file carries
        counters that are legitimately reset when corrupt; the floor is not one
        of them, so it lives in its own write-once-per-raise file.
        """
        try:
            with open(self.floor_path(resource), "r", encoding="utf-8") as fh:
                return _as_float(json.load(fh).get("floor_s"), 0.0)
        except (OSError, ValueError, AttributeError):
            return 0.0

    def _raise_sidecar_floor(self, resource: str, floor: float) -> None:
        if floor <= self._sidecar_floor(resource):
            return
        path = self.floor_path(resource)
        tmp = f"{path}.tmp.{os.getpid()}"
        # Deliberately NOT swallowed: if the ratchet backup cannot be written,
        # a later state corruption drops the floor to the policy minimum — the
        # exact regression the sidecar exists to prevent. Failing loudly here
        # beats discovering it as unexplained speed.
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"floor_s": floor}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    @contextlib.contextmanager
    def locked(self, resource: str) -> Iterator[Any]:
        """Exclusive cross-process lock for one resource.

        `flock` and not a pid file: the kernel drops it when the holder dies,
        so a crashed or SIGKILLed participant cannot wedge every other process.
        """
        os.makedirs(self.state_dir, exist_ok=True)
        fh = open(self.lock_path(resource), "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield fh
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    # -- state ------------------------------------------------------------

    def _fresh_state(self, resource: str) -> dict[str, Any]:
        return {
            "resource": resource,
            "floor_s": 0.0,
            "last_slot": None,
            "cooldown_until": 0.0,
            "cooldown_reason": "",
            "events": [],
        }

    def _read_state(self, resource: str, now: float) -> dict[str, Any]:
        """Load state. Unreadable state fails SAFE, not open.

        A missing file means we have never touched this resource: no wait. A
        present-but-corrupt file means we lost the history of a resource we
        HAVE touched, so we assume a request just went out and make the next
        caller wait a full floor.
        """
        path = self.state_path(resource)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return self._merge_sidecars(resource, self._fresh_state(resource))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
            state = self._fresh_state(resource)
            state["last_slot"] = now
            return self._merge_sidecars(resource, state)
        if not isinstance(raw, dict):
            state = self._fresh_state(resource)
            state["last_slot"] = now
            return self._merge_sidecars(resource, state)

        state = self._fresh_state(resource)
        state["floor_s"] = _as_float(raw.get("floor_s"), 0.0)
        reason = raw.get("cooldown_reason")
        state["cooldown_reason"] = reason if isinstance(reason, str) else ""

        cooldown_raw = raw.get("cooldown_until")
        if cooldown_raw is None:
            state["cooldown_until"] = 0.0
        elif _is_finite_number(cooldown_raw):
            state["cooldown_until"] = float(cooldown_raw)
        else:  # noqa: PLR5501 - the held-closed branch below is the point
            # A cooldown we cannot read is not an absent cooldown. Falling back
            # to 0.0 would forget a recorded block and grant; we cannot prove it
            # expired, so stay closed for the standard retry-tomorrow window.
            state["cooldown_until"] = now + CORRUPT_COOLDOWN_S
            state["cooldown_reason"] = (
                str(raw.get("cooldown_reason") or "unspecified")
                + " (cooldown timestamp unreadable; held closed)"
            )

        last_slot = raw.get("last_slot")
        if _is_finite_number(last_slot):
            last_slot = float(last_slot)
            # A reservation past the horizon is corruption or a clock jump.
            # Collapse it to "now" — conservative (one more floor of wait)
            # without wedging the resource for years.
            state["last_slot"] = now if last_slot > now + SANITY_HORIZON_S else last_slot
        elif last_slot is None:
            state["last_slot"] = None
        else:
            # Junk, including NaN: a NaN here yields max(now, NaN + floor) == now,
            # handing out an immediate slot with no wait. Assume a request just
            # went out instead.
            state["last_slot"] = now

        events: list[list[float]] = []
        for item in raw.get("events") or []:
            # A non-finite cost poisons the running total and switches the cost
            # ceiling off until it ages out of the window; drop it instead.
            if (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and all(_is_finite_number(x) for x in item)
            ):
                ts = float(item[0])
                if ts > now + SANITY_HORIZON_S:
                    ts = now
                events.append([ts, float(item[1])])
        state["events"] = events

        # Declaration and preflight are records, not counters: carry them
        # through normalisation or every read would silently revoke them.
        for key in ("declaration", "preflight"):
            value = raw.get(key)
            if isinstance(value, dict):
                state[key] = value

        return self._merge_sidecars(resource, state)

    def _merge_sidecars(self, resource: str, state: dict[str, Any]) -> dict[str, Any]:
        """Fold in the records that must outlive the state file.

        Both sidecars only ever make the guard MORE careful: the strictest floor
        and the latest cooldown win. Whole-file corruption therefore cannot turn
        a recorded block into a single floor wait, nor undo a ratchet.
        """
        state["floor_s"] = max(_as_float(state.get("floor_s"), 0.0),
                               self._sidecar_floor(resource))
        until, reason = self._sidecar_cooldown(resource)
        if until > _as_float(state.get("cooldown_until"), 0.0):
            state["cooldown_until"] = until
            state["cooldown_reason"] = reason or state.get("cooldown_reason", "")
        return state

    def _write_state(self, resource: str, state: dict[str, Any]) -> None:
        path = self.state_path(resource)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    # -- floor ------------------------------------------------------------

    def _effective_floor(
        self,
        state: dict[str, Any],
        policy: dict[str, Any],
        declared: Optional[float],
        resource: Optional[str] = None,
    ) -> float:
        """Strictest wins. The caller's own interval can only raise the floor."""
        candidates = [float(policy["min_interval_s"]), _as_float(state.get("floor_s"), 0.0)]
        if resource is not None:
            candidates.append(self._sidecar_floor(resource))
        if declared is not None:
            candidates.append(float(declared))
        return max(candidates)

    # -- public API -------------------------------------------------------

    def declare(
        self,
        resource: str,
        min_interval_s: float,
        expected_requests: Optional[int] = None,
        expected_cost: Optional[float] = None,
        ceiling_requests: Optional[int] = None,
        pushback: Optional[str] = None,
        description: Optional[str] = None,
    ) -> float:
        """Register `resource` and ratchet its floor up to `min_interval_s`.

        Returns the effective floor, which may be stricter than what was
        declared — a slower participant, or the policy file, always wins.

        The optional arguments record the blast radius: how much traffic this
        is expected to be, what it may not exceed, and what pushback from this
        resource looks like. Estimating those before first contact is what
        makes "this is more than I said" observable rather than discovered.
        """
        min_interval_s = _finite(min_interval_s, "min_interval_s")
        if min_interval_s < 0:
            raise ValueError("min_interval_s must not be negative")
        policy = self.resource_policy(resource)
        now = self._time()
        with self.locked(resource):
            state = self._read_state(resource, now)
            floor = self._effective_floor(state, policy, min_interval_s, resource)
            state["floor_s"] = floor
            self._raise_sidecar_floor(resource, floor)
            declaration = dict(state.get("declaration") or {})
            declaration["declared_at"] = now
            declaration["min_interval_s"] = min_interval_s
            for key, value in (
                ("expected_requests", expected_requests),
                ("expected_cost", expected_cost),
                ("ceiling_requests", ceiling_requests),
            ):
                if value is not None:
                    # Advisory today, but a NaN here would poison any future
                    # comparison against it, which is how this class starts.
                    declaration[key] = _finite(value, key)
            for key, value in (("pushback", pushback), ("description", description)):
                if value is not None:
                    declaration[key] = str(value)
            state["declaration"] = declaration
            self._write_state(resource, state)
        return floor

    def record_extra_cost(self, resource: str, cost: float) -> float:
        """Charge additional cost to the most recent grant, taking no new slot.

        A proxy only learns a response's size after the request is made, and the
        cost budget exists precisely for the expensive calls. Charging it to the
        latest event keeps the request COUNT honest while letting the weight
        catch up.
        """
        cost = _finite(cost, "cost")
        if cost < 0:
            raise ValueError("cost must not be negative")
        now = self._time()
        with self.locked(resource):
            state = self._read_state(resource, now)
            events = state.get("events") or []
            if events:
                events[-1][1] = float(events[-1][1]) + cost
            else:
                events = [[now, cost]]
            state["events"] = events
            self._write_state(resource, state)
            return float(events[-1][1])

    def record_preflight(
        self, resource: str, checks: list, sample_requests: int
    ) -> dict[str, Any]:
        """Record that bounded-sample invariant checks PASSED for `resource`.

        `checks` is a list of {"name": str, "passed": bool}. A failing check is
        refused rather than stored: recording a red preflight would turn the
        gate into paperwork.
        """
        if not checks:
            raise ValueError(
                "checks must not be empty — name the invariants you actually proved."
            )
        for c in checks:
            if not isinstance(c, dict):
                raise ValueError("each check must be an object with name and passed")
            # STRICT bool. `not c["passed"]` treats any truthy value as a pass,
            # so the string "false" records a red preflight as green and unlocks
            # the wide run. A gate that accepts a near-miss type is not a gate.
            if not isinstance(c.get("passed"), bool):
                raise ValueError(
                    f"check {c.get('name', '?')!r}: `passed` must be a boolean, got "
                    f"{c.get('passed')!r}"
                )
            if not isinstance(c.get("name"), str) or not c["name"].strip():
                raise ValueError("each check needs a non-empty name")
        failed = [c["name"] for c in checks if not c["passed"]]
        if failed:
            raise ValueError(
                "preflight has failing checks and will not be recorded: "
                + ", ".join(failed)
            )
        now = self._time()
        record = {
            "at": now,
            "sample_requests": int(_finite(sample_requests, "sample_requests")),
            "checks": [str(c.get("name", "?")) for c in checks],
        }
        with self.locked(resource):
            state = self._read_state(resource, now)
            state["preflight"] = record
            self._write_state(resource, state)
        return record

    def record_block(
        self,
        resource: str,
        reason: str,
        cooldown_s: Optional[float] = None,
    ) -> float:
        """Record pushback from a host: refuse every caller until the cooldown ends.

        Default cooldown is a next-day retry (policy `block_cooldown_s`), not an
        escalating ladder. Cooldowns only ever extend — a later, shorter report
        cannot shorten an earlier, longer one.
        """
        if cooldown_s is None:
            # Only consult the policy when we actually need its default: a
            # malformed policy must not stop us RECORDING pushback we were
            # explicitly told about.
            cooldown_s = float(self.resource_policy(resource)["block_cooldown_s"])
        cooldown_s = _finite(cooldown_s, "cooldown_s")
        if cooldown_s <= 0:
            # Zero is not a block. It persists the reason and refuses nobody,
            # while the caller is told the resource is now protected.
            raise ValueError("cooldown_s must be positive — a zero cooldown blocks nothing")
        now = self._time()
        with self.locked(resource):
            state = self._read_state(resource, now)
            until = max(_as_float(state.get("cooldown_until"), 0.0), now + cooldown_s)
            if until > _as_float(state.get("cooldown_until"), 0.0):
                state["cooldown_reason"] = reason
            state["cooldown_until"] = until
            self._write_state(resource, state)
            # Survives losing the state file entirely.
            self._extend_sidecar_cooldown(resource, until, state["cooldown_reason"])
        return until

    def status(self, resource: str) -> dict[str, Any]:
        """Current floor, cooldown and budget consumption for `resource`."""
        policy = self.resource_policy(resource)
        now = self._time()
        registered = os.path.exists(self.state_path(resource))
        state = self._read_state(resource, now)
        floor = self._effective_floor(state, policy, None, resource)
        window = float(policy["window_s"])
        events = _live_events(state["events"], now, window)
        cooldown_until = _as_float(state.get("cooldown_until"), 0.0)
        last_slot = state.get("last_slot")
        next_slot = now if last_slot is None else max(now, float(last_slot) + floor)
        return {
            "resource": resource,
            "registered": registered,
            "floor_s": floor,
            "cooldown_until": cooldown_until,
            "cooldown_remaining_s": max(0.0, cooldown_until - now),
            "cooldown_reason": state.get("cooldown_reason", ""),
            "window_s": window,
            "requests_used": len(events),
            "max_requests": int(policy["max_requests"]),
            "cost_used": round(sum(e[1] for e in events), 6),
            "max_cost": float(policy["max_cost"]),
            "max_wait_s": float(policy["max_wait_s"]),
            "next_slot_s": max(0.0, next_slot - now),
            "declaration": state.get("declaration") or None,
            "preflight": state.get("preflight") or None,
            "policy_path": self.policy_path,
            "state_path": self.state_path(resource),
        }

    def acquire(
        self,
        resource: str,
        cost: float = 1.0,
        min_interval_s: Optional[float] = None,
    ) -> Grant:
        """Take one slot against `resource`, waiting for the shared cadence.

        `cost` is the weight of this request in budget units — a 5000-row query
        should not debit the same as a 50-row one.

        Raises `CooldownActive`, `BudgetExhausted` or `WaitTooLong` rather than
        letting anything through early. Checks run in that order so a blocked
        host is reported as blocked, not as out of budget.
        """
        cost = _finite(cost, "cost")
        if cost < 0:
            raise ValueError("cost must not be negative")
        if min_interval_s is not None:
            min_interval_s = _finite(min_interval_s, "min_interval_s")
            if min_interval_s < 0:
                raise ValueError("min_interval_s must not be negative")

        policy = self.resource_policy(resource)
        window = float(policy["window_s"])
        max_requests = int(policy["max_requests"])
        max_cost = float(policy["max_cost"])
        max_wait = float(policy["max_wait_s"])

        now = self._time()
        with self.locked(resource):
            state = self._read_state(resource, now)
            floor = self._effective_floor(state, policy, min_interval_s, resource)
            # Persist the ratchet even on the refusal paths below: a caller
            # that declared a stricter interval has raised the floor for
            # everyone, whether or not its own request goes through.
            state["floor_s"] = floor
            self._raise_sidecar_floor(resource, floor)

            cooldown_until = _as_float(state.get("cooldown_until"), 0.0)
            if cooldown_until > now:
                self._write_state(resource, state)
                raise CooldownActive(
                    resource,
                    cooldown_until - now,
                    cooldown_until,
                    state.get("cooldown_reason", ""),
                )

            if policy.get("require_declaration") and not state.get("declaration"):
                self._write_state(resource, state)
                raise Undeclared(resource)

            events = _live_events(state["events"], now, window)
            requests_used = len(events)
            cost_used = sum(e[1] for e in events)

            preflight_after = int(policy.get("preflight_after_requests") or 0)
            if (
                preflight_after
                and requests_used >= preflight_after
                and not state.get("preflight")
            ):
                state["events"] = events
                self._write_state(resource, state)
                raise PreflightRequired(resource, requests_used, preflight_after)

            if requests_used + 1 > max_requests:
                state["events"] = events
                self._write_state(resource, state)
                raise BudgetExhausted(
                    resource,
                    "requests",
                    requests_used + 1,
                    max_requests,
                    window,
                    _retry_after_requests(events, now, window, max_requests),
                )
            if cost_used + cost > max_cost:
                state["events"] = events
                self._write_state(resource, state)
                raise BudgetExhausted(
                    resource,
                    "cost",
                    cost_used + cost,
                    max_cost,
                    window,
                    _retry_after_cost(events, now, window, max_cost, cost, cost_used),
                )

            last_slot = state.get("last_slot")
            slot = now if last_slot is None else max(now, float(last_slot) + floor)
            wait = slot - now
            if wait > max_wait:
                state["events"] = events
                self._write_state(resource, state)
                raise WaitTooLong(resource, wait, max_wait)

            events.append([slot, cost])
            state["events"] = events
            state["last_slot"] = slot
            self._write_state(resource, state)

        if wait > 0:
            self._sleep_until(slot)
            # Pushback can land while this call is parked. Without this
            # re-check the already-reserved slot still fires, so exactly one
            # request goes out AFTER the resource told us to stop — the
            # "just one more" class of incident, and silent.
            with self.locked(resource):
                state = self._read_state(resource, self._time())
                cooldown_until = _as_float(state.get("cooldown_until"), 0.0)
                now_after = self._time()
                if cooldown_until > now_after:
                    raise CooldownActive(
                        resource,
                        cooldown_until - now_after,
                        cooldown_until,
                        state.get("cooldown_reason", ""),
                    )
        granted_at = self._time()
        return Grant(
            resource=resource,
            slot_at=slot,
            granted_at=granted_at,
            waited_s=max(0.0, granted_at - now),
            floor_s=floor,
            cost=cost,
            window_s=window,
            requests_used=len(events),
            max_requests=max_requests,
            cost_used=cost_used + cost,
            max_cost=max_cost,
        )

    # -- helpers ----------------------------------------------------------

    def _sleep_until(self, target: float, max_iterations: int = 1000) -> None:
        """Sleep until wall time reaches `target`, tolerating short sleeps."""
        for _ in range(max_iterations):
            remaining = target - self._time()
            if remaining <= 0:
                return
            self._sleep(remaining)


def _finite(value: Any, name: str) -> float:
    """Reject NaN and infinity at the door.

    Every comparison against NaN is False, so a NaN slips past `cost > max_cost`,
    past `wait > max_wait` and into persisted state, where it poisons the running
    total and disables that ceiling permanently. A limit that silently stops
    applying is worse than no limit, because nothing reports it.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if math.isnan(out) or math.isinf(out):
        raise ValueError(f"{name} must be finite, got {out!r}")
    return out


def _is_finite_number(value: Any) -> bool:
    """True for a real, finite number — bools and NaN/inf are not numbers here."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and not math.isnan(float(value))
        and not math.isinf(float(value))
    )


def _as_float(value: Any, fallback: float) -> float:
    """Coerce a value read from STATE, which is untrusted input.

    State is shared across processes, survives crashes, and may have been
    written by an older version. A NaN reaching a comparison disables that
    comparison silently — `NaN > limit` is False — so non-finite values fall
    back rather than propagate.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return fallback
        return out
    return fallback


def _live_events(events: list[list[float]], now: float, window: float) -> list[list[float]]:
    """Drop events that have rolled out of the window."""
    cutoff = now - window
    return [[float(ts), float(c)] for ts, c in events if float(ts) > cutoff]


def _retry_after_requests(
    events: list[list[float]], now: float, window: float, max_requests: int
) -> float:
    """When the request ceiling next frees a slot."""
    if max_requests <= 0 or not events:
        return window
    need_to_drop = len(events) + 1 - max_requests
    ordered = sorted(e[0] for e in events)
    if need_to_drop > len(ordered):
        return window
    return max(0.0, ordered[need_to_drop - 1] + window - now)


def _retry_after_cost(
    events: list[list[float]],
    now: float,
    window: float,
    max_cost: float,
    cost: float,
    cost_used: float,
) -> float:
    """When the cost ceiling next frees enough room for `cost`."""
    if cost > max_cost or not events:
        return window
    need = cost_used + cost - max_cost
    freed = 0.0
    for ts, event_cost in sorted(events, key=lambda e: e[0]):
        freed += event_cost
        if freed >= need:
            return max(0.0, ts + window - now)
    return window
