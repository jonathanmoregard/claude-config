#!/usr/bin/env python3
"""PreToolUse guard for native ``Bash``. FAIL-CLOSED.

Replaces the deny path of the ``safe-bash`` MCP with a stock hook. A
``PreToolUse`` deny outranks every permission mode and gates subagent tool
calls too (measured on CLI 2.1.222), which is what the MCP existed for.

Layers, in order:

  1. ``dcg`` (destructive-command guard, a Rust binary speaking hook JSON)
  2. ``find ... -delete`` with no filter predicate
  3. a network fetch piped into a shell (``curl ... | bash``)  [arftl-allow: curl-pipe-shell]
     — documentation of the shape, not a command: the matcher is
     ``_check_pipe_to_shell``, which reads tokenised pipeline stages and never
     this literal. The intent of six ported deny rules that the CLI's matcher
     structurally cannot enforce
  4. the nixos-config push gate (``Pre-push checklist:`` trailer, with its
     claim-vs-diff and merge-base cross-checks)
  5. allow — by emitting NOTHING

Layer 5 is silent on purpose. ``PreToolUse`` fires BEFORE the permission
system, so returning ``permissionDecision: "allow"`` would bypass every
``permissions.deny`` rule in settings.json — including the ``Bash(...)``
globs this migration ported there. A hook that has nothing to say must say
nothing.

FAIL-CLOSED, and that is the whole point of this file
-----------------------------------------------------
safe-bash's ``_check_denied_by_dcg`` returned "allow" whenever dcg was
missing or misbehaving — fail-open by explicit design. The consequence was
that ``dcg`` vanished during the NixOS migration and the first and richest
deny layer sat inert for months without a single symptom. Every way this
guard can fail to reach a verdict now DENIES:

  * dcg binary absent, or present but not executable
  * dcg present and answering, but its user config did not load — absent
    or unparseable. dcg falls back to built-in packs, still denies
    `rm -rf /`, and permits every rule the operator wrote in
    `[overrides] block`. Present and answering is not guarding.
  * dcg exceeded the guard's own timeout
  * dcg emitted output that could not be parsed
  * dcg exited non-zero without rendering a decision
  * dcg rendered a decision value the guard does not understand
  * hook stdin was unparseable, or carried no command to check
  * a pipeline stage downstream of a network fetch could not be tokenised, so
    the guard could not tell whether the fetch feeds a shell
  * the nixos push gate could not run a git command it needed (git absent,
    hung, or refused by the OS), could not read the HEAD commit message,
    could not diff the branch against either origin/main or main, or could
    not resolve the refs its `Rebased on origin/main` check compares
  * any unhandled exception anywhere in this file
  * the guard's own timeout constants no longer hold their invariant

There is deliberately NO agent-reachable bypass environment variable. The
operator's escape is to run the command themselves with ``! <cmd>``.

``DCG_PATH`` is an operator/test seam, not a bypass: it is read from the
hook process's environment, which is the harness environment, and a Bash
tool call cannot set it — each call gets a fresh shell whose exports die
with it.

Timeouts, and the trap they hide
--------------------------------
A harness-level hook timeout CANCELS the hook and DISCARDS its output, so
NO decision is rendered — which is fail-OPEN. A fail-closed guard can
therefore never lean on the harness timeout; it must render its own deny
strictly earlier. The three constants below encode that ordering, the
module refuses to run if the ordering ever inverts, and
``tests/test_bash_guards.py`` asserts that settings.json still declares
``HARNESS_HOOK_TIMEOUT_S`` for this hook.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

# --- Timeout ladder. Read this as one unit; the invariant below is checked
# --- at runtime and asserted against settings.json by the test suite.
#
#   HARNESS_HOOK_TIMEOUT_S  what settings.json declares for this hook. When
#                           it expires the harness discards our output and
#                           renders NO decision => fail-OPEN. Never reach it.
#   GUARD_DEADLINE_S        whole-hook watchdog. We emit our own deny here.
#   DCG_TIMEOUT_S           cap on the dcg subprocess alone, so the normal
#                           hang path is handled with a clean kill rather
#                           than by the watchdog.
#   NIXOS_GIT_TIMEOUT_S     cap on any ONE git call made by the nixos push
#                           gate. It is a ceiling, not an allowance: every
#                           call is additionally clamped to whatever is left
#                           of GUARD_DEADLINE_S, so the gate cannot spend
#                           budget the watchdog has already committed. On
#                           this host the whole gate measures ~0.3s.
HARNESS_HOOK_TIMEOUT_S = 10
GUARD_DEADLINE_S = HARNESS_HOOK_TIMEOUT_S - 4  # 6
DCG_TIMEOUT_S = GUARD_DEADLINE_S - 1  # 5
NIXOS_GIT_TIMEOUT_S = 4

# The SECOND deny channel, for when the first one cannot be written.
#
# Exit status 2 is the ONLY non-zero status that blocks. Measured in the CLI
# 2.1.251 bundle: at offset 180961465 the hook runner maps `nr.status === 2`
# to `yield {blockingError: {...`[hook]`: ${nr.stderr || "No stderr output"}`},
# outcome:"blocking"}`, and the PreToolUse consumer turns a blockingError into
# `{kind:"deny", reason: ...}`. Every OTHER non-zero status yields
# `hook_non_blocking_error` and execution CONTINUES — so exit 1, or the 120
# CPython returns when its shutdown flush fails, are both ALLOW.
#
# The one downgrade path, `L1t` at offset 180925958, fires only for
# Stop / SubagentStop / TaskCompleted / TeammateIdle (and plugin
# UserPromptSubmit) with empty stdout and a "no such file" stderr. PreToolUse
# is not in that list, so exit 2 from this hook always blocks — including when
# a partial write left unparseable JSON on stdout, because the status-2 branch
# is reached whether or not stdout parsed.
HOOK_BLOCK_EXIT_CODE = 2

# Strict ordering. If a future edit inverts it — say by raising
# DCG_TIMEOUT_S past the harness timeout — the guard denies every command
# instead of silently degrading to fail-open. Loud beats inert.
_TIMEOUTS_SANE = (
    0 < DCG_TIMEOUT_S < GUARD_DEADLINE_S < HARNESS_HOOK_TIMEOUT_S
    and 0 < NIXOS_GIT_TIMEOUT_S < GUARD_DEADLINE_S
)

# Margin kept between the last subprocess cap and the watchdog, so a stalled
# child is killed and its deny WRITTEN rather than the watchdog interrupting
# mid-decision.
_BUDGET_SLACK_S = 0.5

LEGACY_DCG_PATH = "/home/jonathan/.local/bin/dcg"

_RECOVERY = (
    "Recover: install dcg (packaged declaratively in nixos-config; verify with "
    "`command -v dcg`) and confirm ~/.config/dcg/config.toml exists. "
    "There is no bypass flag — to run this command anyway, run it yourself in the "
    "prompt with `! <command>`."
)


def _escalate_to_exit_code(reason: str) -> None:
    """Deny via exit status, because stdout could not carry the decision.

    Never returns. This is the fail-closed path for the failure of the
    fail-closed path itself: with stdout unwritable there is no way to render
    a decision object, and a PreToolUse hook that renders no decision is
    ALLOWED. Exit 2 needs no stdout at all, and the CLI uses this hook's
    STDERR as the deny reason, so the operator still gets the explanation.

    stderr is attempted first and its own failure is irrelevant — the exit
    status alone blocks; the text is only there to say why.
    """
    try:
        sys.stderr.write(reason + "\n")
        sys.stderr.flush()
    except Exception:
        # arftl-allow: swallowed-exception — the os._exit below denies whether
        # or not this text lands; there is no permissive outcome to reach.
        pass
    os._exit(HOOK_BLOCK_EXIT_CODE)


def _emit_deny(reason: str) -> None:
    """Write the deny decision and flush. Never raises, never falls open.

    A swallowed write error here used to be a silent ALLOW: every deny in
    this file funnels through this function, and callers follow it with
    ``os._exit(0)`` / ``return 0``. Measured with stdout on /dev/full — the
    guard exited 0 with empty stdout AND empty stderr, so the harness rendered
    no decision and the command ran. That is the exact fail-open class this
    file exists to eliminate, arriving through the one function whose job is
    to close it.
    """
    try:
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )
        sys.stdout.flush()
    except Exception as exc:
        _escalate_to_exit_code(
            f"bash-guard: DENIED. The decision could not be written to stdout "
            f"({type(exc).__name__}: {exc}), so this deny is being delivered by "
            f"exit status {HOOK_BLOCK_EXIT_CODE} instead. The original reason "
            f"follows.\n{reason}"
        )


def _deny_broken(detail: str) -> str:
    return (
        "bash-guard: DENIED because the safety guard is BROKEN, not because the "
        "command is. A guard that cannot run refuses rather than waves through.\n"
        f"Fault: {detail}\n" + _RECOVERY
    )


def _fail_closed(detail: str) -> None:
    """Emit the broken-guard deny and leave immediately.

    ``os._exit`` skips interpreter shutdown on purpose: this is also the
    watchdog's exit path, and normal shutdown could block on the very
    subprocess whose hang triggered it.
    """
    _emit_deny(_deny_broken(detail))
    os._exit(0)


def _on_deadline(signum, frame) -> None:  # noqa: ARG001
    _fail_closed(
        f"the guard exceeded its own {GUARD_DEADLINE_S}s deadline. The harness hook "
        f"timeout ({HARNESS_HOOK_TIMEOUT_S}s) would have discarded this hook's output "
        "and rendered NO decision, which is fail-open; this deny is emitted first "
        "precisely so that cannot happen."
    )


_DEADLINE_AT: float | None = None


def _remaining_budget() -> float:
    """Seconds of whole-hook deadline left, minus the write-out margin.

    Every subprocess the guard starts after dcg draws from this, so that a
    slow first layer shortens the second rather than the two summing past
    GUARD_DEADLINE_S and landing in the watchdog.
    """
    if _DEADLINE_AT is None:  # watchdog never armed (direct import, tests)
        return float(NIXOS_GIT_TIMEOUT_S)
    return _DEADLINE_AT - time.monotonic() - _BUDGET_SLACK_S


def _arm_watchdog() -> None:
    global _DEADLINE_AT
    _DEADLINE_AT = time.monotonic() + GUARD_DEADLINE_S
    try:
        signal.signal(signal.SIGALRM, _on_deadline)
        signal.setitimer(signal.ITIMER_REAL, GUARD_DEADLINE_S)
    except (ValueError, OSError, AttributeError) as exc:
        # No SIGALRM (non-POSIX, or not the main thread). This used to carry
        # on regardless, reasoning that the dcg subprocess timeout still
        # bounds the dominant blocking call. It does not bound the rest: with
        # the watchdog unarmed `_remaining_budget()` degrades to a flat
        # NIXOS_GIT_TIMEOUT_S per call, so dcg's 5s plus a few 4s git calls
        # can outrun the 10s harness timeout — and a harness timeout DISCARDS
        # this hook's output and renders no decision, which is fail-open.
        # An unarmed deadline is therefore a guard that cannot promise to
        # reach a verdict in time, and this file's answer to that is to deny.
        # Same precedent as _TIMEOUTS_SANE: loud beats inert.
        _fail_closed(
            "the guard could not arm its own deadline "
            f"({type(exc).__name__}: {exc}), so it cannot guarantee a verdict "
            f"before the {HARNESS_HOOK_TIMEOUT_S}s harness timeout discards "
            "this hook's output and renders no decision at all"
        )


def _disarm_watchdog() -> None:
    """Stop the timer before emitting a decision.

    Without this, the watchdog could fire mid-write and interleave a second
    JSON object into stdout, corrupting the decision we already reached.
    """
    # arftl-allow: swallowed-exception — cancelling the timer can only fail on
    # a platform where _arm_watchdog's setitimer() also failed, and that path
    # now denies rather than returning, so reaching here means a timer really
    # was armed and really can be cancelled. Nothing to recover, and no
    # permissive outcome either way: the verdict is already decided, and a
    # watchdog that fired anyway calls _fail_closed, which denies.
    try:
        signal.setitimer(signal.ITIMER_REAL, 0)
    # arftl-allow: swallowed-exception — reasoning directly above.
    except (ValueError, OSError, AttributeError): pass


# ----------------------------------------------------------------------
# Layer 1: dcg
# ----------------------------------------------------------------------
def _resolve_dcg() -> str:
    """Path dcg should live at. Existence is checked by the caller.

    Order: explicit DCG_PATH, then PATH, then the historical install
    location. Returning a path that does not exist is a valid outcome —
    that is a fail-closed deny, not something to paper over.
    """
    explicit = os.environ.get("DCG_PATH")
    if explicit:
        return explicit
    found = shutil.which("dcg")
    if found:
        return found
    return LEGACY_DCG_PATH


# Outcome lines from `dcg config`. Measured shape (dcg 0.12.5):
#
#   Config file outcomes (lowest → highest priority):
#     - system: /etc/dcg/config.toml [missing; full]
#     - user: /home/jonathan/.config/dcg/config.toml [loaded; full]
#     - automatic project: /repo/.dcg.toml [missing; enforcement-only]
#
# and, when the file will not parse:
#
#     - user: /home/…/config.toml [invalid; full] — Invalid TOML: TOML parse
#       error at line 71, column 3
_DCG_CONFIG_OUTCOME = re.compile(
    r"^\s*-\s*(system|user|automatic project)\s*:\s*(.*?)\s*"
    r"\[\s*([^\];]+?)\s*;[^\]]*\]\s*(.*)$"
)

# Verified at most once per hook process. There is no cross-invocation
# cache: a stale "config was fine last time" is the same class of stale
# belief this whole file exists to stop.
_DCG_CONFIG_VERIFIED = False


def _check_dcg_config(path: str) -> None:
    """Verify dcg's user config actually LOADED. Never returns on a fault.

    dcg being present and answering does NOT mean dcg is guarding.
    Measured against dcg 0.12.5, three states:

      * config valid      — `curl -X DELETE …` DENIED, user outcome
                            `[loaded; full]`.
      * config ABSENT     — `curl -X DELETE …` PERMITTED. Empty stdout,
                            exit 0, and zero stderr. `rm -rf /` is still
                            denied by the built-in `core.filesystem` pack,
                            so the guard looks completely alive.
      * config MALFORMED  — same fallback. dcg does warn, but on STDERR,
                            which Claude Code discards, so it reaches
                            nobody.

    `curl -X DELETE` is matched by NO built-in pack — only by the
    `[overrides] block` list in the operator's config. So in two of those
    three states the entire user override layer is silently gone while
    every query still gets a confident answer. That is the four-month dcg
    outage rebuilt one storey up, and it is why "dcg ran and returned no
    deny" is not sufficient to allow.

    `dcg config` exits 0 in all three states, so — exactly as on the hook
    path — stdout is the only signal.
    """
    global _DCG_CONFIG_VERIFIED
    if _DCG_CONFIG_VERIFIED:
        return

    # Own clamp rather than a second watchdog: the whole-hook deadline is
    # the only clock, and this call draws from what is left of it. Measured
    # at ~14ms against the real binary, so the cap is for faults only.
    budget = min(float(DCG_TIMEOUT_S), _remaining_budget())
    if budget <= 0:
        _fail_closed(
            "the guard ran out of its whole-hook budget before dcg could report "
            "whether its config had loaded"
        )
    try:
        proc = subprocess.run(
            [path, "config"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=budget,
        )
    except subprocess.TimeoutExpired:
        _fail_closed(
            f"`dcg config` did not answer within {budget:.1f}s, so the guard could "
            "not confirm dcg is running with the operator's block rules"
        )
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        _fail_closed(
            f"`dcg config` could not be invoked: {type(exc).__name__}: {exc}"
        )

    outcomes = []
    for line in (proc.stdout or "").splitlines():
        m = _DCG_CONFIG_OUTCOME.match(line)
        if m:
            outcomes.append(
                (m.group(1), m.group(2).strip(), m.group(3).strip().lower(), m.group(4).strip())
            )

    if not outcomes:
        _fail_closed(
            "`dcg config` did not report any config-file outcomes, so the guard "
            "cannot tell whether dcg is running with the operator's block rules "
            "or with built-in packs alone. Either the binary at "
            f"{path!r} is not dcg, or its `config` output format changed"
            + (f" (stdout: {(proc.stdout or '').strip()[:160]!r})" if proc.stdout else
               " (it printed nothing on stdout)")
        )

    # A file that will not parse is a typo the operator must fix, and it is
    # reported separately from an absent one so they know which.
    for scope, cfg_path, state, detail in outcomes:
        if state == "invalid":
            _fail_closed(
                f"dcg's {scope} config at {cfg_path!r} FAILED TO PARSE, so dcg fell "
                "back to its built-in packs and every rule in the operator's "
                "[overrides] block list is inert. dcg does warn about this, but "
                "only on stderr, which Claude Code discards. "
                f"Fix the file: {detail[:200] or 'see `dcg config`'}"
            )

    user = [o for o in outcomes if o[0] == "user"]
    if not user:
        _fail_closed(
            "`dcg config` reported config outcomes but none of them for the USER "
            "config, so the guard cannot confirm the operator's block rules loaded"
        )
    _scope, cfg_path, state, _detail = user[0]
    if state != "loaded":
        _fail_closed(
            f"dcg's user config at {cfg_path!r} is {state.upper()}, so dcg is running "
            "on built-in packs alone. It still answers every query and still denies "
            "`rm -rf /`, which is why this cannot be noticed from the outside — but "
            "every rule in the operator's [overrides] block list is gone, including "
            "the destructive-HTTP ones (`curl -X DELETE …` is matched by no built-in "
            "pack). The file is generated from the repo-tracked ~/.claude/dcg.toml"
        )

    _DCG_CONFIG_VERIFIED = True


def _check_dcg(command: str) -> str | None:
    """Ask dcg whether to block. Returns a deny reason, or None to allow.

    Never returns None for a failure: every failure mode routes through
    ``_fail_closed`` and exits. This is the exact inversion of safe-bash's
    ``_check_denied_by_dcg``, which returned None (allow) for all of them.

    NOTE the invocation form. Bare ``dcg`` is deliberate and must not be
    changed to ``dcg hook`` in pursuit of a non-zero exit on deny: measured,
    ``dcg hook`` answers the same stdin with batch JSONL
    (``{"index":0,"decision":"deny",…}``) instead of a PreToolUse
    ``permissionDecision`` document, and prints on allows too. Its own
    ``--help`` claims the two are identical; they are not.
    """
    path = _resolve_dcg()
    if not os.path.exists(path):
        _fail_closed(f"dcg binary not found at {path!r}")
    if not os.access(path, os.X_OK):
        _fail_closed(f"dcg at {path!r} exists but is not executable")


    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    try:
        proc = subprocess.run(
            [path],
            input=payload,
            capture_output=True,
            text=True,
            timeout=DCG_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        _fail_closed(f"dcg did not answer within {DCG_TIMEOUT_S}s")
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        _fail_closed(f"dcg could not be invoked: {type(exc).__name__}: {exc}")

    # THE VERDICT IS THE STDOUT JSON. NEVER THE EXIT STATUS.
    #
    # Measured against the packaged binary (dcg 0.12.5, rev d921fe84, the
    # rev nixos-config pins):
    #   * stdout carries exactly ONE line — the JSON decision — and only
    #     when dcg denies. An allow produces no stdout at all.
    #   * the decorated BLOCKED banner goes to stderr, not stdout.
    #   * hook mode exits 0 for a deny AND for an allow. Upstream's
    #     `EXIT_DENIED = 1` (src/exit_codes.rs) governs the CLI
    #     subcommands, not the stdin hook path. Anything inferring the
    #     verdict from the exit code reads every deny as an allow.
    #
    # safe-bash's comment claimed the banner preceded the JSON on stdout,
    # and it scanned in REVERSE so the last `{` line won. This port scanned
    # FORWARD, so the first won. Both were guesses about a shape neither
    # had measured, and they fail in opposite directions the moment dcg
    # emits more than one JSON line. Every `{` line is now inspected and a
    # deny ANYWHERE wins: direction-independent, and fail-closed whichever
    # way a future dcg orders its output.
    deny_reason: str | None = None
    saw_allow = False
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            _fail_closed(
                "dcg emitted a line that opens as JSON but does not parse: "
                f"{line[:120]!r}"
            )
        hook_out = (data.get("hookSpecificOutput") or {}) if isinstance(data, dict) else {}
        verdict = hook_out.get("permissionDecision")
        if verdict == "deny":
            if deny_reason is None:
                rule = hook_out.get("ruleId") or "unknown"
                why = hook_out.get("permissionDecisionReason") or ""
                deny_reason = (
                    f"bash-guard: dcg blocked this command (rule: {rule}).\n"
                    f"{why}\n"
                    "This is dcg working as intended, not a broken guard. To run it "
                    "anyway, run it yourself in the prompt with `! <command>`."
                ).strip()
            continue
        if verdict == "allow":
            saw_allow = True
            continue
        _fail_closed(
            "dcg rendered a permissionDecision this guard does not understand: "
            f"{verdict!r}"
        )

    if deny_reason is not None:
        return deny_reason

    # No deny rendered. Exit 0 is dcg's allow — for a silent run and for an
    # explicit `"allow"` alike. Any other status means dcg fell over, and a
    # rendered allow does not excuse that: the allow speaks for dcg's rules,
    # not for dcg having survived the call.
    if proc.returncode != 0:
        _fail_closed(
            f"dcg exited {proc.returncode} without rendering a deny"
            + (" (it did render an allow, which does not vouch for the exit status)"
               if saw_allow else "")
            + (f" (stderr: {(proc.stderr or '').strip()[:160]!r})" if proc.stderr else "")
        )

    # dcg declined to block. That answer is only worth acting on if dcg was
    # loaded with the rules that make it worth anything — see
    # `_check_dcg_config`. Deliberately checked HERE and not earlier: a dcg
    # DENY is trustworthy whatever its config state, because the built-in
    # packs are a strict subset of the loaded rule set and a deny from a
    # subset is still a valid deny. It is only the ABSENCE of a deny that
    # depends on the full set having been loaded.
    _check_dcg_config(path)
    return None


# ----------------------------------------------------------------------
# Layer 2: find ... -delete with no filter predicate
# (ported verbatim from mcps/safe-bash/server.py, incident text included)
# ----------------------------------------------------------------------
def _split_operators(cmd: str) -> list[tuple[str, str]]:
    """`(segment, the operator that terminated it)`, quote-aware.

    Operators are `&&`, `||`, `;`, `|`, `&` and newline; the last segment
    carries `""`. The operator matters to layer 3, which has to tell a pipe
    from an `&&`: `curl … && bash x.sh` is not the shape being caught, while
    a pipe feeds a shell untrusted bytes. [arftl-allow: curl-pipe-shell — prose]
    `curl … | bash` is the one layer 3 denies.
    """
    parts: list[tuple[str, str]] = []
    current: list[str] = []
    i = 0
    s = cmd
    in_single = False
    in_double = False
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and not in_single:
            current.append(s[i : i + 2])
            i += 2
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            i += 1
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            i += 1
            continue
        if not in_single and not in_double:
            if s[i : i + 2] in ("&&", "||"):
                parts.append(("".join(current).strip(), s[i : i + 2]))
                current = []
                i += 2
                continue
            if ch in (";", "|", "&", "\n"):
                parts.append(("".join(current).strip(), ch))
                current = []
                i += 1
                continue
        current.append(ch)
        i += 1
    if current:
        parts.append(("".join(current).strip(), ""))
    return parts


def _pipeline_subcommands(cmd: str) -> list[str]:
    """Split a full command into operator-separated subcommands.

    Split on `&&`, `||`, `;`, `|`, `&` and newlines, respecting quotes, so
    each `find` invocation in a chain is evaluated independently.
    """
    return [seg for seg, _op in _split_operators(cmd) if seg]


# The danger isn't `-delete` per se — it's `-delete` without any predicate
# that constrains WHAT gets deleted. The set below is the file-property
# predicate vocabulary (names, types, ages, sizes, permissions, ownership).
# `-maxdepth` / `-mindepth` / `-depth` are NOT in the set: they only bound
# traversal, not which files match.
_FIND_FILTER_PREDICATES = frozenset({
    "-name", "-iname", "-path", "-ipath", "-regex", "-iregex", "-lname", "-ilname",
    "-type", "-xtype",
    "-mtime", "-ctime", "-atime", "-mmin", "-cmin", "-amin", "-newer", "-anewer", "-cnewer",
    "-size", "-empty",
    "-perm", "-readable", "-writable", "-executable",
    "-user", "-uid", "-group", "-gid", "-nouser", "-nogroup",
    "-links", "-inum", "-samefile",
})

_FIND_DANGEROUS_HINT = (
    "find ... -delete denied: no filter predicate before -delete, so this "
    "would delete everything under the given path(s).\n"
    "Real incident this guards against: deleted active plugin runtime data "
    "thinking the dir held only old worker logs.\n"
    "Safer alternatives:\n"
    "  1. List first, no -delete:  find PATH -maxdepth 1\n"
    "     Eyeball the output before deleting anything.\n"
    "  2. Filter by property:      find PATH -name 'PATTERN' -delete\n"
    "                              find PATH -type f -mtime +30 -delete\n"
    "  3. Per-file:                find PATH -maxdepth 1 -name 'specific.log' -delete\n"
    "If you genuinely intend to wipe a whole tree, ask the user first."
)

_FIND_UNPARSEABLE_HINT = (
    "find ... -delete denied: this command contains `-delete`, but the shell "
    "string cannot be tokenised (unbalanced quote, or similar), so the guard "
    "cannot tell whether any filter predicate constrains what would be "
    "deleted.\n"
    "An unanalysable delete is refused rather than waved through. safe-bash "
    "skipped it instead, which made `find PATH -delete \"unbalanced` a "
    "measured bypass of this layer — dcg has already had its turn by then, "
    "and no other layer looks at find.\n"
    "Balance the quotes so the command parses, or run it yourself in the "
    "prompt with `! <command>`."
)

# Cap on how many nested strings one command may expand into. A shell
# command deep enough to exhaust this is not one the guard can reason
# about anyway, and the whole-hook watchdog is the backstop.
_MAX_FIND_FRAGMENTS = 64


def _is_find_token(token: str) -> bool:
    """True for `find`, `/usr/bin/find`, `./find` — any path ending in find.

    safe-bash compared against the literal string `find`, so
    `/usr/bin/find PATH -delete` walked straight through. Its own
    `_match_untrusted_ingress` already basenamed the head token for exactly
    this reason; the omission here was an inconsistency, not a decision.
    """
    return os.path.basename(token) == "find"


def _command_substitutions(s: str) -> list[str]:
    """Bodies of `$( ... )` and backtick substitutions.

    `shlex` does not look inside either form, so `echo $(find PATH -delete)`
    tokenises to `$(find`, `PATH`, `-delete)` — no token equals `find` and
    none equals `-delete`. Both were measured bypasses.
    """
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "$" and i + 1 < n and s[i + 1] == "(":
            depth = 1
            j = i + 2
            while j < n and depth:
                if s[j] == "(":
                    depth += 1
                elif s[j] == ")":
                    depth -= 1
                j += 1
            # Unterminated substitution: take the rest, so a truncated or
            # malformed string still gets looked at rather than skipped.
            body = s[i + 2 : j - 1] if depth == 0 else s[i + 2 :]
            if body.strip():
                out.append(body)
            i += 2  # keep scanning inside, so nested $( ) are found too
            continue
        if s[i] == "`":
            j = s.find("`", i + 1)
            body = s[i + 1 : j] if j != -1 else s[i + 1 :]
            if body.strip():
                out.append(body)
            i = j + 1 if j != -1 else n
            continue
        i += 1
    return out


def _find_fragments(command: str) -> list[str]:
    """`command`, plus every nested string a shell would also execute.

    Covers `sh -c "..."`, `bash -c '...'`, `eval "..."`, `timeout N bash -c
    "..."`, `xargs sh -c "..."` and anything else shaped like them, without
    enumerating wrapper names: after shlex, all of them surface the inner
    command as ONE token containing whitespace. Matching the shape rather
    than a name list means a wrapper nobody thought of is covered too.
    """
    frags: list[str] = []
    seen: set[str] = set()
    queue: list[str] = [command]
    while queue and len(frags) < _MAX_FIND_FRAGMENTS:
        s = queue.pop(0)
        if s in seen:
            continue
        seen.add(s)
        frags.append(s)
        queue.extend(_command_substitutions(s))
        for sub in _pipeline_subcommands(s):
            try:
                tokens = shlex.split(sub)
            except ValueError:
                continue  # the caller's strict pass turns this into a deny
            for t in tokens:
                if "-delete" in t and (" " in t or "\t" in t or "\n" in t):
                    queue.append(t)
    return frags


def _check_dangerous_find(command: str) -> str | None:
    """Detect `find ... -delete` invocations with no filter predicate.

    Allows: -delete combined with -name/-type/-mtime/-size/etc., including
    inside a nested shell — the inner command is analysed on its own terms,
    so a properly filtered delete still runs however it is wrapped.
    Denies: bare `find PATH -delete`, `find PATH -maxdepth 1 -delete`, the
    same through any wrapper or substitution, and any `-delete`-bearing
    subcommand that will not tokenise at all.

    The tokenisation failure is a DENY, not a skip. safe-bash `continue`d
    past it, so an unbalanced quote disabled the layer silently.
    """
    if "-delete" not in command:
        return None
    for frag in _find_fragments(command):
        # `strict` marks the entries whose failure to tokenise is a verdict.
        # The whole-string entry is tolerant: when one subcommand is
        # unbalanced the whole string necessarily is too, and denying on it
        # would reject `find PATH -name x -delete ; echo "oops` — where the
        # delete is fully constrained and the unbalanced part carries no
        # -delete at all. Over-broad denial is its own defect.
        candidates = [(s, True) for s in _pipeline_subcommands(frag)]
        candidates.append((frag.strip(), False))
        for sub, strict in candidates:
            if "-delete" not in sub:
                continue
            try:
                tokens = shlex.split(sub)
            except ValueError:
                if strict:
                    return "bash-guard: " + _FIND_UNPARSEABLE_HINT
                continue
            if "-delete" not in tokens or not any(_is_find_token(t) for t in tokens):
                continue
            i = 0
            while i < len(tokens):
                if not _is_find_token(tokens[i]):
                    i += 1
                    continue
                j = i + 1
                delete_pos = -1
                has_filter = False
                while j < len(tokens):
                    t = tokens[j]
                    if t == "-delete":
                        delete_pos = j
                        break
                    if t in _FIND_FILTER_PREDICATES:
                        has_filter = True
                    j += 1
                if delete_pos != -1 and not has_filter:
                    return "bash-guard: " + _FIND_DANGEROUS_HINT
                i = j + 1 if delete_pos != -1 else i + 1
    return None


# ----------------------------------------------------------------------
# Layer 3: a network fetch piped into a shell interpreter
#
# This layer is the intent of six ported deny rules — `Bash(curl *|bash*)`,  (arftl-allow: curl-pipe-shell) - prose only; layer 3 matches tokenised pipeline stages, not this literal
# `Bash(curl *| bash*)`, `Bash(curl *|sh*)`, `Bash(curl *| sh*)`,  (arftl-allow: curl-pipe-shell) - prose only; layer 3 matches tokenised pipeline stages, not this literal
# `Bash(wget *|bash*)`, `Bash(wget *| bash*)` — none of which could ever fire.  (arftl-allow: curl-pipe-shell) - prose only; layer 3 matches tokenised pipeline stages, not this literal
# MEASURED against the shipped bundle, not reasoned: Claude Code consults the
# deny list twice, and the pass that is offered the WHOLE command string
# returns false for every wildcard rule (`case"wildcard":if(r==="exact")
# return!1` inside `Fjs`). Wildcard rules therefore only ever see individual
# subcommands, and no subcommand of `curl … | bash` contains the pipe. The six  (arftl-allow: curl-pipe-shell) - prose only; layer 3 matches tokenised pipeline stages, not this literal
# rules read as protection and stopped nothing.
# `tests/test_deny_globs.py` lifts that matcher out of the bundle and pins it.
#
# D3 re-expressed the family at the SINK (`Bash(bash)`, `Bash(sh)`,
# `Bash(bash -s*)`, `Bash(sh -s*)`), which the matcher CAN enforce, and those
# rules stay: they stop the three canonical spellings without this hook. What
# they cannot reach — measured against the same lifted matcher, all ALLOWED by
# the complete deny list — is `| bash -`, `| sh -`, `| bash -e`,
# `| bash /dev/stdin`, `| zsh`, `| dash`, `| ksh`, `| tee x.sh | bash -`.
# Chasing those with more rules is whack-a-mole, and the one rule that would
# cover them (`Bash(bash -*)`) refuses `bash -n <script>`, which this repo's
# own smoke suite runs. A `Bash(...)` rule matches a STRING; the shape here is
# a relation between two stages of a pipeline, and only something holding the
# whole command string can see it. The hook holds it.
#
# Deliberately NOT done, and why:
#   * No descent into arbitrary multi-word tokens. The `-delete` layer does
#     that, because a token containing `-delete` is executable-shaped wherever
#     it sits. Here it would deny `git commit -m "never run curl url | bash"`,  (arftl-allow: curl-pipe-shell) - prose only; layer 3 matches tokenised pipeline stages, not this literal
#     which is the over-broad-literal defect (`*aut*review*`, retired in D3)
#     wearing a new costume. `$( )` and backtick bodies ARE descended into:
#     those are executed by definition.
#   * Shell sinks only. `curl … | python3 -` is the same threat class, but the
#     rules being replaced named shells, and widening the policy is the
#     operator's call, not this port's.
#   * `curl … > x.sh && bash x.sh` is NOT caught, by anyone. Catching it means
#     refusing `bash <script>`, and `bash tests/smoke.sh` has to keep working.
# ----------------------------------------------------------------------
#
# The fetch vocabulary is the same set `hooks/bash-output-guard.py` treats as
# untrusted external content (`untrusted_ingress_single` in
# hooks/bash-guard-config.json), so "output the model must not trust" and
# "output that must not reach a shell" cannot drift apart. It is duplicated
# here rather than read from that file on purpose: making the PreToolUse guard
# depend on a config file would add a whole new way for it to fail to reach a
# verdict, and every one of those has to become a deny. The test suite asserts
# the two sets are equal instead.
_PIPE_SOURCE_COMMANDS = frozenset({"curl", "wget", "npx"})

# The six rules named `bash` and `sh`. The rest are the same sink under a
# different spelling — the omission was an inconsistency, not a decision, the
# same one that let `/usr/bin/find … -delete` past the layer above.
#
# `eval` is here for SET PARITY with the output guard's SHELL_INTERPRETERS, not
# because it is an exploit: `| eval` with no argument does not read stdin, so
# nothing runs. It is listed so the two vocabularies can be compared as equal
# sets — see the parity assertion in tests/test_bash_guard_pre.py, which is
# what stops them drifting apart again.
_PIPE_SHELL_SINKS = frozenset(
    {"sh", "bash", "dash", "zsh", "ksh", "ash", "mksh", "eval"}
)

# Words that stand in front of the program actually being run. Their own flags
# are skipped with them, so `sudo -E bash` resolves to `bash`.
#
# `busybox` is a MULTIPLEXER, so it belongs here and not in the sink set: it
# runs whatever applet argv[1] names, and only some of those are shells.
# Treating it as a wrapper resolves `busybox sh` and `busybox ash` to the real
# sink while leaving `busybox cat` alone; treating it as a sink would deny
# every applet.
#
# MEASURED, and worse than the set-parity gap that pointed at it:
# `curl … | busybox ash` walked past EVERY layer, dcg included — empty stdout,
# exit 0, nothing objected. `ash` with no script operand executes what it reads
# on stdin, so that is a live fetch-to-execution path. dcg's own rule happens
# to catch `busybox sh` and not `busybox ash`, which made this layer's omission
# load-bearing and unbacked for the spelling that mattered.
_PIPE_WRAPPERS = frozenset(
    {"sudo", "doas", "env", "command", "exec", "nohup", "time", "nice", "ionice",
     "setsid", "stdbuf", "busybox"}
)

_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# A stage whose head cannot be read at all. Distinct from "" (empty stage), so
# the caller can deny on it rather than treating it as "no shell here".
_UNREADABLE_HEAD = "\x00unreadable\x00"

_PIPE_TO_SHELL_HINT = (
    "{src} ... | {sink} denied: this pipes bytes fetched over the network "
    "straight into a shell interpreter, which executes whatever the far end "
    "sent. Nothing between the fetch and the execution ever reads it — not the "
    "agent, not the operator.\n"
    "This is the intent of six deny rules (`Bash(curl *|bash*)` and its "  # arftl-allow: curl-pipe-shell - prose only; layer 3 matches tokenised pipeline stages, not this literal
    "spellings) that were MEASURED to stop nothing: Claude Code offers the "
    "whole command string only to exact- and prefix-type rules, so a wildcard "
    "pattern spanning a pipe is never matched against it.\n"
    "Instead: fetch to a file, READ the file, then run it deliberately —\n"
    "  curl -fsSL URL -o /tmp/install.sh\n"
    "  # read /tmp/install.sh, then:\n"
    "  bash /tmp/install.sh\n"
    "To run it anyway, run it yourself in the prompt with `! <command>`."
)

_PIPE_UNPARSEABLE_HINT = (
    "denied: this pipeline starts with a network fetch ({src}) and a later "
    "stage cannot be tokenised (unbalanced quote, or similar), so the guard "
    "cannot tell whether the fetched bytes are being piped into a shell.\n"
    "An unanalysable pipe out of a fetch is refused rather than waved through "
    "— \"the guard could not look\" must not render the same as \"the guard "
    "found nothing to object to\".\n"
    "Balance the quotes so the command parses, or run it yourself in the "
    "prompt with `! <command>`."
)


def _pipe_stages(command: str) -> list[list[str]]:
    """Group top-level segments into pipelines.

    `a | b && c | d` -> `[["a", "b"], ["c", "d"]]`. Only `|` continues a
    pipeline; `&&`, `||`, `;`, `&` and newline end one. `||` is consumed as a
    single operator by `_split_operators`, so it is never read as a pipe.
    """
    pipelines: list[list[str]] = []
    current: list[str] = []
    for seg, op in _split_operators(command):
        current.append(seg)
        if op != "|":
            pipelines.append(current)
            current = []
    if current:
        pipelines.append(current)
    return [[s for s in p if s] for p in pipelines if any(s for s in p)]


def _stage_head(stage: str) -> str:
    """Basename of the program a pipeline stage runs.

    `""` for an empty stage, `_UNREADABLE_HEAD` when the stage will not
    tokenise. Subshell parens are stripped (`(curl x` -> `curl x`), leading
    `VAR=value` assignments and wrapper words are skipped, and the result is
    basenamed so `/bin/bash` reads as `bash`.
    """
    s = stage.strip()
    while s.startswith("("):
        s = s[1:].lstrip()
    while s.endswith(")"):
        s = s[:-1].rstrip()
    if not s:
        return ""
    try:
        tokens = shlex.split(s)
    except ValueError:
        return _UNREADABLE_HEAD
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if _ENV_ASSIGNMENT.match(token):
            i += 1
            continue
        if os.path.basename(token) in _PIPE_WRAPPERS:
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                i += 1
            continue
        return os.path.basename(token)
    return ""


def _pipe_fragments(command: str) -> list[str]:
    """`command`, plus every `$( )` / backtick body nested inside it.

    A substitution body is executed by definition, so the shape below is the
    same command wearing a hat. [arftl-allow: curl-pipe-shell — prose]
    `echo $(curl … | bash)`. Unlike the `-delete` layer this does
    NOT descend into multi-word tokens — see the header: that would refuse
    ordinary commit subjects quoting the shape.

    Shares the fragment cap with that layer for the same reason: a command
    nested deeper than this is not one the guard can reason about, and the
    whole-hook watchdog is the backstop.
    """
    frags: list[str] = []
    seen: set[str] = set()
    queue: list[str] = [command]
    while queue and len(frags) < _MAX_FIND_FRAGMENTS:
        s = queue.pop(0)
        if s in seen:
            continue
        seen.add(s)
        frags.append(s)
        queue.extend(_command_substitutions(s))
    return frags


def _check_pipe_to_shell(command: str) -> str | None:
    """Detect a network fetch feeding a shell interpreter in one pipeline.

    The Denies/Allows list below quotes the shapes this function decides
    about. [arftl-allow: curl-pipe-shell — prose, not commands.]
    Denies: `curl … | bash` and every spelling of it, including through a
    later stage (`| tee x | bash`), a wrapper (`| sudo bash`), an absolute
    path (`| /bin/bash`), a subshell, or a command substitution.
    Allows: a fetch that feeds anything else (`| jq`, `| grep bash`), a shell
    fed from a local file (`cat x.sh | bash` — nothing untrusted arrived), and
    the shape quoted inside a string, where no shell ever sees the pipe.
    """
    for frag in _pipe_fragments(command):
        for stages in _pipe_stages(frag):
            if len(stages) < 2:
                continue
            heads = [_stage_head(s) for s in stages]
            for i, head in enumerate(heads):
                if head not in _PIPE_SOURCE_COMMANDS:
                    continue
                for downstream in heads[i + 1 :]:
                    if downstream in _PIPE_SHELL_SINKS:
                        return "bash-guard: " + _PIPE_TO_SHELL_HINT.format(
                            src=head, sink=downstream
                        )
                    if downstream == _UNREADABLE_HEAD:
                        return "bash-guard: " + _PIPE_UNPARSEABLE_HINT.format(src=head)
    return None


# ----------------------------------------------------------------------
# Layer 4: the nixos-config push gate
# (ported from `_check_nixos_push` in mcps/safe-bash/server.py, which chunk
# C deletes; the incident texts below are the behaviour, not decoration)
# ----------------------------------------------------------------------
#
# Two PRs deployed broken changes to dellan despite the SessionStart hook's
# HARD RULE and the nixos-config-dev / nixos-agent-testing skills:
#
#   PR #61 (research-agent → microvm): the agent ran `nix run .#feature-vm`
#     and SSH-smoked it, but stubbed `modules/nixos/feature-vm.nix` to make
#     boot succeed (missing virtiofsd share-source dirs). The smoke ran
#     against a mutated artefact, not the prod path. Inner microvm
#     `networking.useDHCP` defaulted false → no resolver → sshd never
#     started → research-agent unreachable post-deploy.
#
#   PR #57 (kitty ctrl+shift+c strip newline): the agent ran only the
#     automated gate (`nix build .#checks.x86_64-linux.dellan-vm`) whose
#     assertion was grep-the-rendered-config — never launched kitty, never
#     pressed the key. `pass_selection_to_program sh -c '… kitten clipboard'`
#     fails silently at runtime because the child has no controlling tty for
#     OSC52.
#
# The shared failure mode is the agent equating "build green" with "feature
# works", and the SessionStart prose (which both sessions had in context)
# did not gate behaviour. This layer is the enforcement the prose isn't.
#
# Relationship to hooks/git-push-merge-check.sh — they are NOT redundant and
# neither subsumes the other. Same event, different jobs:
#   * this layer   — hard DENY, nixos-config remotes only, enforces that the
#                    HEAD commit carries a filled `Pre-push checklist:` and
#                    that its claims survive a mechanical cross-check
#                    against the real diff.
#   * merge-check  — advisory only (always exit 0), EVERY repo, and its
#                    question is "is this branch behind the default branch,
#                    so the PR would land needing an Update-branch click".
# The one place they touch is `Rebased on origin/main`, and even there the
# gate only checks it when the checklist CLAIMS `yes`, whereas merge-check
# nudges unconditionally and in repos this layer never looks at. Deleting
# either one loses coverage the other never had.
#
# What is deliberately preserved from safe-bash, not tightened:
#   * `Override: [skip-vm-gate] <reason>` still bypasses. It is not an
#     agent-reachable env var — it goes in the commit message and stays
#     readable in `git log` after the merge, which is the point.
#   * Pushing `main` itself is not gated (branch-protected upstream).
#
# What is deliberately INVERTED from safe-bash: every way the gate could
# fail to reach a verdict used to return "allow" — a failed `git diff` left
# `diff_touches_fv` False and quietly disabled the PR #61 cross-check while
# the gate still LOOKED present. That is the same silent-disappearance
# failure as the missing dcg binary. All of those now deny.

# There is deliberately NO worktree-root path constant in this file, and
# `tests/test_bash_guard_pre.py` asserts that one never comes back.
# safe-bash kept one naming the canonical `~/Repos/nixos-config-worktrees`
# location long after detection had moved to remote-URL matching, and
# nothing read it. A live-looking constant naming a path the gate never
# consults is exactly what a future reader trusts.
#
# The gate detects a "nixos-config repo" by remote URL, not by filesystem
# path — so the same gate fires whether the worktree lives at the canonical
# `~/Repos/nixos-config-worktrees/<slug>/` location or anywhere else
# (scratch clone in `/tmp`, secondary worktree under `~/projects`, etc.).
# Patterns match the tail `[:/]owner/repo(.git)?` of a remote URL.
_NIXOS_REPO_REMOTE_PATTERNS: list["re.Pattern[str]"] = [
    re.compile(r"[:/]jonathanmoregard/nixos-config(?:\.git)?/?$"),
]

# Heuristic "this diff has branching / runtime / multistep behaviour" markers.
# Order matters only for the rejection-message hit list (first-match labels).
_NIXOS_RISKY_MARKERS = re.compile(
    r"\bmkIf\b"
    r"|\boptionals\b"
    r"|\bExecStart"
    r"|writeShellApplication"
    r"|writeShellScriptBin"
    r"|pass_selection_to_program"
    r"|\bmicrovm\b"
    r"|\bvirtiofsd\b"
    r"|networking\.useDHCP"
    r"|activationScripts?"
    r"|systemd\.(?:services|timers|user\.services)\."
    r"|\bsh\s+-c\s+"
    r"|\bbash\s+-c\s+"
)

_NIXOS_CHECKLIST_HEADER = re.compile(r"^Pre-push checklist:\s*$", re.MULTILINE)
# Field lines are top-level only (anchored `^-\s`, NOT `^\s*-\s`). Indented
# sub-bullets inside a multi-line `Behavioural evidence` value must not
# overwrite the parent field map.
_NIXOS_CHECKLIST_FIELD = re.compile(r"^-\s*([^:\n]+?)\s*:\s*(.*)$")
_NIXOS_OVERRIDE = re.compile(r"^Override:\s*\[skip-vm-gate\]\s*(\S.+)$", re.MULTILINE)
# `<placeholder>` template hints (`<list, or 'none'>`, `<cmd + output>`) left
# unfilled by the agent are treated as empty. The angle-brackets are the
# template's hint marker; real values never start with `<` AND end with `>`.
_NIXOS_TEMPLATE_PLACEHOLDER = re.compile(r"^<.*>$")
_NIXOS_FEATURE_VM_FILE_IN_DIFF = re.compile(
    r"^diff --git .*modules/nixos/feature-vm\.nix\b", re.MULTILINE
)

_NIXOS_GATE_HELP = (
    "nixos-config push gate refused this push (remote points at "
    "jonathanmoregard/nixos-config).\n\n"
    "HEAD commit message must contain a 'Pre-push checklist:' block.\n"
    "Template (copy verbatim, then fill values):\n\n"
    "  Pre-push checklist:\n"
    "  - Type: pure-data           # OR 'risky' for branching/multistep/runtime\n"
    "  - Rebased on origin/main: yes\n"
    "  - Local gate: nix build .#checks.x86_64-linux.dellan-vm rc=0\n"
    "  - Interactive smoke (nixos-agent-testing): <yes — cmd + observed | N/A — reason>\n"
    "  - Advisor review (advice-refine-test-loop): <yes — rounds + verdict | N/A — reason>\n"
    "  - feature-vm.nix modified: no    # MUST match actual diff\n"
    "  - Risky markers in diff: <list, or 'none'>\n"
    "  - Behavioural evidence: <what you actually exercised — command + observed output>\n\n"
    "For 'Type: pure-data' (package add, version bump, comment), only Type +\n"
    "brief justification are required; diff is verified to have no risky markers.\n\n"
    "Bypass (audited): add 'Override: [skip-vm-gate] <reason>' to commit message.\n"
)


def _nixos_git(worktree: str, args: list[str]) -> tuple[int, str, str]:
    """Run one git command for the gate. Faults are FAIL-CLOSED.

    A non-zero exit from git itself is a legitimate ANSWER ("not a git
    repository", "unknown revision") and is handed back to the caller. A
    FAULT — git absent, git hung, the OS refusing to start it — means the
    gate obtained no answer at all, and it denies rather than guessing.
    safe-bash returned a `-1` sentinel here that every caller then treated
    as "no", which is how a broken git would have silently switched the
    whole gate off.

    Capture bytes and decode permissively: `text=True` raises
    UnicodeDecodeError on `git diff` output for commits that add or modify
    binary blobs (e.g. `.age` files), which would otherwise crash the gate
    before it can run.
    """
    budget = min(float(NIXOS_GIT_TIMEOUT_S), _remaining_budget())
    if budget <= 0:
        _fail_closed(
            "the nixos push gate ran out of the guard's whole-hook budget before "
            f"`git {' '.join(args)}` could answer"
        )
    try:
        proc = subprocess.run(
            ["git", "-C", worktree, *args],
            capture_output=True,
            text=False,
            timeout=budget,
        )
    except subprocess.TimeoutExpired:
        _fail_closed(
            f"`git {' '.join(args)}` in {worktree!r} did not answer within "
            f"{budget:.1f}s, so the nixos push gate could not evaluate this push"
        )
    except (OSError, ValueError) as exc:
        _fail_closed(
            f"`git {' '.join(args)}` could not be invoked for the nixos push gate: "
            f"{type(exc).__name__}: {exc}"
        )
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )


def _nixos_pipeline_subcommands(command: str) -> list[str]:
    """Pipeline split that also strips a leading '(' from subshell forms.

    `_pipeline_subcommands` correctly splits on `&&`/`;`/etc. but doesn't
    strip `(` from `(cd X && git push)`. We need the inside.
    """
    out: list[str] = []
    for sub in _pipeline_subcommands(command):
        s = sub.strip()
        while s.startswith("("):
            s = s[1:].lstrip()
        while s.endswith(")"):
            s = s[:-1].rstrip()
        if s:
            out.append(s)
    return out


def _nixos_extract_cd_target(command: str) -> str | None:
    """Last `cd <path>` target encountered in the pipeline chain.

    Returns an absolute path (with ~ expansion) or None. Quotes around the
    path are stripped. Only `cd` as the leading token of a subcommand counts
    — a `cd` mid-command (rare) is ignored.
    """
    last: str | None = None
    for sub in _nixos_pipeline_subcommands(command):
        m = re.match(r"^cd\s+(.+?)\s*$", sub)
        if not m:
            continue
        raw = m.group(1).strip()
        if (raw.startswith('"') and raw.endswith('"')) or (
            raw.startswith("'") and raw.endswith("'")
        ):
            raw = raw[1:-1]
        try:
            last = os.path.abspath(os.path.expanduser(raw))
        except (ValueError, OSError):
            continue
    return last


def _remote_is_nixos_config(url: str) -> bool:
    """True if a git remote URL points at the nixos-config repo.

    Tail-match against `_NIXOS_REPO_REMOTE_PATTERNS` so the same predicate
    works for `git@github.com:owner/repo.git`, `https://github.com/owner/repo`,
    `ssh://git@github.com/owner/repo`, etc.
    """
    url = (url or "").strip()
    if not url:
        return False
    return any(p.search(url) for p in _NIXOS_REPO_REMOTE_PATTERNS)


def _repo_has_nixos_remote(repo_root: str) -> bool:
    """True if the repo has any remote whose URL matches nixos-config.

    Checks ALL configured remotes (fetch + push entries), not just origin,
    so forks / mirrors with a different push target still gate correctly.
    """
    rc, out, _ = _nixos_git(repo_root, ["remote", "-v"])
    if rc != 0:
        return False
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and _remote_is_nixos_config(parts[1]):
            return True
    return False


def _candidate_repo_paths(command: str, cwd: str | None) -> tuple[list[str], list[str]]:
    """Candidate working-dir paths to probe for a git repo.

    Returns ``(paths, unresolved)``. ``unresolved`` names each candidate that
    could not be turned into a path at all.

    Order:
      1. Last `cd <path>` target in the command's pipeline chain
      2. `git -C <path>` flag
      3. The session cwd carried on the hook payload
      4. The hook process's own cwd

    safe-bash had only (1), (2) and the MCP server's `os.getcwd()`. A hook is
    a fresh process per call, so the session's directory arrives on the
    payload instead — dropping it would leave the gate unable to see a plain
    `git push` issued from inside the worktree.

    A candidate that will not resolve is REPORTED rather than dropped. Both
    resolutions below call `os.getcwd()` under the hood for a relative path,
    so a deleted working directory takes out candidates (2) and (4) together
    — and a shorter candidate list makes `_nixos_resolve_target_repo` more
    likely to answer "not a nixos repo", which `_check_nixos_push` reads as
    ALLOW. Silently dropping them let `git -C <relative> push` past the gate
    entirely. The caller decides what to do with that; this function no
    longer decides it by omission.
    """
    candidates: list[str] = []
    unresolved: list[str] = []
    cd_target = _nixos_extract_cd_target(command)
    if cd_target:
        candidates.append(cd_target)
    m = re.search(r"\bgit\s+(?:[^\n;|&]*?\s+)?-C\s+(\S+)", command)
    if m:
        raw = m.group(1).strip("\"'")
        try:
            candidates.append(os.path.abspath(os.path.expanduser(raw)))
        except (ValueError, OSError) as exc:
            unresolved.append(f"the `git -C {raw[:80]}` path ({type(exc).__name__})")
    if isinstance(cwd, str) and cwd.strip():
        candidates.append(cwd)
    try:
        candidates.append(os.getcwd())
    except OSError as exc:
        unresolved.append(f"the hook's own working directory ({type(exc).__name__})")
    # Dedupe while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for p in candidates:
        if p and p not in seen:
            seen.add(p)
            deduped.append(p)
    return deduped, unresolved


def _nixos_resolve_target_repo(command: str, cwd: str | None) -> str | None:
    """Working-tree top-level, if any candidate path is a nixos-config clone.

    Detection is by remote URL, NOT by filesystem path — so a worktree at
    `~/scratch/foo/`, `/tmp/clones/nixos-config/`, or anywhere outside the
    canonical `~/Repos/nixos-config-worktrees/` tree still gates as long as
    one of its configured remotes points at jonathanmoregard/nixos-config.

    Returning None means "no nixos-config repo here", and `_check_nixos_push`
    reads that as allow. So None is only permitted to mean it when every
    candidate was actually LOOKED at. If a candidate could not even be
    resolved to a path, "not a nixos push" and "could not tell" would render
    identically, and the gate would be switched off by a deleted directory.
    That is the same failure `_nixos_git` already refuses to make for a git
    command it could not run.
    """
    paths, unresolved = _candidate_repo_paths(command, cwd)
    for path in paths:
        rc, toplevel, _ = _nixos_git(path, ["rev-parse", "--show-toplevel"])
        if rc != 0:
            continue
        repo_root = toplevel.strip()
        if not repo_root:
            continue
        if _repo_has_nixos_remote(repo_root):
            return repo_root
    if unresolved:
        _fail_closed(
            "the nixos push gate found no nixos-config repo among the paths it "
            "could resolve, but it could not resolve " + "; ".join(unresolved)
            + " — so it cannot say this push is NOT a nixos-config push"
        )
    return None


def _nixos_command_pushes(command: str) -> bool:
    """True if a `git push` is present (excluding --dry-run)."""
    for sub in _nixos_pipeline_subcommands(command):
        if not re.search(r"\bgit\s+(?:-C\s+\S+\s+)?(?:[\w-]+\s+)*push\b", sub):
            continue
        if re.search(r"(?:^|\s)--dry-run(?:\s|$)", sub):
            continue
        return True
    return False


def _nixos_parse_checklist(commit_msg: str) -> dict[str, str] | None:
    """Extract the `Pre-push checklist:` field map. None if header missing.

    Block ends at the first non-field, non-blank, non-comment line after the
    header, or at end-of-message. Keys lowercased + stripped.
    """
    if not _NIXOS_CHECKLIST_HEADER.search(commit_msg):
        return None
    fields: dict[str, str] = {}
    in_block = False
    for line in commit_msg.splitlines():
        if _NIXOS_CHECKLIST_HEADER.match(line):
            in_block = True
            continue
        if not in_block:
            continue
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        m = _NIXOS_CHECKLIST_FIELD.match(line)
        if m:
            key = m.group(1).strip().lower()
            val = m.group(2).strip()
            # Drop inline `# comment` tails so the template's hint text doesn't
            # poison values when the agent left it untrimmed.
            val = re.sub(r"\s+#.*$", "", val).strip()
            fields[key] = val
            continue
        # First unrelated line ends the block.
        break
    return fields


def _check_nixos_push(command: str, cwd: str | None) -> str | None:
    """Gate `git push` of a nixos-config branch.

    Detection: any candidate path (from `cd` / `-C` / cwd) that resolves to a
    git working tree whose remotes include a URL matching nixos-config.
    Worktree location is NOT part of the check.

    Returns rejection text, or None to allow. Pure-data pushes with a minimal
    trailer are accepted. Risky pushes require the full trailer AND the
    mechanical cross-checks to agree with the diff. Anything the gate cannot
    evaluate routes through `_fail_closed`.
    """
    if not _nixos_command_pushes(command):
        return None
    worktree = _nixos_resolve_target_repo(command, cwd)
    if not worktree:
        return None
    rc, branch, _ = _nixos_git(worktree, ["branch", "--show-current"])
    if rc == 0 and branch.strip() == "main":
        return None  # Pushing main is already branch-protected upstream.

    rc, msg, err = _nixos_git(worktree, ["log", "-1", "--format=%B"])
    if rc != 0:
        # safe-bash allowed here ("no HEAD — let git itself fail"). It cannot
        # allow now: an unreadable HEAD message means the checklist was never
        # inspected, and "the gate ran and found nothing to object to" and
        # "the gate could not look" must not render identically.
        _fail_closed(
            f"the nixos push gate could not read the HEAD commit message of "
            f"{worktree!r}, so the Pre-push checklist could not be verified "
            f"(`git log -1` exited {rc}: {err.strip()[:160]!r})"
        )

    override = _NIXOS_OVERRIDE.search(msg)
    if override:
        try:
            sys.stderr.write(
                f"[bash-guard] nixos push gate: bypass via Override: [skip-vm-gate] "
                f"{override.group(1).strip()[:120]!r}\n"
            )
        except Exception:  # noqa: BLE001 — an audit line must never decide the push
            pass
        return None

    fields = _nixos_parse_checklist(msg)
    if fields is None:
        return _NIXOS_GATE_HELP

    # Diff against origin/main, falling back to local main. safe-bash fell
    # back a third time to "" — an empty diff satisfies every cross-check
    # below, so a `git diff` that could not resolve either ref silently
    # switched the gate off. Both refs failing is now a deny.
    rc1, diff, _ = _nixos_git(worktree, ["diff", "origin/main...HEAD"])
    if rc1 != 0 or not diff:
        rc2, diff2, err2 = _nixos_git(worktree, ["diff", "main...HEAD"])
        if rc2 == 0:
            diff = diff2
        elif rc1 != 0:
            _fail_closed(
                "the nixos push gate could not diff this branch against either "
                f"origin/main or main in {worktree!r}, so the checklist's claims "
                f"could not be cross-checked against the code (`git diff` exited "
                f"{rc1} then {rc2}: {err2.strip()[:160]!r})"
            )
    diff_has_risky = bool(_NIXOS_RISKY_MARKERS.search(diff)) if diff else False
    diff_touches_fv = bool(_NIXOS_FEATURE_VM_FILE_IN_DIFF.search(diff)) if diff else False

    type_ = fields.get("type", "").lower()

    if type_ == "pure-data":
        # Report both incompatibilities if they coexist — the markers-only
        # message hides the structural feature-vm.nix signal which is the
        # PR #61 anti-pattern tell.
        reasons: list[str] = []
        if diff_has_risky:
            hits = sorted({m.group(0) for m in _NIXOS_RISKY_MARKERS.finditer(diff)})
            reasons.append("diff contains risky markers: " + ", ".join(hits[:8]))
        if diff_touches_fv:
            reasons.append(
                "diff touches modules/nixos/feature-vm.nix (always 'Type: risky')"
            )
        if reasons:
            return (
                "nixos push gate: checklist claims 'Type: pure-data' "
                "but " + "; ".join(reasons) + ".\n"
                "Update the checklist to 'Type: risky' and fill remaining fields, "
                "OR remove the runtime/branching change from this PR."
            )
        return None

    if type_ != "risky":
        return (
            f"nixos push gate: checklist 'Type:' must be "
            f"'pure-data' or 'risky' (got: {type_!r}).\n\n{_NIXOS_GATE_HELP}"
        )

    required = [
        "rebased on origin/main",
        "local gate",
        "interactive smoke (nixos-agent-testing)",
        "advisor review (advice-refine-test-loop)",
        "feature-vm.nix modified",
        "risky markers in diff",
        "behavioural evidence",
    ]

    # Field counts as empty when blank OR still holding the template
    # `<...>` placeholder. The latter slips past truthy checks but signals
    # the agent left the template untouched.
    def _empty_or_placeholder(v: str) -> bool:
        return (not v) or bool(_NIXOS_TEMPLATE_PLACEHOLDER.match(v.strip()))

    missing = [k for k in required if _empty_or_placeholder(fields.get(k, ""))]
    if missing:
        return (
            "nixos push gate: missing, empty, or template-placeholder "
            "checklist fields: " + ", ".join(missing) + ".\n\n" + _NIXOS_GATE_HELP
        )

    fv_claim = fields["feature-vm.nix modified"].lower()
    fv_claims_no = fv_claim.startswith("no") or fv_claim in ("n", "false")
    if fv_claims_no and diff_touches_fv:
        return (
            "nixos push gate: checklist claims 'feature-vm.nix "
            "modified: no' but the diff DOES touch modules/nixos/feature-vm.nix. "
            "This is the load-bearing tell for hollow smoke evidence (cf. PR #61: "
            "agent stubbed virtiofsd shares to make boot succeed, smoke ran against "
            "a mutated artefact, not the prod path).\n"
            "If the edit was needed to make the smoke test pass, state 'yes' + "
            "explain why the prod code path was still exercised. If the edit is "
            "unrelated, state 'yes (unrelated: <reason>)'."
        )

    rebased_claim = fields["rebased on origin/main"].lower()
    if rebased_claim.startswith("yes"):
        rc_mb, merge_base, err_mb = _nixos_git(worktree, ["merge-base", "HEAD", "origin/main"])
        rc_om, origin_main_sha, err_om = _nixos_git(worktree, ["rev-parse", "origin/main"])
        if rc_mb != 0 or rc_om != 0:
            # safe-bash skipped the comparison here, so an unresolvable
            # origin/main turned the claim into a self-certification.
            _fail_closed(
                "the nixos push gate could not verify the checklist's 'Rebased on "
                f"origin/main: yes' claim in {worktree!r} (`git merge-base` exited "
                f"{rc_mb}, `git rev-parse origin/main` exited {rc_om}: "
                f"{(err_mb or err_om).strip()[:160]!r})"
            )
        if merge_base.strip() != origin_main_sha.strip():
            return (
                "nixos push gate: checklist claims 'Rebased on "
                "origin/main: yes' but `git merge-base HEAD origin/main` "
                f"({merge_base.strip()[:12]}) != `origin/main` "
                f"({origin_main_sha.strip()[:12]}). Branch is behind main; "
                "rebase before pushing."
            )

    return None


# ----------------------------------------------------------------------
def main() -> int:
    if not _TIMEOUTS_SANE:
        _fail_closed(
            "the guard's timeout ladder no longer holds "
            f"(0 < DCG_TIMEOUT_S={DCG_TIMEOUT_S} < GUARD_DEADLINE_S={GUARD_DEADLINE_S} "
            f"< HARNESS_HOOK_TIMEOUT_S={HARNESS_HOOK_TIMEOUT_S}, and "
            f"0 < NIXOS_GIT_TIMEOUT_S={NIXOS_GIT_TIMEOUT_S} < GUARD_DEADLINE_S is false). With the "
            "ladder inverted the harness would time the hook out and render no "
            "decision at all, so the guard denies instead."
        )

    _arm_watchdog()

    try:
        raw = sys.stdin.read()
    except (OSError, ValueError) as exc:
        _fail_closed(f"could not read hook stdin: {type(exc).__name__}: {exc}")

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _fail_closed(
            "hook stdin was not parseable JSON, so the command could not be "
            "inspected"
        )

    if not isinstance(payload, dict):
        _fail_closed("hook stdin was JSON but not an object")

    # Registered with matcher `Bash`; anything else reaching us is not ours
    # to judge, and denying it would be a guard overstepping its scope.
    if payload.get("tool_name") != "Bash":
        _disarm_watchdog()
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        _fail_closed("Bash tool_input was missing or not an object")
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        _fail_closed("Bash call carried no command string to inspect")

    cwd = payload.get("cwd")

    reason = _check_dcg(command)
    if reason is None:
        reason = _check_dangerous_find(command)
    if reason is None:
        reason = _check_pipe_to_shell(command)
    if reason is None:
        reason = _check_nixos_push(command, cwd if isinstance(cwd, str) else None)
        if reason is not None:
            reason = "bash-guard: " + reason

    _disarm_watchdog()
    if reason is not None:
        _emit_deny(reason)
    # Allow: say nothing, so settings.json permissions.deny still decides.
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 — fail-closed is the point
        _disarm_watchdog()
        _emit_deny(
            _deny_broken(
                f"unhandled {type(exc).__name__} inside the guard itself: {exc}"
            )
        )
        sys.exit(0)
