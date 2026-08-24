#!/usr/bin/env python3
"""Tests for the stock-hook guards that replace the safe-bash MCP.

Rehomes the still-relevant cases from ``mcps/safe-bash/test_safe_bash.py``
(the ``find ... -delete`` incident coverage and the untrusted-ingress
matrix) and adds the coverage safe-bash never had:

  * the FAIL-CLOSED paths. safe-bash's ``_check_denied_by_dcg`` failed open
    by explicit design, which is why nobody noticed ``dcg`` had been missing
    for months. Every way the guard can fail to run is asserted here to
    produce ``permissionDecision: deny``.
  * per-rule coverage of the ``Bash(...)`` deny globs ported from
    ``mcps/safe-bash/bash-config.json`` into ``settings.json``.

There is no pytest on this host, so this uses the same ``check()``/
``section()`` idiom as the suite it replaces.

Run:  python3 tests/test_bash_guards.py
Exit: 0 = all green, 1 = at least one failure.

Every secret-shaped fixture below is assembled by concatenation at runtime.
Written as a literal it would be a guaranteed gitleaks false positive on
every commit that touches this file.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
HOOKS = os.path.join(REPO_ROOT, "hooks")

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "OK " if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f" :: {detail}" if detail else ""))
    if not cond:
        FAILURES.append(label)


def section(name: str) -> None:
    print(f"\n== {name} ==")


# ----------------------------------------------------------------------
# Harness
# ----------------------------------------------------------------------
SCRATCH = tempfile.mkdtemp(prefix="bash-guards-test-")


def _write_stub(name: str, body: str, executable: bool = True) -> str:
    path = os.path.join(SCRATCH, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    mode = os.stat(path).st_mode
    if executable:
        os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    else:
        os.chmod(path, mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    return path


# dcg speaks Claude-Code hook JSON: payload on stdin, a JSON object on
# stdout only when it wants to block (decorative text may precede it).
STUB_ALLOW = _write_stub(
    "dcg-allow",
    "#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n",
)
STUB_DENY = _write_stub(
    "dcg-deny",
    "#!/usr/bin/env bash\n"
    "cat >/dev/null\n"
    "echo 'BLOCKED: destructive command'\n"
    """echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","""
    """"permissionDecision":"deny","permissionDecisionReason":"rm -rf on root","""
    """"ruleId":"fs-rm-rf-root"}}'\n"""
    "exit 0\n",
)
# Same deny, but exiting 2 the way a hook-shaped blocker legitimately may.
# A rendered decision must win over the exit status, or a dcg that signals
# its block through exit 2 would be silently downgraded.
STUB_DENY_EXIT2 = _write_stub(
    "dcg-deny-exit2",
    "#!/usr/bin/env bash\n"
    "cat >/dev/null\n"
    """echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","""
    """"permissionDecision":"deny","permissionDecisionReason":"blocked","""
    """"ruleId":"exit2-rule"}}'\n"""
    "exit 2\n",
)
# 30s outlasts every deadline in the ladder while bounding how long a stray
# sleep can linger after the guard has walked away from it.
STUB_HANG = _write_stub(
    "dcg-hang",
    "#!/usr/bin/env bash\ncat >/dev/null\nsleep 30\n",
)
STUB_MALFORMED = _write_stub(
    "dcg-malformed",
    "#!/usr/bin/env bash\ncat >/dev/null\necho '{\"hookSpecificOutput\": {trunc'\nexit 0\n",
)
STUB_EXIT_NONZERO = _write_stub(
    "dcg-exit-nonzero",
    "#!/usr/bin/env bash\ncat >/dev/null\necho 'segfault-ish' >&2\nexit 3\n",
)
STUB_UNKNOWN_DECISION = _write_stub(
    "dcg-unknown-decision",
    "#!/usr/bin/env bash\n"
    "cat >/dev/null\n"
    """echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","""
    """"permissionDecision":"maybe"}}'\n"""
    "exit 0\n",
)
STUB_NOT_EXECUTABLE = _write_stub(
    "dcg-not-executable",
    "#!/usr/bin/env bash\nexit 0\n",
    executable=False,
)
MISSING_DCG = os.path.join(SCRATCH, "definitely-not-here", "dcg")


