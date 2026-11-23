#!/usr/bin/env python3
"""Import ~/.claude, render ~/.codex, check it; publish only what passes.

Production (/etc/nixos/home/ai-client-config-sync.nix) runs this inside a
mount namespace whose HOME is a staged home, and rsyncs the managed paths
into the live home only after it exits 0. A direct run renders in place,
since the output names absolute paths under HOME (hook commands, wrappers,
role files), so it publishes first and checks the result: a failed check
rolls every path the publish may change back to what was there before the
run, config.toml included, as production's backup restore does. So does
SIGTERM, SIGHUP or SIGINT before the check has passed.

One direct run at a time per home: a second one that snapshotted the
first's unchecked output would restore it after its own failed check.
"""
from __future__ import annotations

import fcntl
import os
import signal
import sys
from types import FrameType

import check
import common
import import_claude
import render_codex


class CheckFailed(Exception):
    """The published output failed the check; the rollback undoes it."""


STOP_SIGNALS = render_codex.STOP_SIGNALS
# The home itself, locked as a directory: a lock file would be left behind
# in a home whose first run failed, which the rollback leaves without ~/.codex.
LOCK = common.HOME
# EX_TEMPFAIL: another run holds the home; this one changed nothing.
BUSY = 75


def stop(signum: int, frame: FrameType | None) -> None:
    """A stop signal as an exception, so the rollback runs: by default
    SIGTERM and SIGHUP end the process without unwinding, and the unchecked
    output would stay live. The rollback ends the run with any stop only
    once it has restored, one this raised as it started included; a repeat
    after this is ignored."""
    for other in STOP_SIGNALS:
        signal.signal(other, signal.SIG_IGN)
    raise SystemExit(128 + signum)


def main() -> int:
    lock = os.open(LOCK, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(
                f"ai-client-config sync: another sync holds a lock on {LOCK}; nothing changed",
                file=sys.stderr,
            )
            return BUSY
        return sync()
    finally:
        os.close(lock)


def sync() -> int:
    for signum in STOP_SIGNALS:
        # One the caller ignores (nohup) stays ignored.
        if signal.getsignal(signum) is not signal.SIG_IGN:
            signal.signal(signum, stop)
    import_claude.main()
    plan = render_codex.load_render_plan()
    # config.toml is outside the publish's own rollback set, because Codex
    # writes runtime state into it; a failed check restores it anyway. A
    # Codex write to it between the publish and the check is lost.
    paths = [*render_codex.render_plan_paths(plan), render_codex.CODEX / "config.toml"]
    try:
        # The only rollback: the publish's own would nest in this one.
        with render_codex.rollback_paths_on_error(paths):
            render_codex.publish(plan, rollback=False)
            if check.main() != 0:
                raise CheckFailed
    except CheckFailed:
        print(
            "ai-client-config sync: check failed; every managed path is back "
            "as it was before this run"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
