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
  3b. a ``run_in_background`` command that captures ``$?`` and then ends on
     ``echo``/``printf``, so its completion notice always reads exit code 0
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
  * dcg rendered a decision value the guard does not understand. NOT
    ``ask``, which is a value it does understand: dcg's own "I reached no
    verdict" answer. That one blocks as dcg's answer, with dcg's reason,
    rather than as a broken guard — see ``_check_dcg``.
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

One bypass DOES exist and it is not an environment variable: the nixos push
gate honours ``Override: [skip-vm-gate] <reason>`` in the HEAD commit
message, which the agent writes. That is deliberate and documented in the
gate's own help text as "Bypass (audited)" — but note what "audited" buys
today: the audit line goes to STDERR, which this file measures elsewhere as
discarded by the CLI. The commit message itself is the durable record, since
the override has to be written into it and survives in the history.

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

import fnmatch
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


# Explanation only: these never change a verdict. dcg's own reason for a
# recursive delete says "ask the user to run the command manually", so
# sessions doing cleanup CLAUDE.md sanctions either hunted workarounds or
# handed the deletion back (RSI 2026-09-23). The hint names the form that
# passes, scoped to sanctioned cleanup. Rule ids are pinned against the
# packaged dcg by the live lane in tests/test_bash_guard_pre.py.
# `gio trash` stays OUT of permissions.allow: each use keeps its prompt.
# Allowlisted, one call could trash this hook, and a missing hook fails open.
_CLEANUP_DELETE_RULES = frozenset({
    "core.filesystem:rm-rf-general",
    "core.filesystem:rm-recursive-general",
    "core.filesystem:rm-r-f-separate",
    "core.filesystem:rm-recursive-force-long",
    "core.filesystem:find-delete-general",
    "core.git:clean-force",
})
_CLEANUP_DELETE_HINT = (
    "Sanctioned cleanup (CLAUDE.md 'Safe cleanup without asking') only; any "
    "other deletion is the user's call, so ask the user. After verifying the "
    "exact target: under /tmp, `rm -r /tmp/<literal absolute path>`; "
    "elsewhere, `gio trash <literal absolute path>` (reversible). For "
    "`git clean`, list the targets first with `git clean -n` and remove "
    "those literal paths. Trash frees "
    "no space and fails on system mounts, so if the point is reclaiming space "
    "outside /tmp, ask the user. Wrapping the same deletion in another "
    "command does not change the policy."
)
# A move or write to a `$VAR` path is not a deletion; steering it towards
# rm/trash would be wrong, so these rules get only the literal-path advice.
_DYNAMIC_PATH_RULES = frozenset({
    "core.filesystem:mv-dynamic-path",
    "core.filesystem:redirect-truncate-dynamic-path",
})
_DYNAMIC_PATH_HINT = (
    "A path built from `$VAR`, `$(...)` or backslash escapes cannot be "
    "verified before it runs. Resolve it yourself (e.g. `echo \"$VAR\"`), "
    "check it, then put the literal absolute path in the command, or use the "
    "Write tool for file contents."
)


def _dcg_deny_hint(rule: str) -> str:
    if rule in _CLEANUP_DELETE_RULES:
        return _CLEANUP_DELETE_HINT + "\n"
    if rule in _DYNAMIC_PATH_RULES:
        return _DYNAMIC_PATH_HINT + "\n"
    return ""


