"""PreToolUse guard (`hooks/bash-guard.py`) — the foreground CI-poll layer.

Invariant: a FOREGROUND Bash call that sleeps and then takes a one-shot CI
status snapshot is denied, with the blocking primitive named; every other
shape — a single status look, `--watch`, the same thing in the background,
a sleep next to unrelated work, the words merely quoted in text — passes.

Run:  python3 tests/test_bash_guard_ci_poll.py
Exit: 0 = all green, 1 = at least one failure.
"""
from __future__ import annotations

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(os.path.dirname(HERE), "hooks", "bash-guard.py")

spec = importlib.util.spec_from_file_location("bash_guard", GUARD)
bg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bg)  # type: ignore[union-attr]

FAILURES: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"  [{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        FAILURES.append(label)


# Real commands lifted from session transcripts (2026-09-12 .. 2026-10-02).
DENY = [
    "sleep 150; gh pr checks 287 -R jonathanmoregard/nixos-config 2>&1 | grep -v -E '\\spass\\s' | cut -c1-110",
    "sleep 20; gh pr checks 92 -R jonathanmoregard/.claude 2>&1 | cut -c1-90",
    "sleep 40; gh pr checks 18 -R jonathanmoregard/voquill 2>&1 | cut -c1-100; python3 x.py refresh abc",
    "sleep 45 && gh pr checks 12 --json name,state,bucket",
    "sleep 60; gh run view 123456 --json status,conclusion",
    "sleep 30; gh run list --branch fix/x --limit 3",
    "while true; do gh pr checks 5; sleep 30; done",
    "until gh pr checks 5 | grep -q pending; do sleep 10; done",
    "sleep 90\ngh pr view 7 --json statusCheckRollup",
    "for i in $(seq 1 19); do OUT=$(gh pr checks 97 2>&1); sleep 30; done",
    "for i in $(seq 1 55); do S=$(gh run view 3639 --json status -q .status); [ \"$S\" = completed ] && break; sleep 10; done",
]

ALLOW = [
    # a single look, no wait
    "gh pr checks 287 -R jonathanmoregard/nixos-config",
    # the blocking primitive, with or without a registration delay
    "gh pr checks 287 --watch --fail-fast",
    "sleep 20; gh pr checks 287 --watch --fail-fast",
    "sleep 45; gh run watch 99 --exit-status",
    # watch first, then print the settled result
    "sleep 20; gh pr checks 11 --watch --interval 30 >/dev/null 2>&1; gh pr checks 11",
    # sleep next to unrelated work
    "sleep 5; git status",
    "gh pr checks 3; git log --oneline -3",
    # the words only appear inside quoted text
    "git commit -m 'stop doing sleep 30; gh pr checks 4 loops'",
    "echo \"sleep 10; gh run list\"",
]


def main() -> int:
    print("== foreground sleep + one-shot CI snapshot is denied ==")
    for cmd in DENY:
        reason = bg._check_ci_poll(cmd, background=False)
        check(f"deny: {cmd[:70]!r}", reason is not None)
        if reason is not None:
            check("  reason names the blocking primitive", "--watch" in reason)

    print("== legitimate shapes pass ==")
    for cmd in ALLOW:
        check(f"allow: {cmd[:70]!r}", bg._check_ci_poll(cmd, background=False) is None)

    print("== the same poll in the background is not this layer's business ==")
    for cmd in DENY[:3]:
        check(f"bg allow: {cmd[:60]!r}", bg._check_ci_poll(cmd, background=True) is None)

    print(f"\n{len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
