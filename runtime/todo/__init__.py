"""TickTick habit access for the wellbeing hooks.

The backend reads credentials from environment variables (loaded from
~/.config/todo/env if not already set). Re-authorize with
``~/.claude/ticktick-login``.
"""
from .backends.base import BackendError

__all__ = ["BackendError"]