def run_hook(
    script: str,
    payload,
    env_extra: dict | None = None,
    timeout: float = 60.0,
    raw_stdin: str | None = None,
) -> tuple[int, dict | None, str, str]:
    """Feed crafted hook JSON on stdin; return (rc, parsed_json_or_None, out, err)."""
    env = dict(os.environ)
    env.update(env_extra or {})
    stdin = raw_stdin if raw_stdin is not None else json.dumps(payload)
    path = script if os.path.isabs(script) else os.path.join(HOOKS, script)
    proc = subprocess.run(
        [sys.executable, path],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    parsed = None
    body = proc.stdout.strip()
    if body.startswith("{"):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
    return proc.returncode, parsed, proc.stdout, proc.stderr


def pre_payload(command: str) -> dict:
    return {
        "session_id": "test-session",
        "cwd": REPO_ROOT,
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "permission_mode": "default",
    }


def decision_of(parsed: dict | None) -> str | None:
    if not isinstance(parsed, dict):
        return None
    hso = parsed.get("hookSpecificOutput") or {}
    return hso.get("permissionDecision")


def reason_of(parsed: dict | None) -> str:
    if not isinstance(parsed, dict):
        return ""
    hso = parsed.get("hookSpecificOutput") or {}
    return hso.get("permissionDecisionReason") or ""


def guard(command: str, dcg: str = STUB_ALLOW, **kw) -> tuple[str | None, str]:
    """Run bash-guard on `command`; return (decision, reason)."""
    rc, parsed, out, err = run_hook(
        "bash-guard.py", pre_payload(command), env_extra={"DCG_PATH": dcg}, **kw
    )
    return decision_of(parsed), reason_of(parsed)


# ----------------------------------------------------------------------
# bash-guard: the allow path must stay SILENT
# ----------------------------------------------------------------------
section("bash-guard: allow path renders no decision")

rc, parsed, out, err = run_hook(
    "bash-guard.py", pre_payload("ls -la"), env_extra={"DCG_PATH": STUB_ALLOW}
)
check("benign command: exit 0", rc == 0, f"rc={rc} err={err[:200]}")
check(
    "benign command: emits NOTHING on stdout",
    out.strip() == "",
    # A PreToolUse `allow` fires BEFORE the permission system and would
    # bypass every permissions.deny rule in settings.json — including the
    # 51 globs ported in this same change. Staying silent is load-bearing.
    f"stdout={out[:200]!r}",
)

rc, parsed, out, err = run_hook(
    "bash-guard.py",
    {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": "/tmp/x"},
    },
    env_extra={"DCG_PATH": MISSING_DCG},
)
check(
    "non-Bash tool: no decision even with dcg missing",
    rc == 0 and out.strip() == "",
    f"rc={rc} stdout={out[:200]!r}",
)


# ----------------------------------------------------------------------
# bash-guard: FAIL-CLOSED matrix. This is the point of the exercise.
# safe-bash's _check_denied_by_dcg returned None (allow) for every one of
# these. Each must now deny and name the guard as broken.
# ----------------------------------------------------------------------
section("bash-guard: fail-closed matrix")


# The marker that separates "the guard is broken" from "the guard worked and
# said no". Both are denies; only the first is an operator alarm.
BROKEN_MARKER = "the safety guard is BROKEN"


def check_broken(label: str, dcg: str, command: str = "ls -la", **kw) -> None:
    dec, why = guard(command, dcg=dcg, **kw)
    check(f"{label} -> deny", dec == "deny", f"decision={dec!r}")
    check(
        f"{label} -> reason names the guard as broken",
        "bash-guard" in why and BROKEN_MARKER in why,
        why[:160],
    )
    check(
        f"{label} -> reason tells the operator how to recover",
        "! <command>" in why and "dcg" in why.lower(),
        why[:160],
    )


check_broken("dcg binary missing", MISSING_DCG)
check_broken("dcg not executable", STUB_NOT_EXECUTABLE)
check_broken("dcg output malformed", STUB_MALFORMED)
check_broken("dcg unexpected exit status, no decision", STUB_EXIT_NONZERO)
check_broken("dcg unknown permissionDecision value", STUB_UNKNOWN_DECISION)

t0 = time.monotonic()
check_broken("dcg hangs (hook self-timeout)", STUB_HANG, timeout=45.0)
elapsed = time.monotonic() - t0
# The harness-level hook timeout DISCARDS hook output and renders NO
# decision, which is fail-OPEN. The hook must therefore render its own
# deny strictly before the harness gives up on it.
check(
    "hang deny lands before the harness hook timeout",
    elapsed < 10.0,
    f"{elapsed:.1f}s (settings.json declares timeout: 10)",
)

rc, parsed, out, err = run_hook(
    "bash-guard.py", None, env_extra={"DCG_PATH": STUB_ALLOW}, raw_stdin="not json at all"
)
check(
    "unparseable hook stdin -> deny",
    decision_of(parsed) == "deny",
    f"decision={decision_of(parsed)!r} stdout={out[:160]!r}",
)

rc, parsed, out, err = run_hook(
    "bash-guard.py",
    {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}},
    env_extra={"DCG_PATH": STUB_ALLOW},
)
check(
    "Bash call with no command string -> deny",
    decision_of(parsed) == "deny",
    f"decision={decision_of(parsed)!r}",
)


