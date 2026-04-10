"""Backend protocol and shared errors for the todo tool."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional, Protocol, Union


class BackendError(RuntimeError):
    """Raised when a todo backend operation fails."""


class Backend(Protocol):
    def add_reminder(
        self,
        *,
        title: str,
        due: Union[str, date, datetime],
        list: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> str: ...
