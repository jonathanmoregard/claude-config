"""Shared errors for the todo tool's backends."""
from __future__ import annotations


class BackendError(RuntimeError):
    """Raised when a todo backend operation fails."""
