"""Global egress guard: one cross-process cadence + budget per resource.

The invariant this package exists to hold: no participant can make the
aggregate faster than the strictest cadence anybody has declared.

Public surface lives in `egress_guard.limiter`.
"""
from __future__ import annotations

from .limiter import (  # noqa: F401
    BudgetExhausted,
    CooldownActive,
    EgressGuard,
    EgressGuardError,
    Grant,
    PolicyError,
    WaitTooLong,
)

__all__ = [
    "BudgetExhausted",
    "CooldownActive",
    "EgressGuard",
    "EgressGuardError",
    "Grant",
    "PolicyError",
    "WaitTooLong",
]