def _check_dcg(command: str) -> str | None:
    """Ask dcg whether to block. Returns a deny reason, or None to allow.

    Never returns None for a failure: every failure mode routes through
    ``_fail_closed`` and exits. This is the exact inversion of safe-bash's
    ``_check_denied_by_dcg``, which returned None (allow) for all of them.

    Three decision values, three outcomes: ``deny`` blocks with dcg's rule
    and reason, ``allow`` falls through to the later layers, and ``ask``
    blocks too — it is dcg's "I did not reach a verdict" answer, not a
    fault and not a clearance. See the ``ask`` branch below.

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
    ask_reason: str | None = None
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
                    f"{_dcg_deny_hint(rule)}"
                    "This is dcg working as intended, not a broken guard. To run it "
                    "anyway, run it yourself in the prompt with `! <command>`."
                ).strip()
            continue
        if verdict == "allow":
            saw_allow = True
            continue
        if verdict == "ask":
            # dcg's THIRD decision, and the one this guard used to call a
            # fault. It is not: `ask` is what dcg renders when it reached no
            # verdict — `write_indeterminate_to` (src/hook.rs) for a command
            # it did not finish evaluating inside `general.hook_timeout_ms`
            # (1000ms by default) or one larger than
            # `general.max_command_bytes`, and `write_review_request_to` for
            # a rule whose `[policy]` mode is `ask`.
            #
            # Measured 2026-09-04: eight legitimate commands were refused in
            # one morning with "a permissionDecision this guard does not
            # understand", whose recovery text told the operator to install a
            # dcg that was installed and answering throughout. dcg's own
            # evaluation deadline had been elapsing under load — the same
            # commands re-measured at 45-170ms on an idle box.
            #
            # Still a BLOCK: an unevaluated command is not a cleared one. But
            # the guard does not forward the `ask` to Claude Code either. A
            # PreToolUse decision pre-empts the permission system, so an
            # `ask` emitted here would put an approval prompt in front of
            # commands `permissions.deny` refuses outright — the same
            # bypass an "allow" would be, one click away.
            if ask_reason is None:
                rule = hook_out.get("ruleId") or hook_out.get("packId")
                why = (hook_out.get("permissionDecisionReason") or "").strip()
                ask_reason = (
                    "bash-guard: dcg did not clear this command. It answered "
                    "`ask` — its \"no verdict, a human decides\" decision"
                    + (f" (rule: {rule})" if rule else "")
                    + ", which is not an allow.\n"
                    + (why + "\n" if why else "")
                    + "The guard blocks rather than forwarding the question, "
                    "because a PreToolUse decision pre-empts settings.json "
                    "permission rules. To run it anyway, run it yourself in "
                    "the prompt with `! <command>`."
                    # Only the no-rule case is an unevaluated command. A named
                    # rule means dcg DID evaluate and its policy asks for a
                    # human; telling the agent to re-run that would send it
                    # round a loop the retry can never break.
                    + (
                        ""
                        if rule
                        else "\nNo rule is named, so dcg did not evaluate the "
                        "command at all: its deadline "
                        "(general.hook_timeout_ms) elapsed, or the command "
                        "exceeded general.max_command_bytes. Neither is a "
                        "verdict about the command — re-running it, or "
                        "splitting it, normally clears."
                    )
                )
            continue
        _fail_closed(
            "dcg rendered a permissionDecision this guard does not understand: "
            f"{verdict!r}"
        )

    if deny_reason is not None:
        return deny_reason
    # An ask is a blocking answer, so — exactly as for a deny — dcg's exit
    # status and config state cannot change the outcome and are not checked.
    # dcg's own precedence puts a proven Deny above an unevaluated Ask, which
    # is why this comes second.
    if ask_reason is not None:
        return ask_reason

    # dcg SAID something on stdout and none of it was a decision this guard
    # could read. Measured, dcg 0.12.5 writes exactly one JSON line for a deny
    # and NOTHING AT ALL for an allow — the decorated banner goes to stderr —
    # so any other stdout is a shape this guard has never seen.
    #
    # The loop above skips every line that does not open with `{`. That made
    # "dcg printed a deny in a format we do not parse" render as ALLOW, which
    # the file's own fail-closed list (line 41: "dcg emitted output that could
    # not be parsed") already promises it does not do. The promise only held
    # for a line that LOOKED like JSON and then failed to parse; a future dcg
    # that prefixes or reformats its decision would have walked straight past
    # it. Silence is the allow. Noise we cannot read is not.
    if (proc.stdout or "").strip() and not saw_allow:
        _fail_closed(
            "dcg wrote output this guard could not read as a decision, so its "
            "verdict is unknown (stdout began: "
            f"{(proc.stdout or '').strip()[:160]!r})"
        )

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
# Layer 3b: a background command that captures its exit status and then
# throws it away on a trailing echo
# ----------------------------------------------------------------------
# A `run_in_background` call is reported back as "completed (exit code N)",
# and N is the status of the LAST statement. Capture `$?`, end on `echo`, and
# N is always 0. Measured 2026-09-28 (klaffat): `(scripts/check affected >
# gate3.log 2>&1; echo EXIT=$? >> gate3.log)` ran with one failing e2e test,
# notified exit code 0, and a push went out on it. A foreground call is not
# caught: its output, `$?` line included, is read directly.
_STATUS_CAPTURE = re.compile(r"\$\?|\$\{\?\}|PIPESTATUS")
_STATUS_DISCARDING_TAIL = re.compile(r"^[\s({]*(?:builtin\s+)?(?:echo|printf)\b")

_BG_STATUS_MASKED_HINT = (
    "bash-guard: this background command captures its exit status and then "
    "ends on echo/printf, so the completion notice will report exit code 0 "
    "even if the command failed. That masked a failing gate as green and a "
    "push went out on it (2026-09-28).\n"
    "Keep the log line and re-raise the status as the last statement:\n"
    "  (<cmd> > LOG 2>&1; rc=$?; echo EXIT=$rc >> LOG; exit $rc)"
)


def _check_bg_status_masked(command: str, background: bool) -> str | None:
    if not background or not _STATUS_CAPTURE.search(command):
        return None
    body = command.rstrip().rstrip(")}; \t\n")
    parts = [(seg, op) for seg, op in _split_operators(body) if seg]
    if not parts or not _STATUS_DISCARDING_TAIL.match(parts[-1][0]):
        return None
    # `cmd && echo …` skips the echo when cmd fails, so cmd's status is reported.
    if len(parts) > 1 and parts[-2][1] == "&&":
        return None
    # A `$?` inside a loop body tests an in-loop command; only a capture after
    # the last `done` is a status the trailing echo throws away.
    last_done = max((i for i, (seg, _op) in enumerate(parts)
                     if re.match(r"done\b", seg)), default=-1)
    if not any(_STATUS_CAPTURE.search(seg) for seg, _op in parts[last_done + 1:]):
        return None
    return _BG_STATUS_MASKED_HINT


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
#
# What is deliberately TIGHTENED (K01, 2026-10-03): safe-bash let any push
# from a checkout on `main` through as "branch-protected upstream". The
# upstream protection does not bind admins, so every push whose destination
# is main is now refused outright — see `_nixos_push_to_main_reason`.
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
# The same header with leading whitespace, which the anchored one CANNOT
# match. Recognised only to name the cause: an indented block parses as no
# checklist, and answering that with the generic "there is no checklist here"
# help sends the writer looking for a block they can see in front of them.
_NIXOS_INDENTED_HEADER = re.compile(r"^[ \t]+Pre-push checklist:\s*$", re.MULTILINE)
# Field lines are top-level only (anchored `^-\s`, NOT `^\s*-\s`). Indented
# sub-bullets inside a multi-line `Behavioural evidence` value must not
# overwrite the parent field map — they are joined onto the value above
# instead (see `_nixos_parse_checklist`), which is also what stops them
# truncating the block.
_NIXOS_CHECKLIST_FIELD = re.compile(r"^-\s*([^:\n]+?)\s*:\s*(.*)$")
_NIXOS_OVERRIDE = re.compile(r"^Override:\s*\[skip-vm-gate\]\s*(\S.+)$", re.MULTILINE)
# `<placeholder>` template hints (`<list, or 'none'>`, `<cmd + output>`) left
# unfilled by the agent are treated as empty. The angle-brackets are the
# template's hint marker; a real value never OPENS with one.
#
# Anchored at the start only, not `^<.*>$`. The end-anchored form was defeated
# the moment values could wrap: `- Risky markers in diff: <list, or 'none'>`
# with any continuation line joined onto it no longer ends in `>`, so an
# untouched template hint read as a filled field. Prefix-matching keeps the
# check honest for both shapes, and costs nothing real — a value that opens
# with a bracketed phrase is the template hint whatever follows it.
_NIXOS_TEMPLATE_PLACEHOLDER = re.compile(r"^<[^>]*>")
_NIXOS_FEATURE_VM_FILE_IN_DIFF = re.compile(
    r"^diff --git .*modules/nixos/feature-vm\.nix\b", re.MULTILINE
)

# The `Type: risky` fields the gate insists on, in template order. Module
# level because the wrap diagnosis needs them BEFORE `Type:` is dispatched on
# — a wrap early enough to hide `Type:` itself must be reported as a wrap,
# not as a missing/invalid Type.
_NIXOS_REQUIRED_FIELDS = (
    "rebased on origin/main",
    "local gate",
    "interactive smoke (nixos-agent-testing)",
    "advisor review (advice-refine-test-loop)",
    "feature-vm.nix modified",
    "risky markers in diff",
    "behavioural evidence",
)
_NIXOS_CHECKLIST_KNOWN_FIELDS = ("type",) + _NIXOS_REQUIRED_FIELDS

_NIXOS_GATE_HELP = (
    "nixos-config push gate refused this push (remote points at "
    "jonathanmoregard/nixos-config).\n\n"
    "HEAD commit message must contain a 'Pre-push checklist:' block.\n"
    # The template is printed FLUSH LEFT, and that is load-bearing rather than
    # cosmetic. The header regex and the field regex are both anchored at
    # column 0, so an indented copy of this block parses as no checklist at
    # all — and the gate answers a verbatim copy of its own template with the
    # identical refusal, which is a loop with no exit. It used to print two
    # spaces of indentation under the words "copy verbatim".
    "Template — copy it FLUSH LEFT (leading spaces void the block), then "
    "fill the values:\n\n"
    "Pre-push checklist:\n"
    "- Type: pure-data           # OR 'risky' for branching/multistep/runtime\n"
    "- Rebased on origin/main: yes\n"
    "- Local gate: nix build .#checks.x86_64-linux.dellan-vm rc=0\n"
    "- Interactive smoke (nixos-agent-testing): <yes — cmd + observed | N/A — reason>\n"
    "- Advisor review (advice-refine-test-loop): <yes — rounds + verdict | N/A — reason>\n"
    "- feature-vm.nix modified: no    # MUST match actual diff\n"
    "- Risky markers in diff: <list, or 'none'>\n"
    "- Behavioural evidence: <what you actually exercised — command + observed output>\n\n"
    "A value too long for one line continues on the NEXT line indented by two "
    "spaces; that attaches it to the field above. An unindented continuation "
    "ends the block.\n\n"
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
    rc, out, err = _nixos_git(repo_root, ["remote", "-v"])
    if rc != 0:
        # The caller only reaches here for a path `git rev-parse
        # --show-toplevel` already answered for, so this IS a working tree and
        # `git remote -v` failing means git could not tell us its remotes —
        # not that it has none. Returning False rendered those two
        # identically, and False switches the whole gate off.
        # Reproduced: a `.git/config` git cannot parse (`fatal: bad config
        # line 1`, exit 128) made a real nixos-config clone answer "not a
        # nixos repo", and `git push` sailed past the checklist gate.
        _fail_closed(
            f"the nixos push gate could not list the remotes of {repo_root!r}, "
            "so it cannot tell whether this is a nixos-config push "
            f"(`git remote -v` exited {rc}: {err.strip()[:160]!r})"
        )
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and _remote_is_nixos_config(parts[1]):
            return True
    return False


def _candidate_repo_paths(command: str, cwd: str | None) -> tuple[list[str], list[str]]:
    """Candidate working-dir paths to probe for a git repo.

    Returns ``(paths, unresolved)``. ``unresolved`` names each candidate that
    could not be turned into a path at all.

    Order (the first that IS a repository wins — `_nixos_resolve_target_repo`):
      1. `git -C <path>` flag
      2. Last `cd <path>` target in the command's pipeline chain
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
    # `git -C <path>` names the push's repository on the git command itself,
    # so it outranks a `cd` earlier in the chain: `cd A && git -C B push`
    # pushes B. Both outrank the session cwd, which only matters for a push
    # that names no path at all.
    m = re.search(r"\bgit\s+(?:[^\n;|&]*?\s+)?-C\s+(\S+)", command)
    if m:
        raw = m.group(1).strip("\"'")
        try:
            candidates.append(os.path.abspath(os.path.expanduser(raw)))
        except (ValueError, OSError) as exc:
            unresolved.append(f"the `git -C {raw[:80]}` path ({type(exc).__name__})")
    cd_target = _nixos_extract_cd_target(command)
    if cd_target:
        candidates.append(cd_target)
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