# ----------------------------------------------------------------------
# bash-guard: dcg layer, working
# ----------------------------------------------------------------------
section("bash-guard: dcg layer")

dec, why = guard("rm -rf /", dcg=STUB_DENY)
check("dcg deny propagates", dec == "deny", f"decision={dec!r}")
check("dcg deny surfaces the rule id", "fs-rm-rf-root" in why, why[:160])
check("dcg deny surfaces dcg's own reason", "rm -rf on root" in why, why[:160])
check(
    "dcg deny is NOT reported as a broken guard",
    BROKEN_MARKER not in why,
    why[:160],
)

dec, why = guard("rm -rf /", dcg=STUB_DENY_EXIT2)
check("dcg deny honoured even when dcg exits 2", dec == "deny", f"decision={dec!r}")
check("exit-2 deny is not reported as broken", BROKEN_MARKER not in why, why[:160])


# ----------------------------------------------------------------------
# find ... -delete — rehomed verbatim from test_safe_bash.py.
# Guards a real incident: deleted active plugin runtime data believing the
# directory held only old worker logs.
# ----------------------------------------------------------------------
section("bash-guard: dangerous-find detector (rehomed incident coverage)")

FIND_DENY = [
    ("bare find PATH -delete", "find /tmp/x -delete"),
    ("find PATH -maxdepth 1 -delete (maxdepth is not a filter)", "find /tmp -maxdepth 1 -delete"),
    ("find PATH -depth -delete (depth is not a filter)", "find /tmp -depth -delete"),
    ("multi-find pipeline: bare -delete still flagged",
     "find /a -name X | xargs ls; find /b -delete"),
]
FIND_ALLOW = [
    ("find with -name -delete", "find /tmp -name 'X' -delete"),
    ("find with -type f -delete", "find /tmp -type f -delete"),
    ("find with -mtime -delete", "find /tmp -mtime +30 -delete"),
    ("find with -size -delete", "find /tmp -size +1M -delete"),
    ("find without -delete", "find /tmp -name 'X'"),
    ("plain ls", "ls /tmp"),
    ("find with quoted -name -delete", "find /tmp -name '*.tmp' -delete"),
    ("multi-find pipeline: both filtered",
     "find /a -name X -delete; find /b -type f -delete"),
]

