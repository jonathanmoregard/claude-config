"""Todo list abstraction.

Public API:
    add_reminder(title, due, *, list=None, notes=None, backend=None) -> str

Backends are swappable; the default (TickTick) reads credentials from
environment variables (loaded from ~/.config/todo/env if not already set).
"""
from .core import add_reminder
from .backends.base import BackendError

__all__ = ["add_reminder", "BackendError"]
