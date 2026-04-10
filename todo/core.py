"""Public API and backend dispatch for the todo tool."""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Optional, Union

from ._envfile import load as _load_env_file
from .backends.base import Backend, BackendError


def _get_backend(name: Optional[str] = None) -> Backend:
    _load_env_file()
    name = name or os.environ.get("TODO_BACKEND", "ticktick")
    if name == "ticktick":
        from .backends.ticktick import TickTickBackend
        return TickTickBackend.from_env()
    raise BackendError(f"Unknown todo backend: {name!r}")


def add_reminder(
    title: str,
    due: Union[str, date, datetime],
    *,
    list: Optional[str] = None,
    notes: Optional[str] = None,
    backend: Optional[str] = None,
) -> str:
    """Add a reminder with a due date to the configured todo list.

    Args:
        title: Short reminder title shown in the todo list.
        due: Due date. Accepts 'YYYY-MM-DD' (all-day), 'YYYY-MM-DDTHH:MM[:SS]',
             or a datetime/date object.
        list: Optional list/project name or id. Backend-specific. If omitted,
              the backend's default list is used.
        notes: Optional long-form description/notes.
        backend: Override the default backend name.

    Returns:
        Backend-specific identifier for the created reminder (e.g. task id).
    """
    return _get_backend(backend).add_reminder(
        title=title, due=due, list=list, notes=notes
    )