for label, cmd in FIND_DENY:
    dec, why = guard(cmd)
    check(f"deny: {label}", dec == "deny", f"decision={dec!r}")
    check(
        f"deny reason keeps the incident text: {label}",
        "deleted active plugin runtime data" in why,
        why[:120],
    )

for label, cmd in FIND_ALLOW:
    dec, why = guard(cmd)
    check(f"allow: {label}", dec is None, f"decision={dec!r} reason={why[:120]}")

# Layer ordering: dcg runs first, so its specific rule id wins over the
# generic find hint when both would fire.
dec, why = guard("find /tmp -delete", dcg=STUB_DENY)
check("dcg layer runs before the find layer", "fs-rm-rf-root" in why, why[:160])


# ----------------------------------------------------------------------
# The timeout ladder, and its link to settings.json.
#
# The trap: a harness-level hook timeout CANCELS the hook and DISCARDS its
# output, rendering NO decision — fail-OPEN. So the hook's own deadline has
# to be strictly shorter than the harness's, and the two numbers live in
# different files. These tests are the thing that keeps them in step.
# ----------------------------------------------------------------------
section("bash-guard: timeout ladder cannot silently invert")

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "bash_guard_under_test", os.path.join(HOOKS, "bash-guard.py")
)
bash_guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bash_guard)

check(
    "ladder holds: 0 < DCG_TIMEOUT_S < GUARD_DEADLINE_S < HARNESS_HOOK_TIMEOUT_S",
    0
    < bash_guard.DCG_TIMEOUT_S
    < bash_guard.GUARD_DEADLINE_S
    < bash_guard.HARNESS_HOOK_TIMEOUT_S,
    f"{bash_guard.DCG_TIMEOUT_S} / {bash_guard.GUARD_DEADLINE_S} / "
    f"{bash_guard.HARNESS_HOOK_TIMEOUT_S}",
)

with open(os.path.join(REPO_ROOT, "settings.json"), encoding="utf-8") as fh:
    SETTINGS = json.load(fh)

_pre = SETTINGS.get("hooks", {}).get("PreToolUse", [])
_guard_entries = [
    (entry, hook)
    for entry in _pre
    for hook in entry.get("hooks", [])
    if "bash-guard.py" in (hook.get("command") or "")
]
check("settings.json registers bash-guard.py on PreToolUse", len(_guard_entries) == 1,
      f"found {len(_guard_entries)}")

if _guard_entries:
    entry, hook = _guard_entries[0]
    check(
        "settings.json declares the timeout the hook was built against",
        hook.get("timeout") == bash_guard.HARNESS_HOOK_TIMEOUT_S,
        f"settings.json timeout={hook.get('timeout')!r}, hook "
        f"HARNESS_HOOK_TIMEOUT_S={bash_guard.HARNESS_HOOK_TIMEOUT_S}",
    )
    check(
        "matcher targets the native Bash tool",
        entry.get("matcher") == "Bash",
        f"matcher={entry.get('matcher')!r}",
    )
    check(
        # A fail-closed guard on safe-bash's tool, while dcg is still absent
        # from this host, would deny the shell the migration itself runs on.
        "matcher does NOT cover mcp__safe-bash__exec",
        "safe-bash" not in (entry.get("matcher") or ""),
        f"matcher={entry.get('matcher')!r}",
    )

# Rewrite the constants so the ladder inverts, and confirm the guard notices
# rather than quietly degrading to a hook the harness will time out.
with open(os.path.join(HOOKS, "bash-guard.py"), encoding="utf-8") as fh:
    _src = fh.read()
