"""Command-line entry point: ``todo-add``.

Usage:
    todo-add "Title" --due 2026-04-16 [--list Reminders] [--notes ...]
    todo-add --login     # force TickTick re-authorization via the browser
"""
from __future__ import annotations

import argparse
import sys

from .backends.base import BackendError
from .core import _get_backend, add_reminder


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="todo-add",
        description="Add a reminder with a due date to a todo list "
                    "(default backend: TickTick).",
    )
    parser.add_argument("title", nargs="?", help="Reminder title")
    parser.add_argument(
        "--due",
        default=None,
        help="Due date: 'YYYY-MM-DD' (all-day) or 'YYYY-MM-DDTHH:MM[:SS]'",
    )
    parser.add_argument(
        "--list",
        default=None,
        help="List/project name or id (optional; backend default if omitted)",
    )
    parser.add_argument("--notes", default=None, help="Long-form notes (optional)")
    parser.add_argument("--backend", default=None, help="Backend override")
    parser.add_argument(
        "--login",
        action="store_true",
        help="Force interactive re-authorization with the backend (TickTick only).",
    )
    args = parser.parse_args(argv)

    if args.login:
        try:
            backend = _get_backend(args.backend)
        except BackendError as e:
            print(f"todo-add: {e}", file=sys.stderr)
            return 1
        force_login = getattr(backend, "force_login", None)
        if not callable(force_login):
            print(
                f"todo-add: backend {type(backend).__name__} does not support --login",
                file=sys.stderr,
            )
            return 1
        try:
            force_login()
        except BackendError as e:
            print(f"todo-add: {e}", file=sys.stderr)
            return 1
        print("Re-authorized.")
        return 0

    if not args.title or not args.due:
        parser.error("title and --due are required unless --login is given")

    try:
        task_id = add_reminder(
            title=args.title,
            due=args.due,
            list=args.list,
            notes=args.notes,
            backend=args.backend,
        )
    except BackendError as e:
        print(f"todo-add: {e}", file=sys.stderr)
        return 1

    print(task_id or "ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
