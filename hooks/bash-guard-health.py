#!/usr/bin/env python3
"""SessionStart health check for the fail-closed bash guards.

``dcg`` was ``cargo install``-ed into ``~/.local/bin`` on the old Linux Mint
box. The NixOS migration dropped the binary and its config symlink, and
because safe-bash's dcg layer failed open by design, nothing said a word.
The richest deny layer sat inert for months.

This hook is the thing that would have said so. It checks, at every session
start, that each piece the guards depend on is actually there:

  * the ``dcg`` binary exists and is executable
  * ``dcg``'s config exists (its absence leaves dcg running with none of the
    operator's block rules)
  * the ingress vocabulary ``hooks/bash-guard-config.json`` exists (without
    it ``bash-output-guard.py`` withholds the output of every Bash command)

Healthy means silence. Anything else is reported to the user through
``systemMessage`` and to the model through ``additionalContext``, because
the operator needs to know their guard is down and the model needs to know
why its commands are about to be refused.

It never blocks. Exit 2 does nothing on ``SessionStart`` anyway, and a
health check that could wedge startup would be worse than the fault it
reports.
"""
from __future__ import annotations

import json
import os
import shutil
import sys

LEGACY_DCG_PATH = "/home/jonathan/.local/bin/dcg"
DEFAULT_DCG_CONFIG = os.path.expanduser("~/.config/dcg/config.toml")
DEFAULT_INGRESS_CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "bash-guard-config.json"
)


def _resolve_dcg() -> str:
    """Same resolution order bash-guard.py uses, so both agree on the path."""
    explicit = os.environ.get("DCG_PATH")
    if explicit:
        return explicit
    found = shutil.which("dcg")
    if found:
        return found
    return LEGACY_DCG_PATH


def _faults() -> list[str]:
    out: list[str] = []

    dcg = _resolve_dcg()
    if not os.path.exists(dcg):
        out.append(
            f"dcg is ABSENT (looked at {dcg}). bash-guard.py fails closed, so every "
            "Bash command will be denied until dcg is back on PATH. It is packaged "
            "declaratively in nixos-config — check that the deploy landed."
        )
    elif not os.access(dcg, os.X_OK):
        out.append(
            f"dcg at {dcg} exists but is NOT EXECUTABLE, so every Bash command will "
            "be denied until its mode is fixed."
        )

    dcg_config = os.environ.get("DCG_CONFIG") or DEFAULT_DCG_CONFIG
    if not os.path.exists(dcg_config):
        out.append(
            f"dcg's config is MISSING at {dcg_config}. dcg would run with none of the "
            "operator's block rules — the same silent inertness that hid its "
            "disappearance last time. It is generated from the repo-tracked "
            "~/.claude/dcg.toml."
        )

    ingress = os.environ.get("BASH_GUARD_CONFIG") or DEFAULT_INGRESS_CONFIG
    if not os.path.exists(ingress):
        out.append(
            f"the untrusted-ingress config bash-guard-config.json is MISSING at "
            f"{ingress}. bash-output-guard.py fails closed, so it will WITHHOLD the "
            "output of every Bash command until the file is restored."
        )

    return out


def main() -> int:
    try:
        # Payload is read and discarded: this check depends on the filesystem,
        # not on anything the session tells us. Draining stdin keeps the
        # harness from seeing a broken pipe.
        sys.stdin.read()
    except (OSError, ValueError):
        pass

    try:
        faults = _faults()
    except OSError as exc:
        faults = [f"the health check itself could not inspect the filesystem: {exc}"]

    if not faults:
        return 0

    bullets = "\n".join(f"  - {f}" for f in faults)
    message = (
        "BASH GUARD HEALTH: the fail-closed bash guards are not fully armed.\n"
        f"{bullets}\n"
        "Until this is fixed, Bash commands are denied or their output withheld. "
        "You can still run a command yourself in the prompt with `! <command>`."
    )
    json.dump(
        {
            "systemMessage": message,
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": (
                    "Guard health at session start: " + " ".join(faults)
                ),
            },
        },
        sys.stdout,
    )
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        # A broken health check must not be the thing that breaks startup.
        sys.exit(0)