# git's stderr for the failures that are ANSWERS rather than faults — see
# `_nixos_resolve_target_repo`, the only reader. Matched case-insensitively
# as substrings, because git's wording carries a path and a mount point
# around them.
#
# The membership test is NOT "does this mean the path is not a repo". It is:
# does it mean GIT ITSELF WILL NOT OPERATE ON THIS PATH FOR THIS USER? The
# guard runs as the user whose push it is gating, so any path git refuses
# here is a path the push cannot succeed from either — and a gate cannot be
# evaded through a door that does not open.
#
#   not a git repository       — no repo here.
#   cannot change to           — the directory does not exist.
#   detected dubious ownership — git refuses the repo outright (root-owned
#                                /etc/nixos, from a user session) until a
#                                `safe.directory` exception is added.
#
# That last one was added after the fail-closed branch below denied EVERY
# `git push` from a session sitting in /etc/nixos — measured, not predicted.
# Failing closed there bought nothing: git will not push from that path
# either, so the deny only refused work that could not have happened.
_NIXOS_GIT_UNUSABLE_PATH = (
    "not a git repository",
    "cannot change to",
    "detected dubious ownership",
)


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

    THE CANDIDATES ARE FALLBACKS, NOT A SEARCH. The first one that IS a
    working tree is the repository the push goes to — `cd <repo> && git
    push` pushes <repo>, whatever directory the session sits in. The loop
    used to keep walking after a candidate resolved to a repo that was not
    nixos-config, and fire on the session cwd instead; measured 2026-10-02,
    `cd ~/worktrees/survival-corpus-first-aid && git push` was refused for
    the trailer missing from a nixos-config worktree's merge commit. Later
    candidates are consulted only when an earlier one was not a repository
    at all.
    """
    paths, unresolved = _candidate_repo_paths(command, cwd)
    for path in paths:
        rc, toplevel, err = _nixos_git(path, ["rev-parse", "--show-toplevel"])
        if rc != 0:
            # Some of git's non-zero exits are ANSWERS about the candidate —
            # paths git will not operate on for this user at all, so no push
            # can succeed from them either. `_NIXOS_GIT_UNUSABLE_PATH` lists
            # them and states the membership rule.
            #
            # Every other non-zero exit is git failing to answer: a
            # `.git/config` it cannot parse (`fatal: bad config line 1`, exit
            # 128), a dubious-ownership refusal, an unreadable object store.
            # Those used to take the same `continue`, which switched the gate
            # off for a real nixos-config clone. Reproduced: corrupting
            # `.git/config` in a clone whose remote IS nixos-config let
            # `git push` past the checklist entirely.
            if any(m in err.lower() for m in _NIXOS_GIT_UNUSABLE_PATH):
                continue
            if "must be run in a work tree" in err.lower():
                # A bare repo, or a `.git` directory as the cwd. There is no
                # working tree to name, but there IS a repository, and a push
                # from a bare clone is a real push — so this must neither
                # deny (it would refuse every routine push from such a cwd)
                # nor allow blind. Ask git for the repository instead.
                rc_gd, gitdir, err_gd = _nixos_git(
                    path, ["rev-parse", "--absolute-git-dir"]
                )
                if rc_gd == 0 and gitdir.strip():
                    if _repo_has_nixos_remote(gitdir.strip()):
                        return gitdir.strip()
                    break  # a repository, and not nixos-config: that is the answer
                _fail_closed(
                    f"the nixos push gate found a git repository at {path!r} with "
                    "no working tree and could not resolve its git directory "
                    f"either (`git rev-parse --absolute-git-dir` exited {rc_gd}: "
                    f"{err_gd.strip()[:160]!r})"
                )
            _fail_closed(
                f"the nixos push gate could not determine whether {path!r} is a "
                "git working tree, so it cannot say this push is NOT a "
                f"nixos-config push (`git rev-parse --show-toplevel` exited "
                f"{rc}: {err.strip()[:160]!r})"
            )
        repo_root = toplevel.strip()
        if not repo_root:
            continue
        if _repo_has_nixos_remote(repo_root):
            return repo_root
        break  # the push's repository is this one, and it is not nixos-config
    if unresolved:
        _fail_closed(
            "the nixos push gate found no nixos-config repo among the paths it "
            "could resolve, but it could not resolve " + "; ".join(unresolved)
            + " — so it cannot say this push is NOT a nixos-config push"
        )
    return None


def _nixos_command_pushes(command: str) -> bool:
    """True if a `git push` is present (excluding --dry-run).

    The flag run before `push` accepts `k=v` tokens. `[\\w-]+` alone cannot
    span an `=`, so `git -c foo=bar push` and `git -c protocol.version=2 push`
    matched nothing, the gate never fired, and the whole checklist check was
    one documented git flag away from being skipped. Reproduced against this
    predicate directly.
    """
    for sub in _nixos_pipeline_subcommands(command):
        if not re.search(r"\bgit\s+(?:-C\s+\S+\s+)?(?:[\w.-]+(?:=\S+)?\s+)*push\b", sub):
            continue
        # `--dry-run` switches the gate off, so it must be a real flag on the
        # real command and not merely present in the line. A trailing shell
        # COMMENT is not part of the command: `git push origin feat #
        # --dry-run` pushes for real, and reading the comment let the whole of
        # layer 4 be skipped by a string the gated agent writes itself.
        # Reproduced against this predicate.
        effective = re.split(r"(?:^|\s)#", sub, maxsplit=1)[0]
        if re.search(r"(?:^|\s)--dry-run(?:\s|$)", effective):
            continue
        return True
    return False


class _ChecklistParse(dict):
    """The field map, plus what ended the block and what was written below it.

    A plain `dict` subclass so every existing caller — `fields.get(...)`,
    `fields is None`, `isinstance(..., dict)` — keeps working unchanged. The
    two extra attributes exist only so the gate can tell a WRAPPED value
    from a genuinely absent field; see `_nixos_parse_checklist`.
    """

    #: The line that ended the block, stripped, or None if nothing did.
    terminator: str | None = None
    #: Top-level `- Field:` keys that appear BELOW the terminator, in order.
    fields_after: tuple[str, ...] = ()


def _nixos_parse_checklist(commit_msg: str) -> _ChecklistParse | None:
    """Extract the `Pre-push checklist:` field map. None if header missing.

    Block ends at the first non-field, non-blank, non-comment, NON-INDENTED
    line after the header, or at end-of-message. Keys lowercased + stripped.

    WRAPPED VALUES, and why the end-of-block rule is not enough on its own
    ---------------------------------------------------------------------
    A long value that continues on the next line used to be "the first
    unrelated line", which ended the block — so every field written BELOW it
    disappeared from the map and the gate reported them as
    `missing, empty, or template-placeholder`. Measured twice on 2026-09-04
    (PR #207): four fields were named, three of them single-line and
    verbatim correct, and the real cause — a wrap two fields earlier — was
    not mentioned. The strictness was never the defect; the misattribution
    was, and it cost ~20 minutes of looking at the wrong lines.

    Two shapes, two answers:

      * INDENTED continuation (`  more text`) — unambiguous, so it is joined
        onto the value above and parsing continues. This also subsumes the
        indented sub-bullets of a multi-line `Behavioural evidence` value:
        they become part of that value instead of truncating the block, and
        they still cannot overwrite a top-level field, because
        `_NIXOS_CHECKLIST_FIELD` is anchored at column 0.
      * UNINDENTED continuation — indistinguishable from the commit body
        resuming, so the block still ends there. But the terminating line
        and any checklist fields written below it are recorded, which is
        what lets `_check_nixos_push` name the wrap instead of accusing
        those fields of being absent.
    """
    if not _NIXOS_CHECKLIST_HEADER.search(commit_msg):
        return None
    fields = _ChecklistParse()
    in_block = False
    last_key: str | None = None
    blank_since_field = False
    lines = commit_msg.splitlines()
    for i, line in enumerate(lines):
        if _NIXOS_CHECKLIST_HEADER.match(line):
            in_block = True
            last_key = None
            blank_since_field = False
            continue
        if not in_block:
            continue
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            # A blank line is not a terminator, but it does mean anything
            # after it is no longer CONTIGUOUS with the last field — which is
            # what tells a wrapped value apart from the commit body resuming.
            if stripped == "":
                blank_since_field = True
            continue
        m = _NIXOS_CHECKLIST_FIELD.match(line)
        if m:
            key = m.group(1).strip().lower()
            val = m.group(2).strip()
            # Drop inline `# comment` tails so the template's hint text doesn't
            # poison values when the agent left it untrimmed.
            val = re.sub(r"\s+#.*$", "", val).strip()
            fields[key] = val
            last_key = key
            blank_since_field = False
            continue
        if line[:1].isspace() and last_key is not None:
            # Indented continuation of the value above.
            tail = re.sub(r"\s+#.*$", "", stripped).strip()
            if tail:
                fields[last_key] = (fields[last_key] + " " + tail).strip()
            continue
        # First unrelated line ends the block. Record it, and the fields
        # written in the RUN that follows it, so a wrap can be diagnosed as a
        # wrap.
        #
        # CONTIGUITY is what tells the two apart, and it is checked on both
        # sides of the terminating line. A wrapped value and the fields it
        # swallowed run together: no blank line before the terminator (it is
        # the continuation of the field above it) and none between it and the
        # fields below. A commit BODY that happens to bullet a field name
        # ("Context for the reviewer:" then "- Risky markers in diff: none")
        # is separated from the block by a blank line, and reporting that as
        # a wrap is a false deny on a legitimate push — the same
        # misdiagnosis this parser exists to end, pointed the other way.
        fields.terminator = stripped
        after: list[str] = []
        if not blank_since_field:
            for later in lines[i:]:
                if later.strip() == "":
                    break
                m2 = _NIXOS_CHECKLIST_FIELD.match(later)
                if m2:
                    after.append(m2.group(1).strip().lower())
        fields.fields_after = tuple(after)
        break
    return fields


# K01 (security review 2026-10-03): nixos-config `main` is fetched and
# activated as root by nixos-auto-deploy, and the upstream protection does not
# bind admins (enforce_admins=false), so "pushing main is branch-protected
# upstream" was not a control: a direct push skipped every required check.
# Every push whose DESTINATION is main is refused here, before the checklist
# and before `Override: [skip-vm-gate]`, which is a vm-gate bypass and never a
# licence to skip review. Main changes land through a pull request.
_NIXOS_PROTECTED_BRANCH = "main"
# `git push` options that consume the NEXT argument as their value.
_GIT_PUSH_VALUE_OPTS = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
# Options that push every local branch, main included.
_GIT_PUSH_EVERY_BRANCH_OPTS = frozenset({"--all", "--mirror", "--branches"})
_NIXOS_MAIN_PUSH_HELP = (
    "nixos push gate: this push would update nixos-config `main` directly "
    "({why}).\n\n"
    "main auto-deploys as root, and the upstream branch protection does not "
    "bind admins, so a direct push skips every required check. Push a feature "
    "branch and open a pull request instead: from a "
    "~/Repos/nixos-config-worktrees/<slug> worktree, `git push -u origin "
    "<branch>`, then `gh pr create`.\n"
    "There is no override: `Override: [skip-vm-gate]` does not apply to main."
)
_NIXOS_UNRESOLVED_PUSH_HELP = (
    "nixos push gate: cannot tell where this nixos-config push lands ({why}), "
    "so it is refused rather than guessed (a push to main must never slip "
    "through a wrapper).\n\n"
    "Issue the push as a plain literal command instead: "
    "`git push <remote> <branch>` with the names written out, e.g. "
    "`git push -u origin my-feature` — no `bash -c`/`eval`/`xargs`, no "
    "`$VAR` and no `$(...)`."
)


_HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


# A heredoc whose body is only DATA: a commit message read by `git commit -F -`
# (or `git tag -F -`). Any other opener (bash, python3, cat | sh, ...) may
# execute its body.
_HEREDOC_DATA_OPENER = re.compile(r"\bgit\s+(?:commit|tag)\b[^|;&]*\s-F\s*-(?:\s|$)")
_GIT_PUSH_TEXT = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(?:[\w.-]+(?:=\S+)?\s+)*push\b")


def _strip_heredoc_bodies(command: str) -> tuple[str, bool]:
    """Drop heredoc bodies, and say whether a push hid inside an executable one.

    A commit-message body that merely MENTIONS `git push` next to `$(...)` must
    not read as an unresolvable push: on 30 days of real nixos-config commands
    those were the main false hit. But `bash <<EOF` / `python3 - <<EOF` run
    their body, so a push inside one of those is reported (fail closed)."""
    out: list[str] = []
    ends: list[str] = []
    data_body = False
    hidden_push = False
    for line in command.split("\n"):
        if ends:
            if line.strip() == ends[0]:
                ends.pop(0)
            elif not data_body and _GIT_PUSH_TEXT.search(line):
                hidden_push = True
            continue
        out.append(line)
        opened = [m.group(2) for m in _HEREDOC_START.finditer(line)]
        if opened:
            data_body = bool(_HEREDOC_DATA_OPENER.search(line))
            ends.extend(opened)
    return "\n".join(out), hidden_push


def _nixos_push_shapes(command: str) -> list[tuple[str, object]]:
    """Every git push in the command, classified for the main-destination check.

    ("literal", args)    a plain `[VAR=x ...] git ... push <args>` whose args
                         are all literal words: the destination is knowable.
    ("unresolved", why)  a push the parser cannot see through: a shell wrapper
                         (`bash -c`, `eval`, `xargs`, `env -S`, ...), command
                         substitution, or a `$variable` in the git argv. These
                         FAIL CLOSED (K01 close-out: `bash -c 'git push origin
                         main'` reached main because the parser never saw it).
    A literal push carrying `--dry-run` is dropped: it updates nothing.
    """
    out: list[tuple[str, object]] = []
    stripped, hidden_push = _strip_heredoc_bodies(command)
    if hidden_push:
        out.append(("unresolved", "the push sits in a heredoc that a shell or interpreter runs"))
    for sub in _nixos_pipeline_subcommands(stripped):
        effective = re.split(r"(?:^|\s)#", sub, maxsplit=1)[0]
        if not _GIT_PUSH_TEXT.search(effective):
            continue
        if "`" in effective or "$(" in effective or "<(" in effective:
            out.append(("unresolved", "command substitution in the push"))
            continue
        try:
            toks = shlex.split(effective)
        except ValueError:
            out.append(("unresolved", "quoting the gate cannot parse"))
            continue
        git_at = next((i for i, t in enumerate(toks) if os.path.basename(t) == "git"), None)
        if git_at is None or "push" not in toks[git_at + 1:]:
            wrapper = toks[0] if toks else "a wrapper"
            out.append(("unresolved", f"the push runs inside `{wrapper}`"))
            continue
        prefix = [t for t in toks[:git_at] if not _ENV_ASSIGNMENT.match(t)]
        if prefix:
            out.append(("unresolved", f"the push runs through `{prefix[0]}`"))
            continue
        if any("$" in t for t in toks[git_at:]):
            out.append(("unresolved", "a $variable in the push"))
            continue
        args = toks[toks.index("push", git_at + 1) + 1:]
        opts = args[: args.index("--")] if "--" in args else args
        if "--dry-run" in opts or "-n" in opts:
            continue
        out.append(("literal", args))
    return out


def _nixos_push_arg_lists(command: str) -> list[list[str]]:
    """The arguments after `push` for every literal, non-dry-run git push."""
    return [a for kind, a in _nixos_push_shapes(command) if kind == "literal" and isinstance(a, list)]


def _nixos_ref_is_main(dst: str) -> bool:
    if "*" in dst:  # glob refspec, e.g. refs/heads/*:refs/heads/*
        return fnmatch.fnmatchcase("refs/heads/" + _NIXOS_PROTECTED_BRANCH, dst)
    for prefix in ("refs/heads/", "heads/"):
        if dst.startswith(prefix):
            return dst[len(prefix):] == _NIXOS_PROTECTED_BRANCH
    return dst == _NIXOS_PROTECTED_BRANCH


def _nixos_default_push_dst(worktree: str, branch: str) -> str:
    """Where a refspec-less push sends the current branch, per git itself."""
    rc, mode, _ = _nixos_git(worktree, ["config", "--get", "push.default"])
    if rc == 0 and mode.strip() == "matching":
        return _NIXOS_PROTECTED_BRANCH  # every same-named branch, main included
    rc, ref, _ = _nixos_git(worktree, ["rev-parse", "--symbolic-full-name", "@{push}"])
    ref = ref.strip()
    if rc == 0 and ref.startswith("refs/remotes/"):
        return ref.split("/", 3)[3] if ref.count("/") >= 3 else ref
    return branch


def _nixos_push_to_main_reason(command: str, worktree: str, branch: str) -> str | None:
    """Why this command would update main, or None if no push lands there."""
    for args in _nixos_push_arg_lists(command):
        positional: list[str] = []
        flags: set[str] = set()
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--":
                positional.extend(args[i + 1:])
                break
            if a.startswith("-") and a != "-":
                name = a.split("=", 1)[0]
                if name in _GIT_PUSH_EVERY_BRANCH_OPTS:
                    return f"`{a}` pushes every branch, main included"
                flags.add(name)
                if not a.startswith(("--", "-o")) and "d" in a[1:]:
                    flags.add("--delete")  # -d, or bundled like -fd
                takes_next = name in _GIT_PUSH_VALUE_OPTS and "=" not in a and (
                    a.startswith("--") or a == "-o"
                )
                i += 2 if takes_next else 1
                continue
            positional.append(a)
            i += 1
        refspecs = positional[1:]
        if not refspecs:
            if flags & {"--tags", "-d", "--delete"}:
                continue
            dst = _nixos_default_push_dst(worktree, branch)
            if _nixos_ref_is_main(dst):
                return f"a push without a refspec sends branch {branch!r} to {dst!r}"
            continue
        for spec in refspecs:
            s = spec.lstrip("+")
            if "--delete" in flags:
                dst = s
            elif ":" in s:
                src, dst = s.split(":", 1)
                dst = dst or src
            else:
                dst = s
            if dst in ("HEAD", "@"):
                dst = branch
            if _nixos_ref_is_main(dst):
                return f"refspec `{spec}` targets main"
    return None


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
    shapes = _nixos_push_shapes(command)
    unresolved = [why for kind, why in shapes if kind == "unresolved"]
    # `_nixos_command_pushes` drops any line carrying `--dry-run`; a wrapped
    # push (`bash -c 'git push origin main' --dry-run`) must not ride on that.
    if not _nixos_command_pushes(command) and not unresolved:
        return None
    worktree = _nixos_resolve_target_repo(command, cwd)
    if not worktree:
        return None
    if unresolved:
        return _NIXOS_UNRESOLVED_PUSH_HELP.format(why=unresolved[0])
    rc, branch, _ = _nixos_git(worktree, ["branch", "--show-current"])
    branch = branch.strip() if rc == 0 else ""
    to_main = _nixos_push_to_main_reason(command, worktree, branch)
    if to_main:
        return _NIXOS_MAIN_PUSH_HELP.format(why=to_main)
    if branch == _NIXOS_PROTECTED_BRANCH:
        # From a main checkout, a push that does NOT land on main (a tag, or
        # main's tip to a feature branch for a PR) keeps its old verdict.
        return None

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
        if _NIXOS_INDENTED_HEADER.search(msg):
            return (
                "nixos push gate: this commit message HAS a 'Pre-push "
                "checklist:' line, but it is INDENTED, and the gate anchors "
                "both the header and every `- Field:` line at column 0 — so "
                "the block it can see is not one it can read.\n"
                "Move the whole block flush left. Nothing else about it needs "
                "to change.\n\n" + _NIXOS_GATE_HELP
            )
        return _NIXOS_GATE_HELP

    # A checklist field written BELOW the line that ended the block is a
    # field the gate can see but cannot attribute. Report THAT, before any
    # check that would otherwise blame the fields themselves.
    # `not in fields` keeps this to the wrap case only: a commit body that
    # quotes the template — a commit ABOUT this gate, say — repeats field
    # names below the block while the block itself parsed fine, and must not
    # be refused for it.
    hidden = [
        k
        for k in fields.fields_after
        if k in _NIXOS_CHECKLIST_KNOWN_FIELDS and k not in fields
    ]
    if hidden:
        return (
            "nixos push gate: the 'Pre-push checklist:' block ends early. This "
            "line is neither `- Field: value` nor blank, so it closes the "
            "block:\n\n"
            f"    {(fields.terminator or '')[:200]}\n\n"
            "These checklist fields are written BELOW it and were therefore "
            "never read: " + ", ".join(dict.fromkeys(hidden)) + ".\n\n"
            "That shape is almost always a WRAPPED value — a long value "
            "continued on the next line. Fix it either way:\n"
            "  - keep each value on ONE line, or\n"
            "  - indent continuation lines by two spaces, which attaches them "
            "to the field above.\n\n"
            "This is NOT a report that those fields are absent. They are in "
            "the commit message; the gate can see them but cannot tell which "
            "field each one belongs to, because the block ended above them."
        )

    # Diff against origin/main, falling back to local main. safe-bash fell
    # back a third time to "" — an empty diff satisfies every cross-check
    # below, so a `git diff` that could not resolve either ref silently
    # switched the gate off. Both refs failing is now a deny.
    #
    # Fall back ONLY when the origin/main diff FAILED. An empty diff from a
    # resolvable origin/main is a real answer — the branch adds nothing on
    # top of origin/main (already merged, or HEAD sits on origin/main's tip)
    # — and there is then nothing for the claims to contradict. Falling back
    # on empty compared HEAD against a stale local `main` instead, whose
    # unrelated history then produced false "feature-vm.nix modified" and
    # risky-marker mismatches.
    rc1, diff, _ = _nixos_git(worktree, ["diff", "origin/main...HEAD"])
    if rc1 != 0:
        rc2, diff2, err2 = _nixos_git(worktree, ["diff", "main...HEAD"])
        if rc2 == 0:
            diff = diff2
        else:
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

    required = list(_NIXOS_REQUIRED_FIELDS)

    # Field counts as empty when blank OR still holding the template
    # `<...>` placeholder. The latter slips past truthy checks but signals
    # the agent left the template untouched.
    def _empty_or_placeholder(v: str) -> bool:
        return (not v) or bool(_NIXOS_TEMPLATE_PLACEHOLDER.match(v.strip()))

    missing = [k for k in required if _empty_or_placeholder(fields.get(k, ""))]
    if missing:
        # Where the block ended on something, say so even when the wrap
        # diagnosis above did not fire — a blank line before the terminator
        # suppresses that diagnosis (it is what tells a wrap from the commit
        # body resuming), and without this line the message reverts to the
        # bare "missing fields" list that sent the last reader looking at the
        # wrong lines for twenty minutes.
        tail = ""
        if fields.terminator:
            tail = (
                "\n\nNote: the checklist block ended at this line, so nothing "
                "below it was read:\n\n"
                f"    {fields.terminator[:200]}\n\n"
                "If a value wrapped there, indent the continuation by two "
                "spaces or put the value on one line."
            )
        return (
            "nixos push gate: missing, empty, or template-placeholder "
            "checklist fields: " + ", ".join(missing) + "." + tail
            + "\n\n" + _NIXOS_GATE_HELP
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
# Layer 5: foreground CI polling
# ----------------------------------------------------------------------
# `sleep N; gh pr checks X` in the foreground holds the turn for N seconds and
# then returns a snapshot that is usually "still pending", so it gets re-issued
# turn after turn: measured 2026-09-12 at 12-19 polls per wait on NixOS VM
# suites, and still live 2026-10-02 (`sleep 20/40/150; gh pr checks …`). gh
# ships the blocking primitive; run in the background, the harness re-invokes
# the session when it exits, so one call replaces the whole loop. A single
# status look (no sleep) is fine and stays allowed.
#
# The same loop shows up for waits gh has no `--watch` for: GitHub's lazily
# computed mergeability (`sleep 15; gh pr view … mergeable` while it reads
# UNKNOWN) and deploys/units (`sleep 30; systemctl status …`). scripts/wait-for
# is the blocking primitive for those, so a status read of either kind counts.
# A unit status read is held to a narrower test: replayed against ~30k real
# foreground commands, "act, then look" (`systemctl restart x && sleep 2 &&
# systemctl status x`) is a common health check, not a wait. So it counts only
# inside a loop or after a sleep over _UNIT_SETTLE_S, and never when the same
# command acts on a unit.
_SHELL_KEYWORDS = frozenset({"do", "then", "else", "while", "until", "if", "!", "{", "(", "time"})

_CI_POLL_HINT = (
    "bash-guard: foreground CI poll denied — `sleep` plus a one-shot CI status "
    "snapshot (`gh pr checks` / `gh pr view` / `gh run view|list` / `systemctl "
    "status|is-active|show`) holds the "
    "turn and still returns a snapshot, so it gets repeated: a hand-rolled wait "
    "loop.\n"
    "Use the blocking primitive, with Bash run_in_background: true — you are "
    "re-invoked when it exits:\n"
    "  ~/.claude/scripts/wait-for pr <PR> [-R owner/repo]   (CI settled + mergeability known)\n"
    "  ~/.claude/scripts/wait-for unit <name> [--user] [--since now]   (deploy/unit run finished)\n"
    "  ~/.claude/scripts/wait-for run <RUN_ID> [-R owner/repo]\n"
    "  or gh's own: gh pr checks <PR> --watch --fail-fast / gh run watch <RUN_ID> --exit-status\n"
    "Checks not registered yet right after a push? Keep the delay in front of the "
    "watch: `sleep 20; gh pr checks <PR> --watch --fail-fast` (background). "
    "Same for fetching a fresh run id after a push.\n"
    "Long or must survive /clear: scheduling-follow-through (durable CronCreate). "
    "A single status look without `sleep` is allowed."
)


def _segment_tokens(segment: str) -> list[str]:
    try:
        toks = shlex.split(segment)
    except ValueError:
        toks = segment.split()
    while toks and toks[0] in _SHELL_KEYWORDS:
        toks = toks[1:]
    return toks


_UNIT_STATUS_VERBS = frozenset({"status", "is-active", "is-failed", "show"})
_UNIT_ACTION_VERBS = frozenset({"start", "restart", "reload", "try-restart",
                                "reload-or-restart", "enable", "stop"})
_LOOP_KEYWORDS = frozenset({"while", "until", "for"})
_UNIT_SETTLE_S = 10


def _systemctl_verb(toks: list[str]) -> str | None:
    if not toks or toks[0] != "systemctl":
        return None
    verbs = [t for t in toks[1:] if not t.startswith("-")]
    return verbs[0] if verbs else None


def _sleep_seconds(toks: list[str]) -> float:
    """Seconds a `sleep` segment waits; unparseable counts as long."""
    total = 0.0
    for arg in toks[1:]:
        m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", arg)
        if not m:
            return float("inf")
        total += float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return total


def _is_ci_snapshot(toks: list[str]) -> bool:
    if len(toks) < 3 or toks[0] != "gh":
        return False
    if "--watch" in toks:
        return False
    sub = toks[1:3]
    return sub in (["pr", "checks"], ["pr", "view"], ["run", "view"], ["run", "list"])


_HEREDOC_OP_RE = re.compile(r"(?<!<)<<(?!<)-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _split_heredocs(command: str) -> tuple[str, list[str]]:
    """(shell text with heredoc bodies removed, bodies of UNQUOTED heredocs).

    A heredoc body is data, not commands: a commit message or progress note
    that says "`sleep 30; gh pr checks 9`" is not a poll. Only an unquoted
    delimiter still expands `$( )` / backticks inside the body, so those
    bodies are returned for the substitution scan alone.
    """
    lines = command.split("\n")
    shell: list[str] = []
    live: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        shell.append(line)
        i += 1
        for m in _HEREDOC_OP_RE.finditer(line):
            quoted, delim = bool(m.group(1)), m.group(2)
            body: list[str] = []
            while i < len(lines) and lines[i].strip() != delim:
                body.append(lines[i])
                i += 1
            i += 1  # the terminator line (or past the end when unterminated)
            if not quoted:
                live.append("\n".join(body))
    return "\n".join(shell), live


def _mask_single_quotes(s: str) -> str:
    """`s` with single-quoted text blanked, so a backtick or `$(` inside
    'literal text' is not mistaken for a command substitution."""
    out: list[str] = []
    in_single = in_double = False
    i = 0
    while i < len(s):
        ch = s[i]
        if in_single:
            if ch == "'":
                in_single = False
                out.append(ch)
            else:
                out.append(" ")
        elif ch == "\\" and i + 1 < len(s):
            out.append(s[i : i + 2])
            i += 2
            continue
        elif ch == '"':
            in_double = not in_double
            out.append(ch)
        elif ch == "'" and not in_double:
            in_single = True
            out.append(ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _check_ci_poll(command: str, background: bool) -> str | None:
    if background:
        return None
    has_sleep = has_snapshot = has_unit_read = acts_on_unit = in_loop = False
    longest_sleep = 0.0
    shell_text, live_bodies = _split_heredocs(command)
    segments = _pipeline_subcommands(shell_text)
    # `s=$(gh pr checks 9 | grep -c pending)` inside a loop is the same poll.
    subst_sources = [_mask_single_quotes(shell_text), *live_bodies]
    for src in subst_sources:
        for body in _command_substitutions(src):
            segments.extend(_pipeline_subcommands(body))
    for seg in segments:
        try:
            raw = shlex.split(seg)
        except ValueError:
            raw = seg.split()
        if raw and raw[0] in _LOOP_KEYWORDS:
            in_loop = True
        toks = _segment_tokens(seg)
        if not toks:
            continue
        verb = _systemctl_verb(toks)
        if verb in _UNIT_STATUS_VERBS:
            has_unit_read = True
            continue
        if verb in _UNIT_ACTION_VERBS:
            acts_on_unit = True
            continue
        if toks[0] == "sleep":
            has_sleep = True
            longest_sleep = max(longest_sleep, _sleep_seconds(toks))
        elif toks[:3] == ["gh", "run", "watch"] or (toks[:1] == ["gh"] and "--watch" in toks):
            # The blocking primitive is in the command; a snapshot after it
            # just prints the settled result.
            return None
        elif _is_ci_snapshot(toks):
            has_snapshot = True
    if has_sleep and has_unit_read and not acts_on_unit and (
        in_loop or longest_sleep > _UNIT_SETTLE_S
    ):
        has_snapshot = True
    return _CI_POLL_HINT if has_sleep and has_snapshot else None


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
        reason = _check_bg_status_masked(
            command, tool_input.get("run_in_background") is True
        )
    if reason is None:
        reason = _check_nixos_push(command, cwd if isinstance(cwd, str) else None)
        if reason is not None:
            reason = "bash-guard: " + reason
    if reason is None:
        reason = _check_ci_poll(command, tool_input.get("run_in_background") is True)

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
