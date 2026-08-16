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

There is no override flag, no force parameter and no bypass environment
variable. Widening a limit means editing the policy file.

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
    },
    "resources": {},
}

# A reservation further ahead than this is not a queue, it is corruption or a
# clock jump. Treat it as "a request just went out" instead of blocking for
# years. Generous enough that a legitimately deep queue is never discarded.
SANITY_HORIZON_S = 86400.0

_NUMERIC_FIELDS = (
    "min_interval_s",
    "window_s",
    "max_requests",
    "max_cost",
    "max_wait_s",
    "block_cooldown_s",
)

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
            if value < 0:
                raise PolicyError(
                    f"egress policy: {resource!r}.{field} must not be negative."
                )
        merged["max_requests"] = int(merged["max_requests"])
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
            return self._fresh_state(resource)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
            state = self._fresh_state(resource)
            state["last_slot"] = now
            return state
        if not isinstance(raw, dict):
            state = self._fresh_state(resource)
            state["last_slot"] = now
            return state

        state = self._fresh_state(resource)
        state["floor_s"] = _as_float(raw.get("floor_s"), 0.0)
        state["cooldown_until"] = _as_float(raw.get("cooldown_until"), 0.0)
        reason = raw.get("cooldown_reason")
        state["cooldown_reason"] = reason if isinstance(reason, str) else ""

        last_slot = raw.get("last_slot")
        if isinstance(last_slot, (int, float)) and not isinstance(last_slot, bool):
            last_slot = float(last_slot)
            # A reservation past the horizon is corruption or a clock jump.
            # Collapse it to "now" — conservative (one more floor of wait)
            # without wedging the resource for years.
            state["last_slot"] = now if last_slot > now + SANITY_HORIZON_S else last_slot
        else:
            state["last_slot"] = None

        events: list[list[float]] = []
        for item in raw.get("events") or []:
            if (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in item)
            ):
                ts = float(item[0])
                if ts > now + SANITY_HORIZON_S:
                    ts = now
                events.append([ts, float(item[1])])
        state["events"] = events
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

    @staticmethod
    def _effective_floor(
        state: dict[str, Any], policy: dict[str, Any], declared: Optional[float]
    ) -> float:
        """Strictest wins. The caller's own interval can only raise the floor."""
        candidates = [float(policy["min_interval_s"]), _as_float(state.get("floor_s"), 0.0)]
        if declared is not None:
            candidates.append(float(declared))
        return max(candidates)

    # -- public API -------------------------------------------------------

    def declare(self, resource: str, min_interval_s: float) -> float:
        """Register `resource` and ratchet its floor up to `min_interval_s`.

        Returns the effective floor, which may be stricter than what was
        declared — a slower participant, or the policy file, always wins.
        """
        min_interval_s = float(min_interval_s)
        if min_interval_s < 0:
            raise ValueError("min_interval_s must not be negative")
        policy = self.resource_policy(resource)
        now = self._time()
        with self.locked(resource):
            state = self._read_state(resource, now)
            floor = self._effective_floor(state, policy, min_interval_s)
            state["floor_s"] = floor
            self._write_state(resource, state)
        return floor

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
        policy = self.resource_policy(resource)
        if cooldown_s is None:
            cooldown_s = float(policy["block_cooldown_s"])
        cooldown_s = float(cooldown_s)
        if cooldown_s < 0:
            raise ValueError("cooldown_s must not be negative")
        now = self._time()
        with self.locked(resource):
            state = self._read_state(resource, now)
            until = max(_as_float(state.get("cooldown_until"), 0.0), now + cooldown_s)
            if until > _as_float(state.get("cooldown_until"), 0.0):
                state["cooldown_reason"] = reason
            state["cooldown_until"] = until
            self._write_state(resource, state)
        return until

    def status(self, resource: str) -> dict[str, Any]:
        """Current floor, cooldown and budget consumption for `resource`."""
        policy = self.resource_policy(resource)
        now = self._time()
        registered = os.path.exists(self.state_path(resource))
        state = self._read_state(resource, now)
        floor = self._effective_floor(state, policy, None)
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
        cost = float(cost)
        if cost < 0:
            raise ValueError("cost must not be negative")
        if min_interval_s is not None:
            min_interval_s = float(min_interval_s)
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
            floor = self._effective_floor(state, policy, min_interval_s)
            # Persist the ratchet even on the refusal paths below: a caller
            # that declared a stricter interval has raised the floor for
            # everyone, whether or not its own request goes through.
            state["floor_s"] = floor

            cooldown_until = _as_float(state.get("cooldown_until"), 0.0)
            if cooldown_until > now:
                self._write_state(resource, state)
                raise CooldownActive(
                    resource,
                    cooldown_until - now,
                    cooldown_until,
                    state.get("cooldown_reason", ""),
                )

            events = _live_events(state["events"], now, window)
            requests_used = len(events)
            cost_used = sum(e[1] for e in events)

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


def _as_float(value: Any, fallback: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
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
