"""Command-line entry point: ``ticktick-login``.

Forces TickTick re-authorization via the browser and stores the new token in
``~/.config/todo/env``. Run it from an interactive terminal.
"""
from __future__ import annotations

import sys

from .backends.base import BackendError
from .backends.ticktick import TickTickBackend


def main() -> int:
    try:
        TickTickBackend.from_env().force_login()
    except BackendError as e:
        print(f"ticktick-login: {e}", file=sys.stderr)
        return 1
    print("Re-authorized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