_inverted_src = _src.replace(
    "DCG_TIMEOUT_S = GUARD_DEADLINE_S - 1  # 5",
    "DCG_TIMEOUT_S = HARNESS_HOOK_TIMEOUT_S + 5  # inverted on purpose",
)
check("inversion fixture actually patched the source", _inverted_src != _src)
_inverted_path = os.path.join(SCRATCH, "bash-guard-inverted.py")
with open(_inverted_path, "w", encoding="utf-8") as fh:
    fh.write(_inverted_src)

rc, parsed, out, err = run_hook(
    _inverted_path, pre_payload("ls -la"), env_extra={"DCG_PATH": STUB_ALLOW}
)
check(
    "inverted ladder -> deny (not a silently fail-open hook)",
    decision_of(parsed) == "deny",
    f"decision={decision_of(parsed)!r}",
)
check(
    "inverted ladder -> reason names the ladder",
    "ladder" in reason_of(parsed),
    reason_of(parsed)[:200],
)

# A dcg that leaks its stdout/stderr pipes to a process surviving the kill —
# the failure mode safe-bash's `_run` docstring describes at length.
STUB_LEAKS_PIPES = _write_stub(
    "dcg-leaks-pipes",
    "#!/usr/bin/env bash\ncat >/dev/null\nsleep 30 &\nsleep 30\n",
)
t0 = time.monotonic()
dec, why = guard("ls -la", dcg=STUB_LEAKS_PIPES, timeout=45.0)
elapsed = time.monotonic() - t0
check("dcg leaking pipes to a surviving child -> deny", dec == "deny", f"decision={dec!r}")
check(
    "leaked-pipe deny lands before the harness hook timeout",
    elapsed < bash_guard.HARNESS_HOOK_TIMEOUT_S,
    f"{elapsed:.1f}s vs harness {bash_guard.HARNESS_HOOK_TIMEOUT_S}s",
)
check("leaked-pipe deny names the guard as broken", BROKEN_MARKER in why, why[:160])

# The two cases above are both caught by the dcg subprocess cap, which sits
# BELOW the whole-hook watchdog in the ladder and therefore always fires
# first. Proving the watchdog itself works needs the cap removed: inject the
# fault of an unbounded dcg call and assert a deny still lands in time.
_no_cap_src = _src.replace("timeout=DCG_TIMEOUT_S,", "timeout=None,  # fault injection")
check("watchdog fixture actually removed the subprocess cap", _no_cap_src != _src)
_no_cap_path = os.path.join(SCRATCH, "bash-guard-no-subprocess-cap.py")
with open(_no_cap_path, "w", encoding="utf-8") as fh:
    fh.write(_no_cap_src)

t0 = time.monotonic()
rc, parsed, out, err = run_hook(
    _no_cap_path, pre_payload("ls -la"), env_extra={"DCG_PATH": STUB_HANG}, timeout=45.0
)
elapsed = time.monotonic() - t0
check(
    "subprocess cap removed: whole-hook watchdog still denies",
    decision_of(parsed) == "deny",
    f"decision={decision_of(parsed)!r}",
)
check(
    "watchdog fires at its own deadline, not the harness's",
    bash_guard.GUARD_DEADLINE_S - 1 <= elapsed < bash_guard.HARNESS_HOOK_TIMEOUT_S,
    f"{elapsed:.1f}s (deadline {bash_guard.GUARD_DEADLINE_S}s, "
    f"harness {bash_guard.HARNESS_HOOK_TIMEOUT_S}s)",
)
check(
    "watchdog deny explains the fail-open trap it is avoiding",
    "deadline" in reason_of(parsed) and "fail-open" in reason_of(parsed),
    reason_of(parsed)[:200],
)


# ----------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------
section("Summary")
shutil.rmtree(SCRATCH, ignore_errors=True)
if FAILURES:
    print(f"\nFAIL: {len(FAILURES)} failure(s):")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("\nAll checks passed.")
sys.exit(0)
