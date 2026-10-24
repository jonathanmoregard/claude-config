#!/usr/bin/env python3
"""Keep `scripts/e2e-run --command` alive until its service is stopped."""

from __future__ import annotations

import signal
import threading


stopping = threading.Event()


def stop(_signum, _frame) -> None:
    stopping.set()


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)
print("klaffat-app-runner: holding app", flush=True)
stopping.wait()
