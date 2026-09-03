#!/usr/bin/env python3
"""Tests for ``hooks/bash-output-guard.py`` — the PostToolUse output guard.

Covers sections F (secret redaction) and G (untrusted-ingress wrapping) of
the safe-bash parity inventory. ``tests/test_bash_guards.py`` already holds
the first pass at both; this module closes the rows that pass had left
untested or wrong:

  * F10 — the guard inspected two named fields. safe-bash redacted one
    composed string, so every byte it returned passed the redactor. The
    field set the Bash tool actually returns is enumerated below from
    10,027 real results in ``~/.claude/projects/*/*.jsonl`` (CLI 2.1.222),
    and the guard now scans all of it, recursively, plus the overflow file
    that ``persistedOutputPath`` points at.
  * F11 — redaction versus truncation ordering.
  * F12 — an unhandled exception inside the redactor.
  * G3/G4 — every entry of the ingress vocabulary, driven off the config
    file rather than a hand-kept list, so a new entry cannot arrive
    untested.
  * G6–G9 — the helper branches (heredoc stripping, the token cap and the
    shlex ValueError path, basenamed heads, the attribute truncation
    boundary).
  * G11 — the port emitted TWO ``<untrusted_external_content>`` blocks for
    a command with both stdout and stderr where safe-bash emitted one. A
    doubled wrapper trains a reader to ignore the marker.
  * G12 — image-carrying results.

FAIL-CLOSED, output-side: a PostToolUse hook cannot deny, so the
equivalent of denying is WITHHOLDING the output rather than passing
uninspected bytes to the model. Every path that can stop the guard doing
its job has a row here.

There is no pytest on this host, so this uses the ``check()``/``section()``
idiom of the suite it sits beside.

Run:  python3 tests/test_bash_guard_post.py
Exit: 0 = all green, 1 = at least one failure.

Every secret-shaped fixture is assembled by concatenation at runtime.
Written as a literal it would be a guaranteed gitleaks false positive on
every commit that touches this file. No fixture is a real credential.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
HOOKS = os.path.join(REPO_ROOT, "hooks")
GUARD = os.path.join(HOOKS, "bash-output-guard.py")
INGRESS_CONFIG = os.path.join(HOOKS, "bash-guard-config.json")

FAILURES: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    mark = "OK " if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f" :: {detail}" if detail else ""))
    if not cond:
        FAILURES.append(label)


def section(name: str) -> None:
    print(f"\n== {name} ==")


SCRATCH = tempfile.mkdtemp(prefix="bash-output-guard-test-")

_spec = importlib.util.spec_from_file_location("bash_output_guard_under_test", GUARD)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

with open(GUARD, encoding="utf-8") as fh:
    GUARD_SRC = fh.read()

with open(INGRESS_CONFIG, encoding="utf-8") as fh:
    CONFIG = json.load(fh)

WITHHELD_MARKER = "the output guard is BROKEN"


# ----------------------------------------------------------------------
# THE SHAPE THE CLI WILL ACTUALLY APPLY
#
# `updatedToolOutput` is not handed to the model as written. The CLI parses
# it against the TOOL'S OWN output schema first, and on a mismatch it throws
# the rewrite away and uses the ORIGINAL result (2.1.251 bundle, offset
# 182502178, in the main tool-use path):
#
#     let rr = e.outputSchema?.safeParse(Yn),
#         Er = (po) => { n(`PostToolUse hook returned updatedToolOutput that
#                          does not match ${e.name}'s output shape: ${po}`,
#                          {level:"error"}),
#                        Yn = _n.data,          // <- the ORIGINAL, uninspected
#                        Dr = !0; … };
#     if (rr && !rr.success) Er(rr.error.message);
#
# For a redaction guard that is fail-OPEN: the rewrite is dropped and the
# unredacted bytes reach the model anyway. An error is surfaced, which makes
# it loud, but loud is not closed.
#
# MEASURED end-to-end against the shipped 2.1.251 CLI driving the native
# Bash tool, with a PostToolUse hook replacing the output of
# `echo ORIGINALCANARY9184` with a fixed marker:
#
#   emitted updatedToolOutput                        the model received
#   ----------------------------------------------   ---------------------
#   "REWRITTEN_BY_HOOK_5521"          (bare string)  ORIGINALCANARY9184
#   {"stdout":…, "stderr":""}                        ORIGINALCANARY9184
#   {"stdout":…, "stderr":"", "interrupted":false}   REWRITTEN_BY_HOOK_5521
#   {…, "interrupted":false, "someFutureField":"x"}  REWRITTEN_BY_HOOK_5521
#
# Two things fall out of that table and both are load-bearing here:
#   * `interrupted` is REQUIRED. An object carrying only stdout and stderr
#     is rejected exactly like a bare string.
#   * unknown keys are tolerated, so preserving a field this guard has never
#     heard of does not cost the rewrite.
#
# The gate string is byte-identical in 2.1.247 and 2.1.248, so this is not a
# 2.1.251 regression — a non-conforming rewrite has never been applied.
#
# The map below is transcribed from the CLI's own Bash `outputSchema` in the
# 2.1.251 bundle (offset 183851760): required = no `.optional()`.
# ----------------------------------------------------------------------
REQUIRED_RESULT_FIELDS = {"stdout": str, "stderr": str, "interrupted": bool}

OPTIONAL_RESULT_FIELDS = {
    "rawOutputPath": str,
    "isImage": bool,
    "backgroundTaskId": str,
    "backgroundedByUser": bool,
    "backgroundedByTurnAbort": bool,
    "backgroundedToDeliverMessage": bool,
    "timedOutAfterMs": (int, float),
    "backgroundCwdHint": str,
    "backgroundEndsWithFinalResponse": bool,
    "dangerouslyDisableSandbox": bool,
    "returnCodeInterpretation": str,
    "noOutputExpected": bool,
    "structuredContent": list,
    "persistedOutputPath": str,
    "persistedOutputSize": (int, float),
    "staleReadFileStateHint": str,
    "ghRateLimitHint": str,
    "gitOperation": dict,
}


def _typed(value, expected) -> bool:
    # zod tells a boolean from a number; Python's isinstance does not.
    if expected is bool:
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    return isinstance(value, expected)


def schema_rejection(updated) -> str | None:
    """Why the CLI would REJECT this rewrite, or ``None`` if it applies.

    A rejected rewrite is not a no-op: the CLI falls back to the ORIGINAL,
    un-inspected tool result. So "the guard emitted something" is never the
    question worth asking — "would the emitted value survive the consumer"
    is. That distinction is the entire bug this section exists for.
    """
    if not isinstance(updated, dict):
        return (
            f"a bare {type(updated).__name__} — the Bash outputSchema is an "
            "object, so this is discarded and the original output is used"
        )
    for key, typ in REQUIRED_RESULT_FIELDS.items():
        if key not in updated:
            return f"required field {key!r} is missing"
        if not _typed(updated[key], typ):
            got = type(updated[key]).__name__
            return f"required field {key!r} is a {got}, not {typ.__name__}"
    for key, typ in OPTIONAL_RESULT_FIELDS.items():
        if key in updated and updated[key] is not None and not _typed(updated[key], typ):
            got = type(updated[key]).__name__
            want = typ if isinstance(typ, type) else typ[0]
            return f"field {key!r} is a {got}, not {getattr(want, '__name__', want)}"
    return None


def check_applied(label: str, updated) -> None:
    """The emitted rewrite must be a value the CLI will actually apply."""
    why = schema_rejection(updated)
    check(
        f"{label} -> the rewrite is a value the CLI will APPLY",
        why is None,
        f"{why}; the original un-inspected output would reach the model instead",
    )


# Every rewrite this suite makes the guard emit, so no path can quietly grow
# a shape the consumer rejects. Filled by run_hook.
EMITTED: list[tuple[str, object]] = []


# ----------------------------------------------------------------------
# Harness
# ----------------------------------------------------------------------
def run_hook(
    script: str,
    payload,
    env_extra: dict | None = None,
    timeout: float = 60.0,
    raw_stdin: str | None = None,
) -> tuple[int, dict | None, str, str]:
    """Feed crafted hook JSON on stdin; return (rc, parsed_or_None, out, err)."""
    env = dict(os.environ)
    env.update(env_extra or {})
    stdin = raw_stdin if raw_stdin is not None else json.dumps(payload)
    proc = subprocess.run(
        [sys.executable, script],
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
    if isinstance(parsed, dict):
        hso = parsed.get("hookSpecificOutput")
        if isinstance(hso, dict) and "updatedToolOutput" in hso:
            EMITTED.append(
                (str(payload if raw_stdin is None else raw_stdin)[:120],
                 hso["updatedToolOutput"])
            )
    return proc.returncode, parsed, proc.stdout, proc.stderr


def post_payload(command: str, response, tool_name: str = "Bash") -> dict:
    return {
        "session_id": "test-session",
        "cwd": REPO_ROOT,
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command},
        "tool_response": response,
        "tool_use_id": "toolu_test",
    }


def bash_out(command: str, response, script: str = GUARD, **kw):
    """Run the guard; return (parsed, updatedToolOutput, raw_stdout)."""
    rc, parsed, out, err = run_hook(script, post_payload(command, response), **kw)
    updated = None
    if isinstance(parsed, dict):
        updated = (parsed.get("hookSpecificOutput") or {}).get("updatedToolOutput")
    return parsed, updated, out


def response_obj(stdout: str, stderr: str = "", **extra) -> dict:
    """The real Bash tool result shape (see FIELDS_SEEN below)."""
    return dict({"stdout": stdout, "stderr": stderr, "interrupted": False}, **extra)


def stdout_of(updated):
    return updated.get("stdout") if isinstance(updated, dict) else updated


def secret(tag: str = "A", n: int = 32) -> str:
    """A synthetic anthropic-api-shaped value. Never a real credential."""
    return "sk-" + "ant-" + "api03-" + (tag * n)


def patched_guard(name: str, *replacements: tuple[str, str]) -> str:
    """Write a fault-injected copy of the guard and return its path."""
    src = GUARD_SRC
    for old, new in replacements:
        if old not in src:
            raise AssertionError(f"fixture {name}: anchor not found: {old!r}")
        src = src.replace(old, new, 1)
    check(f"fault fixture {name} actually patched the source", src != GUARD_SRC)
    path = os.path.join(SCRATCH, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(src)
    return path


def check_withheld(label: str, parsed, updated, raw: str = "") -> None:
    body = stdout_of(updated)
    check(f"{label} -> output replaced", updated is not None, f"parsed={str(parsed)[:140]}")
    check(
        f"{label} -> replacement says the guard is broken",
        isinstance(body, str) and WITHHELD_MARKER in body,
        str(body)[:200],
    )
    check(
        f"{label} -> replacement tells the operator how to recover",
        isinstance(body, str) and "Recover:" in body and "! <command>" in body,
        str(body)[:200],
    )


# ----------------------------------------------------------------------
# THE FAIL-OPEN. Every emit site must survive the consumer.
#
# The guard had one job on these paths — fail CLOSED — and the one branch
# whose entire purpose is withholding emitted a bare string, which the CLI
# discards in favour of the original, un-inspected output. The original work
# proved `updatedToolOutput` EXISTS by reading the schema and never
# exercised what the consumer does with a bad value; a test that asserts
# only "a withhold happened" is the test that missed it, so every check here
# asserts the emitted value would actually be APPLIED.
# ----------------------------------------------------------------------
section("bash-output-guard: every rewrite must be one the CLI applies")

# The predicate itself, against the two values measured end-to-end above.
check(
    "schema_rejection rejects a bare string, as the CLI did",
    schema_rejection("just some text") is not None,
    str(schema_rejection("just some text")),
)
check(
    "schema_rejection rejects stdout+stderr with no `interrupted`, as the CLI did",
    schema_rejection({"stdout": "x", "stderr": ""}) is not None,
    str(schema_rejection({"stdout": "x", "stderr": ""})),
)
check(
    "schema_rejection accepts the shape the CLI applied",
    schema_rejection({"stdout": "x", "stderr": "", "interrupted": False}) is None,
)
check(
    "schema_rejection tolerates an unknown key, as the CLI did",
    schema_rejection(
        {"stdout": "x", "stderr": "", "interrupted": False, "someFutureField": "x"}
    )
    is None,
)

# _withhold's `_SHAPE is None` branch. Reached whenever the guard fails
# before it has read an object-shaped tool_response — a string result, a
# list result, unparseable stdin — which is most of the fail-closed ladder.
_s = secret("Q")
parsed, updated, raw = bash_out(
    "cat /run/agenix/some-key",
    f"KEY={_s}",
    env_extra={"BASH_GUARD_CONFIG": os.path.join(SCRATCH, "no-such-config.json")},
)
check_withheld("withhold with no object shape to copy", parsed, updated)
check_applied("withhold with no object shape to copy", updated)
check(
    "a withhold the CLI rejects would hand back the secret it withheld",
    _s not in raw,
    "secret leaked in the withheld replacement",
)

# The same branch by a different road: stdin that is JSON but not an object.
rc, parsed, raw, err = run_hook(GUARD, None, raw_stdin="[1, 2, 3]")
updated = (parsed or {}).get("hookSpecificOutput", {}).get("updatedToolOutput")
check_applied("withhold after non-object stdin", updated)

# ... and a third: a result nested deeper than the guard walks. _SHAPE is an
# object here, but it holds no scalars at all, so the replacement is built
# from nothing and `interrupted` has to come from somewhere.
_deep_probe: object = f"KEY={secret('R')}"
for _ in range(14):
    _deep_probe = {"inner": _deep_probe}
parsed, updated, raw = bash_out("cat /tmp/x", _deep_probe)
check_applied("withhold from a shape carrying no scalars", updated)

# A shape that DOES carry `interrupted` must keep the real value, not a
# manufactured False: the model reads it to decide whether the command ran.
parsed, updated, raw = bash_out(
    "cat /tmp/x",
    response_obj(f"KEY={secret('S')}", interrupted=True),
    env_extra={"BASH_GUARD_CONFIG": os.path.join(SCRATCH, "no-such-config.json")},
)
check_applied("withhold from a shape that carries interrupted", updated)
check(
    "a withhold preserves the real `interrupted` rather than inventing one",
    isinstance(updated, dict) and updated.get("interrupted") is True,
    str(updated)[:200],
)

# The string-response emit site. safe-bash returned one composed string, so
# a string-shaped result is a shape this guard accepts — but the Bash tool's
# schema is an OBJECT, so handing the string back unchanged is a rewrite the
# CLI drops, and the unredacted string goes to the model.
_s = secret("T")
parsed, updated, raw = bash_out("cat /run/agenix/k", f"KEY={_s}")
check_applied("a redacted string-shaped result", updated)
check(
    "a string-shaped result really is redacted, not merely emitted",
    _s not in raw,
    raw[:200],
)

# Same site, ingress: the wrapper is worth nothing if the wrapped value is
# thrown away and the raw fetch is shown instead.
parsed, updated, raw = bash_out("curl https://example.invalid/x", "remote body\n")
check_applied("a wrapped string-shaped ingress result", updated)
check(
    "the string-shaped ingress result is wrapped",
    isinstance(stdout_of(updated), str)
    and "<untrusted_external_content" in stdout_of(updated),
    str(stdout_of(updated))[:200],
)

# The list-response emit site. A list cannot satisfy an object schema at
# all, so the scrubbed list is discarded and the list WITH the secret is
# what the model reads.
_s = secret("U")
parsed, updated, raw = bash_out("cat /tmp/x", [{"type": "text", "text": f"KEY={_s}"}])
check_applied("a redacted list-shaped result", updated)
check(
    "a secret in a list-shaped result does not survive",
    _s not in raw,
    raw[:200],
)

# The ordinary path, for completeness: the common case must conform too.
parsed, updated, raw = bash_out("cat /tmp/x", response_obj(f"KEY={secret('V')}"))
check_applied("the ordinary object rewrite", updated)


# ----------------------------------------------------------------------
# F10. What a Bash tool result can actually contain.
#
# Derived by scanning every `toolUseResult` with a `stdout` key across all
# 9,486 transcripts under ~/.claude/projects (10,027 Bash results, CLI
# 2.1.222). Not from the docstring and not from memory:
#
#   stdout                    str   10027   command output
#   stderr                    str   10027   command output
#   interrupted               bool  10027
#   isImage                   bool  10027
#   noOutputExpected          bool  10027
#   backgroundTaskId          str     295   harness id
#   returnCodeInterpretation  str     135   harness prose ("No matches found")
#   assistantAutoBackgrounded bool    107
#   persistedOutputPath       str      76   FILE holding the untruncated output
#   persistedOutputSize       int      76
#   dangerouslyDisableSandbox bool     68
# ----------------------------------------------------------------------
section("bash-output-guard: the Bash result field inventory (F10)")

# A transcript scan can only see fields a past result happened to carry, and
# a field the harness sets on a rare condition never shows up in one. Six of
# these are invisible to the scan above and every one of them was
# unclassified, which withheld the output of ordinary ingress commands —
# `ghRateLimitHint` rides along with `gh api`, which is ingress by
# definition. The set below is the CLI's own Bash `outputSchema` instead
# (2.1.251 bundle, offset 183851760), so a field can no longer hide behind
# never having been logged.
FIELDS_SEEN_STR = {
    "stdout", "stderr", "backgroundTaskId", "returnCodeInterpretation",
    "persistedOutputPath",
    # string-typed, in the schema, absent from 10,027 sampled results
    "rawOutputPath", "backgroundCwdHint", "staleReadFileStateHint",
    "ghRateLimitHint",
}

classified = set(getattr(guard, "OUTPUT_FIELDS", ())) | set(
    getattr(guard, "METADATA_STRING_FIELDS", ())
)
check(
    "every string-typed field the Bash tool really returns is classified",
    FIELDS_SEEN_STR <= classified,
    f"unclassified: {sorted(FIELDS_SEEN_STR - classified)}",
)

# The non-string ones the walk still has to have an answer for.
check(
    "gitOperation is classified as never reaching the model",
    "gitOperation" in getattr(guard, "NON_MODEL_FACING_FIELDS", frozenset()),
)
check(
    "structuredContent is classified as content-REPLACING, not content-adjacent",
    getattr(guard, "CONTENT_REPLACING_FIELD", None) == "structuredContent",
)

# The guard and this file each transcribe the CLI's Bash outputSchema. They
# are independent copies on purpose — the test must be able to fail the
# guard — so they are compared here rather than shared, and a drift in
# either one is loud.
check(
    "the guard's required-field table matches this file's transcription",
    getattr(guard, "REQUIRED_RESULT_FIELDS", None) == REQUIRED_RESULT_FIELDS,
    str(getattr(guard, "REQUIRED_RESULT_FIELDS", None)),
)
check(
    "the guard's optional-field table matches this file's transcription",
    getattr(guard, "OPTIONAL_RESULT_FIELDS", None) == OPTIONAL_RESULT_FIELDS,
    "differs: "
    + str(
        set(getattr(guard, "OPTIONAL_RESULT_FIELDS", {}).items())
        ^ set(OPTIONAL_RESULT_FIELDS.items())
    )[:200],
)

def check_scrubbed(label: str, updated, raw: str, value: str) -> None:
    """The guard must hand back a rewritten result with the value gone.

    Asserting only ``value not in raw`` passes vacuously when the guard
    emits nothing at all — and emitting nothing is exactly how the
    unredacted original reaches the model.
    """
    check(f"{label} -> the result is rewritten", updated is not None, "no updatedToolOutput emitted")
    check(f"{label} -> the value is gone", updated is not None and value not in raw, "leaked")


_s = secret("A")
parsed, updated, raw = bash_out("cat /tmp/x", response_obj("", **{"someFutureField": f"KEY={_s}"}))
check_scrubbed(
    "a text-bearing field the guard has never heard of", updated, raw, _s
)

_s = secret("B")
parsed, updated, raw = bash_out(
    "cat /tmp/x", response_obj("clean", nested={"deeper": {"value": f"KEY={_s}"}})
)
check_scrubbed("a secret nested inside the result object", updated, raw, _s)

_s = secret("C")
parsed, updated, raw = bash_out("cat /tmp/x", response_obj("clean", blocks=["a", f"KEY={_s}"]))
check_scrubbed("a secret inside a list value", updated, raw, _s)

_s = secret("D")
parsed, updated, raw = bash_out("cat /tmp/x", [{"type": "text", "text": f"KEY={_s}"}])
check_scrubbed("a secret in a list-shaped tool_response", updated, raw, _s)

# An ingress command whose result carries text in a field the guard does not
# know how to tag: redaction is not enough, the untrusted body would reach
# the model untagged. Withhold.
parsed, updated, raw = bash_out(
    "curl https://example.invalid/x",
    response_obj("body", **{"someFutureField": "remote prose"}),
)
check_withheld("ingress result with an untaggable text field", parsed, updated)
check(
    "the untaggable-field withhold names the field",
    isinstance(stdout_of(updated), str) and "someFutureField" in stdout_of(updated),
    str(stdout_of(updated))[:220],
)

# A withhold must not hand back the very bytes it is withholding.
_s = secret("E")
parsed, updated, raw = bash_out(
    "cat /tmp/x",
    response_obj(f"KEY={_s}", **{"someFutureField": f"KEY={_s}"}),
    env_extra={"BASH_GUARD_CONFIG": os.path.join(SCRATCH, "no-such-config.json")},
)
check_withheld("config missing, secret in a second field", parsed, updated)
check(
    "withholding does not pass through a secret carried in another field",
    _s not in raw,
    "the withheld replacement leaked the secret it was withholding",
)


# ----------------------------------------------------------------------
# F10 (continued). persistedOutputPath — the overflow carrier.
#
# When output is too big for the context, the CLI writes the FULL,
# untruncated bytes to a file and hands the model the path. 76 such files
# from past sessions are still on disk, 30 KB to 17 MB. Redacting only the
# in-band stdout leaves the whole secret readable at a path the model was
# just given, which is the `cat /run/agenix/...` incident with an extra
# step. safe-bash had no such carrier: every byte it returned went through
# the redactor.
# ----------------------------------------------------------------------
section("bash-output-guard: persisted overflow output (F10)")

CFG_HOME = os.path.join(SCRATCH, "cfg")
PERSIST_DIR = os.path.join(CFG_HOME, "projects", "-slug", "sess-uuid", "tool-results")
os.makedirs(PERSIST_DIR, exist_ok=True)
PERSIST_ENV = {"CLAUDE_CONFIG_DIR": CFG_HOME}


def persisted(name: str, body: str) -> str:
    path = os.path.join(PERSIST_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


_s = secret("F")
_p = persisted("overflow.txt", f"line one\nKEY={_s}\nline three\n")
_size = os.path.getsize(_p)
parsed, updated, raw = bash_out(
    "cat /run/agenix/some-key",
    response_obj("line one\n", persistedOutputPath=_p, persistedOutputSize=_size),
    env_extra=PERSIST_ENV,
)
with open(_p, encoding="utf-8") as fh:
    _after = fh.read()
check("the persisted overflow file is redacted in place", _s not in _after, _after[:120])
check(
    "the persisted overflow file keeps a redaction marker",
    "[REDACTED:anthropic-api:" in _after,
    _after[:160],
)
check(
    "a scrubbed persisted file keeps its pointer",
    isinstance(updated, dict) and updated.get("persistedOutputPath") == _p
    if updated is not None
    else False,
    str(updated)[:200],
)
check(
    "persistedOutputSize is re-stated after the rewrite",
    isinstance(updated, dict) and updated.get("persistedOutputSize") == len(_after.encode()),
    str(updated)[:200],
)

# Clean overflow file: nothing to do, and the guard must not churn the result.
_p_clean = persisted("clean.txt", "nothing interesting here\n")
parsed, updated, raw = bash_out(
    "ls -R /",
    response_obj("nothing interesting here\n", persistedOutputPath=_p_clean,
                 persistedOutputSize=os.path.getsize(_p_clean)),
    env_extra=PERSIST_ENV,
)
check("a clean persisted file leaves the result untouched", raw.strip() == "", raw[:160])

# Unreadable: the guard cannot vouch for those bytes, so the model does not
# get the path.
_p_bad = persisted("unreadable.txt", f"KEY={secret('G')}\n")
os.chmod(_p_bad, 0)
parsed, updated, raw = bash_out(
    "cat /tmp/big",
    response_obj("head\n", persistedOutputPath=_p_bad, persistedOutputSize=10),
    env_extra=PERSIST_ENV,
)
check(
    "an unreadable persisted file has its pointer withheld",
    isinstance(updated, dict) and "persistedOutputPath" not in updated,
    str(updated)[:220],
)
check(
    "the dropped pointer is explained in the output",
    isinstance(stdout_of(updated), str) and "bash-output-guard" in stdout_of(updated),
    str(stdout_of(updated))[:220],
)
os.chmod(_p_bad, 0o600)

# Oversized: scanning 17 MiB costs ~7 s, past the guard's own 6 s deadline.
# Bigger than the scan limit means the pointer goes, not that the guard
# blows its deadline and gets its output discarded by the harness.
_scan_limit = getattr(guard, "PERSISTED_SCAN_LIMIT_BYTES", 4 * 1024 * 1024)
_p_big = persisted("oversized.txt", "x" * (_scan_limit + 1))
parsed, updated, raw = bash_out(
    "cat /tmp/huge",
    response_obj("head\n", persistedOutputPath=_p_big,
                 persistedOutputSize=os.path.getsize(_p_big)),
    env_extra=PERSIST_ENV,
)
check(
    "an oversized persisted file has its pointer withheld",
    isinstance(updated, dict) and "persistedOutputPath" not in updated,
    str(updated)[:220],
)

# The scan limit exists to keep the guard inside its own deadline: a
# harness-level timeout DISCARDS the hook's output and rewrites nothing,
# which is fail-open. A file right at the limit must still land in time.
_filler = ("path/to/file-%04d some prose and a hash 0a1b2c3d4e5f\n" % 0) * 512
_big_body = (_filler * ((_scan_limit // len(_filler)) + 1))[: _scan_limit - 64]
_p_limit = persisted("at-the-limit.txt", _big_body + f"\nKEY={secret('P')}\n")
_t0 = time.monotonic()
parsed, updated, raw = bash_out(
    "cat /tmp/big",
    response_obj("head\n", persistedOutputPath=_p_limit,
                 persistedOutputSize=os.path.getsize(_p_limit)),
    env_extra=PERSIST_ENV,
)
_elapsed = time.monotonic() - _t0
check(
    "a persisted file at the scan limit is still scrubbed",
    "[REDACTED:anthropic-api:" in open(_p_limit, encoding="utf-8").read(),
    "the limit is set above what the guard can actually scan",
)
check(
    "scrubbing a limit-sized file finishes inside the harness hook timeout",
    _elapsed < guard.HARNESS_HOOK_TIMEOUT_S,
    f"{_elapsed:.1f}s vs harness {guard.HARNESS_HOOK_TIMEOUT_S}s",
)

# The path comes from the harness, but the guard must not become a write
# primitive for anything outside the CLI's own tool-results tree.
_outside = os.path.join(SCRATCH, "not-a-tool-result.txt")
with open(_outside, "w", encoding="utf-8") as fh:
    fh.write(f"KEY={secret('H')}\n")
_before = open(_outside, encoding="utf-8").read()
parsed, updated, raw = bash_out(
    "cat /tmp/x",
    response_obj("head\n", persistedOutputPath=_outside, persistedOutputSize=10),
    env_extra=PERSIST_ENV,
)
check(
    "a persisted path outside the CLI tool-results tree is not written to",
    open(_outside, encoding="utf-8").read() == _before,
    "the guard rewrote a file the harness did not own",
)
check(
    "a persisted path outside the tool-results tree has its pointer withheld",
    isinstance(updated, dict) and "persistedOutputPath" not in updated,
    str(updated)[:220],
)


# ----------------------------------------------------------------------
# F13. The fields the walk had never heard of.
#
# Every one of these is in the CLI's Bash outputSchema in 2.1.247, 2.1.248
# AND 2.1.251 (`backgroundedToDeliverMessage` excepted — that one really is
# new in .251), so none of it is a fresh regression; the guard simply never
# knew the names. Unclassified, each forced a WITHHOLD the moment the
# command was untrusted ingress, and `ghRateLimitHint` arrives on `gh`
# calls, which are ingress by definition. Routine output was being held back.
#
# Each disposition below is measured against the builder, not inferred from
# the field name: a hint that interpolated the command's own bytes would
# need the ingress tag, not a metadata classification.
# ----------------------------------------------------------------------
section("bash-output-guard: result fields the walk had never heard of (F13)")

INGRESS_CMD = "gh api /rate_limit"

# The three model-facing hints. All harness prose (builders read at 2.1.251
# offsets 181248504 and 183866300); none carries command bytes.
HARNESS_HINTS = [
    (
        "ghRateLimitHint",
        "<system-reminder>GitHub API rate limit exceeded (5,000/hr shared across "
        "all tools and agents).</system-reminder>",
    ),
    (
        "staleReadFileStateHint",
        "[This command modified 2 files you've previously read: a.py, b.py. "
        "Call Read before editing.]",
    ),
    (
        "backgroundCwdHint",
        "Session cwd remains /home/u/repo; directory changes made by the "
        "backgrounded command do not apply to subsequent commands.",
    ),
]

for _field, _value in HARNESS_HINTS:
    parsed, updated, raw = bash_out(
        INGRESS_CMD, response_obj("remote body\n", **{_field: _value})
    )
    _body = stdout_of(updated)
    check(
        f"an ingress result carrying {_field} is NOT withheld",
        isinstance(_body, str) and WITHHELD_MARKER not in _body,
        str(_body)[:200],
    )
    check(
        f"... and its output is still wrapped as untrusted ingress ({_field})",
        isinstance(_body, str) and 'source="bash:gh-api"' in _body,
        str(_body)[:200],
    )
    check(
        f"... and {_field} survives the rewrite",
        isinstance(updated, dict) and updated.get(_field) == _value,
        str(updated)[:240],
    )

# The harness hints are still redacted — everything is — even though they
# need no tag.
_s = secret("W")
parsed, updated, raw = bash_out(
    "cat /tmp/x", response_obj("clean\n", ghRateLimitHint=f"note {_s}")
)
check_scrubbed("a secret planted in a harness hint field", updated, raw, _s)

# gitOperation: a structured object the Bash mapper never renders. Nothing
# there can reach the model, so it needs no tag — but it must not withhold.
_git_op = {"kind": "push", "branch": "feat/x", "remote": "origin"}
parsed, updated, raw = bash_out(
    INGRESS_CMD, response_obj("remote body\n", gitOperation=_git_op)
)
_body = stdout_of(updated)
check(
    "an ingress result carrying gitOperation is NOT withheld",
    isinstance(_body, str) and WITHHELD_MARKER not in _body,
    str(_body)[:200],
)
check(
    "... and gitOperation survives the rewrite intact",
    isinstance(updated, dict) and updated.get("gitOperation") == _git_op,
    str(updated)[:240],
)
_s = secret("X")
parsed, updated, raw = bash_out(
    "git push", response_obj("clean\n", gitOperation={"kind": "push", "note": _s})
)
check_scrubbed("a secret nested inside gitOperation", updated, raw, _s)

# backgroundedToDeliverMessage: genuinely new in 2.1.251 (0 occurrences in
# the 2.1.247 and 2.1.248 bundles, 17 in .251). A boolean the harness wrote:
# no bytes, nothing to tag, nothing to redact. What it needs is to survive
# both a rewrite and a withhold, and to never be mistaken for text.
parsed, updated, raw = bash_out(
    INGRESS_CMD,
    response_obj("remote body\n", backgroundTaskId="bg_1",
                 backgroundedToDeliverMessage=True),
)
check(
    "an ingress result carrying backgroundedToDeliverMessage is NOT withheld",
    isinstance(stdout_of(updated), str) and WITHHELD_MARKER not in stdout_of(updated),
    str(stdout_of(updated))[:200],
)
check(
    "... and the flag survives the rewrite",
    isinstance(updated, dict) and updated.get("backgroundedToDeliverMessage") is True,
    str(updated)[:240],
)
parsed, updated, raw = bash_out(
    "cat /tmp/x",
    response_obj(f"KEY={secret('Y')}", backgroundedToDeliverMessage=True),
    env_extra={"BASH_GUARD_CONFIG": os.path.join(SCRATCH, "no-such-config.json")},
)
check(
    "... and it survives a withhold, which keeps scalars so the result renders",
    isinstance(updated, dict) and updated.get("backgroundedToDeliverMessage") is True,
    str(updated)[:240],
)

# rawOutputPath. In the schema and in the harness's own path-remap table
# (`Bash:{responseMembers:["rawOutputPath","persistedOutputPath"]}`), and
# scrubbed by NOBODY: the guard redacted the path STRING and handed the
# model a pointer to a file full of unredacted bytes.
_s = secret("Z")
_raw_p = persisted("raw-output.txt", f"header\nKEY={_s}\ntail\n")
parsed, updated, raw = bash_out(
    "cat /run/agenix/some-key",
    # The in-band body carries the secret too, so a rewrite is forced and the
    # pointer's survival is actually observable. rawOutputPath has no size
    # field to re-state, so a clean stdout would mean no rewrite at all and
    # the assertion below would have nothing to look at.
    response_obj(f"header\nKEY={_s}\n", rawOutputPath=_raw_p),
    env_extra=PERSIST_ENV,
)
with open(_raw_p, encoding="utf-8") as fh:
    _raw_after = fh.read()
check(
    "the file behind rawOutputPath is redacted in place",
    _s not in _raw_after,
    _raw_after[:160],
)
check(
    "... and keeps a redaction marker",
    "[REDACTED:anthropic-api:" in _raw_after,
    _raw_after[:160],
)
check(
    "a scrubbed rawOutputPath keeps its pointer",
    isinstance(updated, dict) and updated.get("rawOutputPath") == _raw_p,
    str(updated)[:240],
)

_raw_outside = os.path.join(SCRATCH, "raw-not-a-tool-result.txt")
with open(_raw_outside, "w", encoding="utf-8") as fh:
    fh.write(f"KEY={secret('a')}\n")
_raw_before = open(_raw_outside, encoding="utf-8").read()
parsed, updated, raw = bash_out(
    "cat /tmp/x",
    response_obj("head\n", rawOutputPath=_raw_outside),
    env_extra=PERSIST_ENV,
)
check(
    "a rawOutputPath outside the CLI tool-results tree is not written to",
    open(_raw_outside, encoding="utf-8").read() == _raw_before,
    "the guard rewrote a file the harness did not own",
)
check(
    "... and has its pointer withheld",
    isinstance(updated, dict) and "rawOutputPath" not in updated,
    str(updated)[:240],
)
check(
    "... and the dropped pointer is explained, naming the field",
    isinstance(stdout_of(updated), str) and "rawOutputPath" in stdout_of(updated),
    str(stdout_of(updated))[:280],
)

# Both pointer fields at once: neither may shadow the other's notice.
_p_both = persisted("both-persisted.txt", f"KEY={secret('b')}\n")
_r_both = os.path.join(SCRATCH, "both-raw-outside.txt")
with open(_r_both, "w", encoding="utf-8") as fh:
    fh.write("no secret here\n")
parsed, updated, raw = bash_out(
    "cat /tmp/x",
    response_obj("head\n", persistedOutputPath=_p_both,
                 persistedOutputSize=os.path.getsize(_p_both),
                 rawOutputPath=_r_both),
    env_extra=PERSIST_ENV,
)
check(
    "a scrubbable persistedOutputPath keeps its pointer while rawOutputPath drops",
    isinstance(updated, dict)
    and updated.get("persistedOutputPath") == _p_both
    and "rawOutputPath" not in updated,
    str(updated)[:280],
)

# structuredContent. Not a redaction hole — `_redact_tree` walks it like
# everything else — but a TAGGING hole: the Bash mapper's first line is
# `if (O && O.length > 0) return {..., content: O}`, so it REPLACES the
# whole result and any wrapper on stdout is never rendered. Teaching the
# walk to tag stdout cannot close that, so it stays fail-closed, with a
# reason that says which of the two problems it actually is.
_s = secret("c")
parsed, updated, raw = bash_out(
    "cat /tmp/x",
    response_obj("clean\n", structuredContent=[{"type": "text", "text": f"KEY={_s}"}]),
)
check_scrubbed("a secret inside structuredContent", updated, raw, _s)
check(
    "a non-ingress result carrying structuredContent is not withheld",
    isinstance(stdout_of(updated), str) and WITHHELD_MARKER not in stdout_of(updated),
    str(stdout_of(updated))[:200],
)

parsed, updated, raw = bash_out(
    INGRESS_CMD,
    response_obj("remote body\n",
                 structuredContent=[{"type": "text", "text": "remote prose"}]),
)
check_withheld("ingress result carrying structuredContent", parsed, updated)
check(
    "the structuredContent withhold says it REPLACES the result, not that it is untagged",
    isinstance(stdout_of(updated), str)
    and "structuredContent" in stdout_of(updated)
    and "REPLACES" in stdout_of(updated),
    str(stdout_of(updated))[:320],
)
parsed, updated, raw = bash_out(
    INGRESS_CMD, response_obj("remote body\n", structuredContent=[])
)
check(
    "an EMPTY structuredContent replaces nothing, so it does not withhold",
    isinstance(stdout_of(updated), str) and WITHHELD_MARKER not in stdout_of(updated),
    str(stdout_of(updated))[:200],
)

# The whole set at once, on the command that actually triggers them: a
# routine `gh api` call whose result carries every field the walk learned.
parsed, updated, raw = bash_out(
    INGRESS_CMD,
    response_obj(
        "remote body\n",
        ghRateLimitHint="<system-reminder>rate limited</system-reminder>",
        staleReadFileStateHint="[This command modified 1 file…]",
        backgroundCwdHint="Session cwd remains /x;…",
        gitOperation={"kind": "pr_view"},
        backgroundedToDeliverMessage=False,
        noOutputExpected=False,
    ),
)
check(
    "a routine gh api result carrying every learned field is not withheld",
    isinstance(stdout_of(updated), str) and WITHHELD_MARKER not in stdout_of(updated),
    str(stdout_of(updated))[:240],
)
check(
    "... and is still wrapped as untrusted ingress",
    isinstance(stdout_of(updated), str)
    and 'source="bash:gh-api"' in stdout_of(updated),
    str(stdout_of(updated))[:240],
)


# ----------------------------------------------------------------------
# F14. Redaction, proven end to end.
#
# The pattern set was compared against safe-bash's and found byte-identical,
# nine patterns each side — and that is a statement about two lists, not
# about a pipeline. Nothing had ever put a secret through THIS hook and
# looked at what came out. Every pattern here is planted in the real stdout
# of a real command and asserted gone from what the guard emits, driven off
# the guard's own pattern list so a tenth pattern cannot arrive untested.
#
# No fixture is a real credential; each is assembled by concatenation at
# runtime so the file is not a standing gitleaks hit.
# ----------------------------------------------------------------------
section("bash-output-guard: every redaction pattern fires end-to-end (F14)")

E2E_SECRETS = {
    "anthropic-api": "sk-" + "ant-" + "api03-" + ("A" * 95),
    "openai-project": "sk-" + "proj-" + ("B" * 48),
    "openai-legacy": "sk-" + ("C" * 48),
    "github-token": "ghp" + "_" + ("D" * 36),
    "github-pat": "github" + "_pat_" + ("E" * 82),
    "slack-token": "xoxb" + "-" + "1234567890-0987654321-abcdefghij",
    "aws-access": "AKIA" + "IOSFODNN7EXAMPLE",
    "jwt": (
        "eyJ" + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        + "." + "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkEifQ"
        + "." + "dBjftJeZ4CVP" + "mB92K27uhbUJU1p1r-wW1gFWFOEjXk"
    ),
    "tavily-key": "tvly" + "-" + ("F" * 28),
}

check(
    "every configured redaction pattern has an end-to-end fixture",
    {label for label, _ in guard._SECRET_PATTERNS} == set(E2E_SECRETS),
    "untested: "
    + str(sorted({label for label, _ in guard._SECRET_PATTERNS} - set(E2E_SECRETS))),
)

# Private keys are NOT in _SECRET_PATTERNS — they are a marker scan, because a
# BEGIN..END regex was quadratic (F12). The equality check above therefore
# cannot see them, so the second mechanism gets its own coverage rather than
# quietly falling outside the drift guard that exists to stop exactly that.
check(
    "the private-key scanner is wired into the redactor",
    "_redact_private_keys" in guard._redact_secrets.__code__.co_names,
    "keys are a separate mechanism; if it is not called, nothing redacts them",
)
for _marker_re, _which in ((guard._KEY_BEGIN_RE, "BEGIN"), (guard._KEY_END_RE, "END")):
    check(
        f"the {_which} marker regex spans no body",
        r"[\s\S]" not in _marker_re.pattern,
        f"a body-spanning quantifier here is what made the old version "
        f"quadratic: {_marker_re.pattern}",
    )

E2E_DIR = os.path.join(SCRATCH, "e2e-secrets")
os.makedirs(E2E_DIR, exist_ok=True)


def real_command_output(label: str, value: str) -> tuple[str, str]:
    """Actually run a command whose output carries the secret.

    Hand-built strings would prove the regex compiles. Running `cat` over a
    file on disk and taking its real stdout proves the value survives the
    round trip a real command makes — encoding, trailing newline and all.
    """
    path = os.path.join(E2E_DIR, f"{label}.env")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            f"# fixture for {label}\n"
            f"export {label.upper().replace('-', '_')}={value}\n"
            "OTHER=not-a-secret\n"
        )
    proc = subprocess.run(["cat", path], capture_output=True, text=True, timeout=30)
    return proc.stdout, proc.stderr


_missed: list[str] = []
for _label, _value in sorted(E2E_SECRETS.items()):
    _out, _err = real_command_output(_label, _value)
    check(
        f"the {_label} fixture really is in the command's own stdout",
        _value in _out,
        "the fixture never reached the pipeline, so the next check is vacuous",
    )
    parsed, updated, raw = bash_out(f"cat {E2E_DIR}/{_label}.env",
                                    response_obj(_out, stderr=_err))
    _body = stdout_of(updated)
    check(
        f"end-to-end: a real {_label} secret does not survive the guard",
        updated is not None and _value not in raw,
        "PATTERN DID NOT FIRE IN THE REAL PIPELINE" if updated is None
        else str(_body)[:160],
    )
    check(
        f"end-to-end: the {_label} redaction is marked, not silently dropped",
        isinstance(_body, str) and f"[REDACTED:{_label}:" in _body,
        str(_body)[:200],
    )
    if updated is None or _value in raw:
        _missed.append(_label)

check(
    "no configured pattern fails to fire in the real pipeline",
    not _missed,
    f"patterns present in the set but inert end-to-end: {_missed}",
)

# The same nine, through the overflow file rather than in band: the pointer
# path has only ever been exercised with one pattern.
for _label, _value in sorted(E2E_SECRETS.items()):
    _p = persisted(f"e2e-{_label}.txt", f"line one\nKEY={_value}\nline three\n")
    parsed, updated, raw = bash_out(
        "cat /run/agenix/k",
        response_obj("line one\n", persistedOutputPath=_p,
                     persistedOutputSize=os.path.getsize(_p)),
        env_extra=PERSIST_ENV,
    )
    check(
        f"end-to-end: a real {_label} secret in the overflow file is scrubbed",
        _value not in open(_p, encoding="utf-8").read(),
        open(_p, encoding="utf-8").read()[:160],
    )

# Private keys end-to-end, by the same method as the loop above: a real file,
# a real `cat`, its real stdout. Every armor spelling the scanner claims.
for _kind in ("OPENSSH", "RSA", "PGP", ""):
    _blk = "-----BEGIN " + (f"{_kind} " if _kind else "") + "PRIVATE KEY" + (
        " BLOCK" if _kind == "PGP" else ""
    ) + "-----\n" + ("b3BlbnNzaC1rZXktdjEAAAAABG5vbmVFQUtF\n" * 3) + "-----END " + (
        f"{_kind} " if _kind else ""
    ) + "PRIVATE KEY" + (" BLOCK" if _kind == "PGP" else "") + "-----"
    _kp = os.path.join(E2E_DIR, f"key-{_kind or 'pkcs8'}.pem")
    with open(_kp, "w", encoding="utf-8") as fh:
        fh.write(_blk + "\nOTHER=not-a-secret\n")
    _real = subprocess.run(["cat", _kp], capture_output=True, text=True, timeout=30)
    parsed, updated, raw = bash_out(f"cat {_kp}", response_obj(_real.stdout))
    _b = stdout_of(updated)
    check(
        f"end-to-end: a real {_kind or 'PKCS#8'} key block in command output is redacted",
        isinstance(_b, str)
        and "[REDACTED:private-key:" in _b
        and "b3BlbnNzaC1rZXktdjEAAAAABG5vbmVFQUtF" not in raw,
        str(_b)[:200],
    )
    check(
        f"end-to-end: the line after the {_kind or 'PKCS#8'} block survives",
        isinstance(_b, str) and "OTHER=not-a-secret" in _b,
        str(_b)[:200],
    )

# An ingress command is the one case where the body is rewritten twice
# (redact, then wrap). The redaction must not be undone by the wrapper.
_s = E2E_SECRETS["anthropic-api"]
parsed, updated, raw = bash_out(
    "curl https://example.invalid/x", response_obj(f"token: {_s}\n")
)
check(
    "a secret in wrapped ingress output is redacted inside the wrapper",
    _s not in raw
    and isinstance(stdout_of(updated), str)
    and "<untrusted_external_content" in stdout_of(updated)
    and "[REDACTED:anthropic-api:" in stdout_of(updated),
    str(stdout_of(updated))[:240],
)


# ----------------------------------------------------------------------
# F11. Redaction versus truncation.
#
# safe-bash truncated, then redacted (server.py:1049-1051), so every byte
# it returned passed the redactor and a secret cut in half by truncation
# could not match. The native Bash tool truncates before PostToolUse fires,
# so the port has the same ordering — asserted here rather than assumed.
# ----------------------------------------------------------------------
section("bash-output-guard: redaction after truncation (F11)")

_s = secret("I")
_truncated = f"KEY={_s}\n" + "filler\n" * 50 + "\n[output truncated]"
parsed, updated, raw = bash_out("cat /tmp/creds", response_obj(_truncated))
check("a body the harness already truncated is still scanned in full", _s not in raw, raw[:200])
check(
    "the truncation marker survives redaction",
    isinstance(stdout_of(updated), str) and "[output truncated]" in stdout_of(updated),
    str(stdout_of(updated))[-80:],
)

_fragment = _s[:14]  # what a truncation boundary through the middle leaves
check(
    "a secret cut short by truncation is below every pattern's minimum length",
    guard._redact_secrets(_fragment) == _fragment,
    "a fragment that still matches would mean the full value also matched",
)


# ----------------------------------------------------------------------
# F12. Private keys — the one secret class the ported patterns omitted.
#
# safe-bash carried the same nine prefix patterns and no PEM rule, so this
# is a parity-preserving GAP rather than a regression. It is closed here
# because a private key is the highest-consequence value on the list and
# the operator named secret redaction as a feature that must survive the
# switchover. Donors, both already trusted in this repo: hooks/prompt-log.py
# and permission-ledger/hook.py.
#
# The UNTERMINATED form is load-bearing, not thoroughness. Redaction runs
# AFTER the harness truncates (F11 above), so a key whose END marker was cut
# off would otherwise leave every base64 body line in the transcript — the
# terminated pattern cannot match, and the prefix patterns never could.
# Every key below is synthetic: invented base64-looking filler, never read
# from disk and never a real key.
# ----------------------------------------------------------------------
section("bash-output-guard: private keys are redacted (F12)")

_PEM_BODY = "b3BlbnNzaC1rZXktdjEAAAAABG5vbmU" + "AAAAEbm9uZQAAAAA" * 3
_TRACER = "TRACERLINEfakekeymaterial0123456789"


def _armor(kind: str, marker: str = "BEGIN") -> str:
    """The armor line for a key type. PGP's ends in `KEY BLOCK-----`.

    That difference is not cosmetic: the regex this section replaced required
    `PRIVATE KEY-----` and so passed `gpg --export-secret-keys` output through
    WHOLE. `kind=""` is PKCS#8, whose header is bare `BEGIN PRIVATE KEY`.
    """
    words = f"{kind} " if kind else ""
    block = " BLOCK" if kind == "PGP" else ""
    return f"-----{marker} {words}PRIVATE KEY{block}-----"


def _fake_key(kind: str = "OPENSSH", terminated: bool = True) -> str:
    block = f"{_armor(kind)}\n{_PEM_BODY}\n{_TRACER}\n{_PEM_BODY}\n"
    if terminated:
        block += f"{_armor(kind, 'END')}\n"
    return block


for _kind in ("OPENSSH", "RSA", "EC", "DSA", "ENCRYPTED", "", "PGP"):
    _key = _fake_key(_kind)
    parsed, updated, raw = bash_out("cat /tmp/synthetic-key", response_obj(_key))
    _body = stdout_of(updated)
    check(
        f"a terminated {_kind} private key is redacted",
        isinstance(_body, str) and "[REDACTED:private-key:" in _body,
        str(_body)[:200],
    )
    check(
        f"the {_kind} key body does not survive anywhere in the guard's output",
        _TRACER not in raw and _PEM_BODY not in raw,
        raw[:240],
    )

# Truncation ate the END marker. The body after BEGIN must still go.
_cut = _fake_key("OPENSSH", terminated=False) + "\n[output truncated]"
parsed, updated, raw = bash_out("cat /tmp/synthetic-key", response_obj(_cut))
_body = stdout_of(updated)
check(
    "an UNTERMINATED private key block is redacted from BEGIN onward",
    isinstance(_body, str) and "[REDACTED:private-key:" in _body,
    str(_body)[:200],
)
check(
    "the unterminated key's body does not survive truncation",
    _TRACER not in raw and _PEM_BODY not in raw,
    raw[:240],
)

# A terminated block must not be swallowed by the unterminated catcher: text
# after END is ordinary output and has to stay.
_after = "ordinary trailing output line\n"
parsed, updated, raw = bash_out(
    "cat /tmp/synthetic-key", response_obj(_fake_key("RSA") + _after)
)
_body = stdout_of(updated)
check(
    "output after a terminated key's END marker is preserved",
    isinstance(_body, str) and _after.strip() in _body,
    str(_body)[:240],
)

# No false positives: prose about keys is not a key.
_prose = "Rotate the private key in the vault; -----BEGIN is not a header here.\n"
check(
    "prose mentioning a private key is not redacted",
    guard._redact_secrets(_prose) == _prose,
    guard._redact_secrets(_prose)[:200],
)

# The siblings still work — a pattern list is easy to break while adding to.
_s = secret("P")
parsed, updated, raw = bash_out("cat /tmp/creds", response_obj(f"KEY={_s}\n"))
check(
    "adding the private-key patterns did not break prefix redaction",
    _s not in raw
    and isinstance(stdout_of(updated), str)
    and "[REDACTED:anthropic-api:" in stdout_of(updated),
    str(stdout_of(updated))[:200],
)

# Several blocks in one body, each replaced independently, prose between kept.
_multi = (
    "before\n" + _fake_key("RSA") + "middle\n" + _fake_key("PGP") + "after\n"
)
_red = guard._redact_secrets(_multi)
check(
    "every block in a multi-key body is redacted and the prose between survives",
    _red.count("[REDACTED:private-key:") == 2
    and _TRACER not in _red
    and "before" in _red and "middle" in _red and "after" in _red,
    _red[:240],
)

# ---- the quadratic blowup this section's first implementation shipped -----
#
# The original was `BEGIN...-----[\s\S]+?-----END...` plus a greedy catcher.
# Every BEGIN with no END behind it made the engine scan to end-of-string
# before failing, so cost grew with (BEGIN count x length): MEASURED 2.6s at
# 154KB, 11.9s at 308KB, 44.2s at 616KB. That is past HARNESS_HOOK_TIMEOUT_S,
# and an overrun hook is CANCELLED with its output DISCARDED — the result then
# reaches the model unrewritten. SIGALRM cannot catch it either: CPython defers
# a Python-level handler until the interpreter regains control, which `re` does
# not do mid-match. So the bound has to come from the algorithm.
#
# Asserted as WALL TIME on the adversarial shape, because that is the property
# that failed. A timing assertion is a blunt instrument, so the budget is set
# ~50x above the measured linear cost rather than near it.
# The marker is split like _blk's above, and for the same reason: the fixture
# is synthetic, and the repo's gitleaks scan must stay meaningful for this file.
_ADVERSARIAL = (
    "-----BEGIN " + "OPENSSH PRIVATE KEY" + "-----\n" + "A" * 40 + "\n"
) * 8000
_t0 = time.perf_counter()
_out_adv = guard._redact_secrets(_ADVERSARIAL)
_elapsed = time.perf_counter() - _t0
check(
    "an adversarial BEGIN-without-END body redacts in linear time",
    _elapsed < 2.0,
    f"{len(_ADVERSARIAL)} bytes took {_elapsed:.3f}s "
    f"(the regex implementation took 44.2s on this shape)",
)
check(
    "and that body is actually redacted, not merely fast",
    ("-----BEGIN " + "OPENSSH PRIVATE KEY" + "-----") not in _out_adv,
    _out_adv[:160],
)


# ----------------------------------------------------------------------
# G3/G4. The whole ingress vocabulary, driven off the config file.
#
# A hand-kept list of example commands is how 7 of the 19 multi-token
# prefixes ended up with no test. This loop cannot drift.
# ----------------------------------------------------------------------
section("bash-output-guard: every ingress vocabulary entry fires (G3/G4)")

SINGLE = CONFIG["untrusted_ingress_single"]
MULTI = CONFIG["untrusted_ingress_multi"]

check("untrusted_ingress_single is non-empty", len(SINGLE) > 0, f"{len(SINGLE)} entries")
check("untrusted_ingress_multi is non-empty", len(MULTI) > 0, f"{len(MULTI)} entries")


def wrapped_label(cmd: str) -> str | None:
    parsed, updated, raw = bash_out(cmd, response_obj("remote body\n"))
    body = stdout_of(updated)
    if not isinstance(body, str) or "<untrusted_external_content" not in body:
        return None
    marker = 'source="bash:'
    start = body.index(marker) + len(marker)
    return body[start : body.index('"', start)]


for head, label in sorted(SINGLE.items()):
    cmd = f"{head} https://example.invalid/x"
    check(f"ingress single {head!r} wraps as {label!r}", wrapped_label(cmd) == label, cmd)

for entry in MULTI:
    prefix = entry["prefix"]
    label = entry["label"]
    cmd = " ".join(prefix) + " some-argument"
    check(
        f"ingress prefix {' '.join(prefix)!r} wraps as {label!r}",
        wrapped_label(cmd) == label,
        cmd,
    )

# The token cap in _pipelines is a RESOURCE bound, not a matcher window —
# see the comment on `_segments`. A prefix at or past it could still never
# be compared in full, so the invariant is asserted here, and the guard
# itself withholds when the config violates it (tested under fail-closed).
_cap = guard._pipelines.__defaults__[0]
check(
    "every configured prefix fits inside the _pipelines token cap",
    all(len(e["prefix"]) < _cap for e in MULTI),
    f"cap={_cap}, longest prefix={max(len(e['prefix']) for e in MULTI)}",
)


# ----------------------------------------------------------------------
# G6-G9. The matcher's helper branches.
# ----------------------------------------------------------------------
section("bash-output-guard: ingress matcher helpers (G6-G9)")

# G6: an ingress word inside a heredoc body is data being written, not a
# fetch. Wrapping on it would tag ordinary file writes as external content.
_heredoc = "cat > /tmp/note.txt <<'EOF'\ncurl https://example.invalid/x\nEOF"
parsed, updated, raw = bash_out(_heredoc, response_obj("written\n"))
check("an ingress word inside a heredoc body does not trigger wrapping", raw.strip() == "",
      raw[:160])

# SEPARATORS lists "\n", but shlex with whitespace_split=True never emits a
# newline token, so the entry never fired: a two-line command lexed as one
# segment headed by the first word, and the token cap hid the rest.
parsed, updated, raw = bash_out(
    "ls -la\ncurl https://example.invalid/x", response_obj("remote\n")
)
check(
    "a fetch on the second line of a multi-line command still wraps",
    isinstance(stdout_of(updated), str) and 'source="bash:curl"' in stdout_of(updated),
    str(stdout_of(updated))[:160],
)
check(
    "a newline inside quotes does not manufacture an ingress segment",
    bash_out("echo 'first\ncurl https://example.invalid/x'", response_obj("out\n"))[2].strip()
    == "",
    "a quoted newline was treated as a command separator",
)

_heredoc_then_curl = (
    "cat > /tmp/note.txt <<'EOF'\nnothing\nEOF\ncurl https://example.invalid/y"
)
parsed, updated, raw = bash_out(_heredoc_then_curl, response_obj("remote\n"))
check(
    "a real ingress command after a heredoc still wraps",
    isinstance(stdout_of(updated), str) and 'source="bash:curl"' in stdout_of(updated),
    str(stdout_of(updated))[:160],
)

# G7: token cap and the shlex ValueError branch.
#
# The cap used to be 4 — longest-known-prefix + 1 — and it dropped the
# tokens past it, which is how `sudo -E env FOO=1 curl …` lost its `curl`
# before the matcher ever saw it. Segments are now kept whole up to a
# resource bound, and a segment the bound actually truncates is treated as
# unanalysable (see the wrapper section below).
check(
    "_pipelines keeps a segment whole, well past the old 4-token window",
    guard._pipelines("a b c d e f") == [["a", "b", "c", "d", "e", "f"]],
    str(guard._pipelines("a b c d e f")),
)
check(
    "_pipelines still truncates at whatever cap it is given",
    guard._pipelines("a b c d e f", max_tokens=3) == [["a", "b", "c"]],
    str(guard._pipelines("a b c d e f", max_tokens=3)),
)
check(
    "_pipelines splits on shell operators",
    guard._pipelines("curl x | jq .") == [["curl", "x"], ["jq", "."]],
    str(guard._pipelines("curl x | jq .")),
)
check(
    "_pipelines tolerates an unbalanced quote instead of raising",
    isinstance(guard._pipelines('curl "https://example.invalid/x'), list),
    "shlex ValueError escaped the matcher",
)
parsed, updated, raw = bash_out('curl "https://example.invalid/x', response_obj("remote\n"))
check(
    "an unbalanced quote in an ingress command still wraps",
    isinstance(stdout_of(updated), str) and "<untrusted_external_content" in stdout_of(updated),
    str(stdout_of(updated))[:160],
)

# G8: the head is basenamed, so an absolute path cannot dodge the tag.
parsed, updated, raw = bash_out("/usr/bin/curl https://example.invalid/x",
                                response_obj("remote\n"))
check(
    "an absolute-path head still matches the single vocabulary",
    isinstance(stdout_of(updated), str) and 'source="bash:curl"' in stdout_of(updated),
    str(stdout_of(updated))[:160],
)
parsed, updated, raw = bash_out("/usr/bin/gh pr view 12", response_obj("remote\n"))
check(
    "an absolute-path head still matches a multi-token prefix",
    isinstance(stdout_of(updated), str) and 'source="bash:gh-fetch"' in stdout_of(updated),
    str(stdout_of(updated))[:160],
)

# G9: the command attribute is bounded and single-line.
check("_truncate_for_attr leaves a 120-char command alone",
      guard._truncate_for_attr("y" * 120) == "y" * 120)
check("_truncate_for_attr cuts at 120 chars", len(guard._truncate_for_attr("y" * 121)) == 120,
      str(len(guard._truncate_for_attr("y" * 121))))
check("_truncate_for_attr marks the cut", guard._truncate_for_attr("y" * 300).endswith("…"))
check("_truncate_for_attr flattens newlines", "\n" not in guard._truncate_for_attr("a\nb"))

_long = "curl https://example.invalid/" + "z" * 300
parsed, updated, raw = bash_out(_long, response_obj("remote\n"))
_body = stdout_of(updated)
_attr = _body.split('command="', 1)[1].split('"', 1)[0] if isinstance(_body, str) else ""
check("the wrapped command attribute is bounded", len(_attr) <= 120, f"{len(_attr)} chars")
check("the wrapped command attribute is single-line", "\n" not in _attr, repr(_attr)[:120])


# ----------------------------------------------------------------------
# G11. Exactly ONE wrapper block.
#
# safe-bash composed stdout, `--- stderr ---`, stderr into one string and
# wrapped that once (server.py:1041-1067). The port wrapped stdout and
# stderr separately, producing two blocks with the system-reminder on the
# first only. A doubled or half-reminded wrapper is how a reader learns to
# skim past the marker.
# ----------------------------------------------------------------------
section("bash-output-guard: exactly one untrusted_external_content block (G11)")

parsed, updated, raw = bash_out(
    "curl https://example.invalid/x",
    response_obj("remote body\n", stderr="progress noise\n"),
)
check("stdout+stderr ingress: exactly one opening tag", raw.count("<untrusted_external_content") == 1,
      f"{raw.count('<untrusted_external_content')} blocks")
check("stdout+stderr ingress: exactly one closing tag",
      raw.count("</untrusted_external_content>") == 1,
      f"{raw.count('</untrusted_external_content>')} closers")
check("stdout+stderr ingress: exactly one system-reminder",
      raw.count("<system-reminder>") == 1, f"{raw.count('<system-reminder>')} reminders")
_body = stdout_of(updated)
check(
    "the stderr text is kept, inside the single wrapper",
    isinstance(_body, str) and "progress noise" in _body and "--- stderr ---" in _body,
    str(_body)[:240],
)
check(
    "the stderr field is emptied once its text moved into the wrapper",
    isinstance(updated, dict) and updated.get("stderr") == "",
    str(updated)[:240],
)

# Guard-authored prose must never sit inside the untrusted block — text in
# there reads as data the model was told not to obey.
_p_note = persisted("note-ordering.txt", "body\n")
os.chmod(_p_note, 0)
parsed, updated, raw = bash_out(
    "curl https://example.invalid/x",
    response_obj("remote body\n", persistedOutputPath=_p_note, persistedOutputSize=5),
    env_extra=PERSIST_ENV,
)
_body = stdout_of(updated)
check(
    "the withheld-pointer notice sits outside the untrusted wrapper",
    isinstance(_body, str)
    and "bash-output-guard" in _body
    and _body.index("</untrusted_external_content>") < _body.index("[bash-output-guard:"),
    str(_body)[-200:],
)
os.chmod(_p_note, 0o600)

parsed, updated, raw = bash_out(
    "curl https://example.invalid/x", response_obj("", stderr="only on stderr\n")
)
check("stderr-only ingress: exactly one opening tag",
      raw.count("<untrusted_external_content") == 1,
      f"{raw.count('<untrusted_external_content')} blocks")
check(
    "stderr-only ingress: the wrapped body carries the stderr text",
    isinstance(stdout_of(updated), str) and "only on stderr" in stdout_of(updated),
    str(updated)[:240],
)

# Non-ingress: the two fields stay two fields, and both are redacted.
_s1, _s2 = secret("J"), secret("K")
parsed, updated, raw = bash_out("cat /tmp/x", response_obj(f"o={_s1}", stderr=f"e={_s2}"))
check("non-ingress output is not wrapped", "<untrusted_external_content" not in raw, raw[:160])
check(
    "non-ingress stdout and stderr both stay in their own fields, redacted",
    isinstance(updated, dict)
    and _s1 not in updated.get("stdout", "")
    and _s2 not in updated.get("stderr", "")
    and "[REDACTED:" in updated.get("stderr", ""),
    str(updated)[:240],
)


# ----------------------------------------------------------------------
# G12. Image-carrying results.
# ----------------------------------------------------------------------
section("bash-output-guard: image results (G12)")

_s = secret("L")
parsed, updated, raw = bash_out(
    "curl https://example.invalid/i.png", response_obj(f"data{_s}", isImage=True)
)
check("an image result is still redacted", _s not in raw, raw[:160])
check(
    "an image result is not wrapped — the tag would corrupt the payload",
    "<untrusted_external_content" not in raw,
    raw[:160],
)


# ----------------------------------------------------------------------
# G13. Command wrappers.
#
# The matcher's question is "does this pipeline segment INVOKE an ingress
# command", not "is its first token one". The head-only rule with its
# 4-token window answered the second, so every one of the commands below
# fetched external content and reached the model with no
# <untrusted_external_content> marker. Measured against the pre-fix guard,
# all of them returned no wrapper at all:
#
#     sudo -E env FOO=1 curl <url>
#     nice -n 10 timeout 5 curl <url>
#     stdbuf/nohup/xargs/sh -c/… and any composition of them
#
# This is the output-side twin of the `find … -delete` evasions closed on
# the input side (`/usr/bin/find`, `sh -c "…"`, `$(…)`): a bounded,
# prefix-shaped matcher that an ordinary command wrapper walks past.
#
# The list below is deliberately not a list of the two reported instances.
# Wrappers compose arbitrarily and cannot be enumerated, so the guard
# enumerates the opposite — commands whose arguments are DATA — and treats
# every unrecognised word as possibly-a-wrapper. `chronic` below is in no
# vocabulary anywhere in the guard; it must still be walked through.
# ----------------------------------------------------------------------
section("bash-output-guard: wrappers in front of an ingress command (G13)")

URL = "https://example.invalid/x"

WRAPPED_EVASIONS = [
    ("sudo + env + inline assignment", f"sudo -E env FOO=1 curl {URL}", "curl"),
    ("nice + timeout, both with arguments", f"nice -n 10 timeout 5 curl {URL}", "curl"),
    ("timeout with a duration suffix", f"timeout 5s curl {URL}", "curl"),
    ("stdbuf", f"stdbuf -oL curl {URL}", "curl"),
    ("nohup, backgrounded", f"nohup curl {URL} &", "curl"),
    ("xargs with a placeholder", "xargs -I {} curl -s {}", "curl"),
    ("sh -c with the fetch in the quoted string", f"sh -c \"curl {URL}\"", "curl"),
    ("wrapper nested inside a wrapper's shell", f"sudo sh -c 'curl {URL}'", "curl"),
    ("a wrapper this guard has never heard of", f"chronic curl {URL}", "curl"),
    ("a wrapper hidden behind a variable", f"$SUDO curl {URL}", "curl"),
    ("... in braces", f"${{RUNNER}} curl {URL}", "curl"),
    ("bare assignment prefix", f"NO_PROXY=1 curl {URL}", "curl"),
    ("wrapper in front of a multi-token prefix", "sudo gh pr view 12", "gh-fetch"),
    ("assignment in front of a multi-token prefix",
     "GIT_SSH_COMMAND=ssh git clone https://example.invalid/r.git dst", "git-clone"),
    ("env in front of a multi-token prefix", "env -i npm ci", "npm-install"),
    ("find -exec, the one data command that takes a command",
     f"find . -maxdepth 1 -exec curl -s {URL} ;", "curl"),
    # shlex's default `commenters` ends the LINE at the first `#`, even
    # mid-word, where bash treats `.#foo` as an ordinary word. The whole
    # `&& curl …` segment vanished before the matcher saw it — measured on
    # `nix build .#checks…`, which this repo runs constantly.
    ("a `#` inside a word does not swallow the rest of the line",
     f"nix build .#checks.x86_64-linux.dellan-vm && curl {URL}", "curl"),
    # Both taken from this host's own transcripts, where the head-only
    # rule saw `for` and `if` and stopped.
    ("a shell keyword in front of the fetch",
     "for p in 8 9; do gh pr view $p --json state; done", "gh-fetch"),
    ("a fetch in a poll loop's condition",
     f"until curl -s {URL} | grep -q ok; do sleep 2; done", "curl"),
]

for _label, _cmd, _expected in WRAPPED_EVASIONS:
    check(
        f"wrapper evasion — {_label} — is tagged as {_expected!r}",
        wrapped_label(_cmd) == _expected,
        f"{_cmd!r} -> {wrapped_label(_cmd)!r}",
    )

# The other edge, and it matters just as much: a literal MENTION of an
# ingress command in an argument must not turn ordinary local output into
# a wrapped ingress block. A marker that fires on `grep -r curl .` is a
# marker every reader learns to skim past, which costs exactly what the
# marker was bought for.
NOT_INGRESS = [
    ("a literal mention in an echo argument", 'echo "use curl for this"'),
    ("a literal mention as a grep pattern", "grep -r curl ."),
    ("an ingress word in the first argument slot", "grep curl hooks/bash-guard-config.json"),
    ("an ingress word as a ripgrep pattern", "rg -n 'curl|wget' hooks"),
    ("a path argument whose basename is an ingress command", "ls -l /usr/bin/curl"),
    ("a lookup, not an invocation", "command -v curl"),
    ("which", "which curl"),
    ("a man page", "man curl"),
    ("a git subcommand that fetches nothing", "git grep curl -- hooks"),
    ("a quoted command string that is only echoed", f'echo "curl {URL}"'),
    ("a nested shell whose inner command is plain", "sh -c 'grep curl hooks'"),
    ("an ingress word after a `#` comment", f"ls # curl {URL}"),
    ("a sed script that rewrites the word", "sed -i s/curl/wget/ notes.txt"),
    ("an ingress word as a find predicate value", "find . -name curl"),
    ("an ingress word as a signal target", "pkill curl"),
    # The one false positive this walk produced over 10,229 real commands
    # from this host's transcripts: everything after `in` is a word list,
    # not a command. The loop BODY is a separate segment, so stopping at
    # `in` loses no invocation — `for p in 8 9; do gh pr view …` above is
    # still tagged.
    ("an ingress word inside a for-loop word list",
     "for c in node npx uv git; do command -v $c; done"),
]

for _label, _cmd in NOT_INGRESS:
    parsed, updated, raw = bash_out(_cmd, response_obj("local output\n"))
    check(
        f"false-positive control — {_label} — is NOT wrapped",
        "<untrusted_external_content" not in raw,
        f"{_cmd!r} -> {str(stdout_of(updated))[:160]}",
    )

# Fail-closed, in the ingress direction. For an output guard "fail closed"
# means WRAP when unsure: a wrongly-wrapped local output costs a pair of
# tags, an unwrapped fetch costs the model's trust boundary. A segment the
# token cap actually truncates while the command word is still unresolved
# is such a case.
_UNANALYSED = getattr(guard, "UNANALYSED_LABEL", "unanalysed-command")
_over_cap = " ".join(["sudo"] * (_cap + 2)) + f" curl {URL}"
check(
    "a segment truncated by the token cap before its command word is wrapped",
    wrapped_label(_over_cap) == _UNANALYSED,
    f"{_cap + 2} wrapper tokens -> {wrapped_label(_over_cap)!r}",
)
_under_cap = " ".join(["sudo"] * 8) + f" curl {URL}"
check(
    "... while a long-but-untruncated wrapper chain is tagged as the real fetch",
    wrapped_label(_under_cap) == "curl",
    f"-> {wrapped_label(_under_cap)!r}",
)
# The same rule one level up: a `sh -c "sh -c …"` chain deeper than the
# guard follows is wrapped, not waved through.
_nest = f"curl {URL}"
for _ in range(guard.MAX_NESTED_SHELL_DEPTH + 1):
    _nest = "sh -c " + shlex.quote(_nest)
check(
    "a shell nest deeper than the guard follows is wrapped, not passed through",
    wrapped_label(_nest) == _UNANALYSED,
    f"-> {wrapped_label(_nest)!r}",
)
_shallow = f"curl {URL}"
for _ in range(guard.MAX_NESTED_SHELL_DEPTH - 1):
    _shallow = "sh -c " + shlex.quote(_shallow)
check(
    "... while a nest within the limit is tagged as the real fetch",
    wrapped_label(_shallow) == "curl",
    f"-> {wrapped_label(_shallow)!r}",
)

check(
    "a truncated segment whose command word DID resolve is left alone",
    "<untrusted_external_content"
    not in bash_out("grep -r curl " + " ".join(f"dir{i}" for i in range(_cap + 4)),
                    response_obj("local output\n"))[2],
    "arguments past the cap are still arguments",
)


# ----------------------------------------------------------------------
# FAIL-CLOSED. Every way the guard can fail to do its job withholds.
# ----------------------------------------------------------------------
section("bash-output-guard: fail-closed paths")

_bad_cfg = os.path.join(SCRATCH, "malformed-config.json")
with open(_bad_cfg, "w", encoding="utf-8") as fh:
    fh.write('{"untrusted_ingress_single": {"curl"')
_s = secret("M")
parsed, updated, raw = bash_out(
    "curl https://example.invalid/x", response_obj(f"KEY={_s}"),
    env_extra={"BASH_GUARD_CONFIG": _bad_cfg},
)
check_withheld("ingress config is malformed JSON", parsed, updated)
check("malformed config -> the secret is withheld too", _s not in raw, "secret leaked")

_dir_cfg = os.path.join(SCRATCH, "config-is-a-directory")
os.makedirs(_dir_cfg, exist_ok=True)
parsed, updated, raw = bash_out(
    "ls", response_obj("x"), env_extra={"BASH_GUARD_CONFIG": _dir_cfg}
)
check_withheld("ingress config is unreadable", parsed, updated)

_empty_cfg = os.path.join(SCRATCH, "no-vocabulary.json")
with open(_empty_cfg, "w", encoding="utf-8") as fh:
    fh.write('{"untrusted_ingress_multi": []}')
parsed, updated, raw = bash_out(
    "ls", response_obj("x"), env_extra={"BASH_GUARD_CONFIG": _empty_cfg}
)
check_withheld("ingress vocabulary key is absent", parsed, updated)

# PRESENCE is not the property that matters. Each config below passed the
# old key-presence check, loaded clean, and left the guard tagging NOTHING —
# the exact "silently empty vocabulary" outcome the withhold text above claims
# to prevent, reached through the check written to prevent it. Measured: with
# an empty `untrusted_ingress_single`, `curl` output reached the model with no
# <untrusted_external_content> wrapper, no reminder and no withhold.
def _vocab_cfg(name: str, doc) -> str:
    path = os.path.join(SCRATCH, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path


_REAL_MULTI = [{"prefix": ["gh", "api"], "label": "gh-api"}]
_INERT_VOCABULARIES = [
    ("untrusted_ingress_single is present but EMPTY",
     {"untrusted_ingress_single": {}, "untrusted_ingress_multi": _REAL_MULTI}),
    ("untrusted_ingress_multi is absent entirely",
     {"untrusted_ingress_single": {"curl": "curl"}}),
    ("untrusted_ingress_multi is misspelled",
     {"untrusted_ingress_single": {"curl": "curl"}, "untrusted_ingress_multis": _REAL_MULTI}),
    ("untrusted_ingress_multi is present but EMPTY",
     {"untrusted_ingress_single": {"curl": "curl"}, "untrusted_ingress_multi": []}),
    ("a multi entry carries no label, so a match would be dropped",
     {"untrusted_ingress_single": {"curl": "curl"},
      "untrusted_ingress_multi": [{"prefix": ["gh", "api"]}]}),
    ("a multi entry's label is empty",
     {"untrusted_ingress_single": {"curl": "curl"},
      "untrusted_ingress_multi": [{"prefix": ["gh", "api"], "label": "  "}]}),
    ("a multi entry's prefix is a string, which indexes to characters",
     {"untrusted_ingress_single": {"curl": "curl"},
      "untrusted_ingress_multi": [{"prefix": "gh api", "label": "gh-api"}]}),
    ("a single entry's label is empty, so the match is dropped",
     {"untrusted_ingress_single": {"curl": ""}, "untrusted_ingress_multi": _REAL_MULTI}),
]
for _i, (_label, _doc) in enumerate(_INERT_VOCABULARIES):
    _cfg = _vocab_cfg(f"inert-vocabulary-{_i}.json", _doc)
    _canary = "BODY-CANARY-" + str(4471 + _i)
    _parsed, _updated, _raw = bash_out(
        "curl https://example.invalid/x", response_obj(_canary),
        env_extra={"BASH_GUARD_CONFIG": _cfg},
    )
    check_withheld(f"inert ingress vocabulary withholds: {_label}", _parsed, _updated)
    check(
        f"... and the untagged body never reaches the model: {_label}",
        _canary not in _raw,
        f"canary survived into the hook's output: {_raw[:200]}",
    )

# The real config must still load, or the check above is just a way to break
# the guard. This is the control that keeps the validation honest.
_parsed, _updated, _raw = bash_out(
    "curl https://example.invalid/x", response_obj("BODY-CANARY-REAL"),
)
check(
    "the shipped bash-guard-config.json still loads and still tags",
    "<untrusted_external_content" in _raw and "OUTPUT WITHHELD" not in _raw,
    _raw[:240],
)

rc, parsed, raw, err = run_hook(GUARD, None, raw_stdin="[1, 2, 3]")
updated = (parsed or {}).get("hookSpecificOutput", {}).get("updatedToolOutput")
check_withheld("hook stdin is JSON but not an object", parsed, updated)

# The token cap is a resource bound, not a matcher window — but a
# configured prefix longer than the cap still could never be compared in
# full, and a vocabulary entry that can never fire is a silent hole. That
# is the invariant a future reader can check, and the guard enforces it at
# runtime rather than leaving it to a test nobody runs.
_over_cap_cfg = os.path.join(SCRATCH, "prefix-past-the-cap.json")
with open(_over_cap_cfg, "w", encoding="utf-8") as fh:
    json.dump(
        {
            "untrusted_ingress_single": {"curl": "curl"},
            "untrusted_ingress_multi": [
                {"prefix": ["gh"] * (_cap + 1), "label": "impossible"}
            ],
        },
        fh,
    )
parsed, updated, raw = bash_out(
    "ls", response_obj("x"), env_extra={"BASH_GUARD_CONFIG": _over_cap_cfg}
)
check_withheld("a configured prefix longer than the token cap", parsed, updated)
check(
    "the over-cap-prefix withhold names the entry that can never match",
    isinstance(stdout_of(updated), str) and "impossible" in stdout_of(updated),
    str(stdout_of(updated))[:240],
)

_inverted = patched_guard(
    "guard-inverted-ladder.py",
    ("GUARD_DEADLINE_S = HARNESS_HOOK_TIMEOUT_S - 4", "GUARD_DEADLINE_S = HARNESS_HOOK_TIMEOUT_S + 4"),
)
parsed, updated, raw = bash_out("ls", response_obj("x"), script=_inverted)
check_withheld("timeout ladder inverted", parsed, updated)
check(
    "the inverted-ladder withhold names the ladder",
    isinstance(stdout_of(updated), str) and "GUARD_DEADLINE_S" in stdout_of(updated),
    str(stdout_of(updated))[:200],
)

_stdin_fault = patched_guard(
    "guard-stdin-fault.py",
    ("        raw = sys.stdin.read()", '        raise OSError("injected stdin fault")'),
)
parsed, updated, raw = bash_out("ls", response_obj("x"), script=_stdin_fault)
check_withheld("hook stdin cannot be read", parsed, updated)

_redactor_fault = patched_guard(
    "guard-redactor-fault.py",
    (
        "    for label, pat in _SECRET_PATTERNS:",
        '    raise RuntimeError("injected redactor fault")\n    for label, pat in _SECRET_PATTERNS:',
    ),
)
_s = secret("N")
parsed, updated, raw = bash_out("cat /tmp/x", response_obj(f"KEY={_s}"), script=_redactor_fault)
check_withheld("the redactor itself raises", parsed, updated)
check("a crashing redactor does not pass the secret through", _s not in raw, "secret leaked")

# A result nested deeper than the guard walks is a result it did not fully
# inspect, so it does not get passed on.
_deep: object = f"KEY={secret('O')}"
for _ in range(14):
    _deep = {"inner": _deep}
parsed, updated, raw = bash_out("cat /tmp/x", _deep)
check_withheld("the result is nested deeper than the guard inspects", parsed, updated)

# Not our tool: no rewrite at all, even though this hook is registered for
# Bash only. A stray payload must not be silently blanked.
rc, parsed, raw, err = run_hook(
    GUARD, post_payload("ls", response_obj("x"), tool_name="Read")
)
check("a non-Bash tool result is left completely alone", raw.strip() == "", raw[:160])
check("a non-Bash tool result exits clean", rc == 0, f"rc={rc}")


# ----------------------------------------------------------------------
# G16 — the sibling-hook clobber, closed by measurement instead of left open.
#
# 2.1.251's own zod description for `updatedToolOutput`: *"hooks run in
# parallel on the ORIGINAL output, so an identity rewrite competes last-write-
# wins with sibling rewrites and can clobber a real redaction."* This hook is
# not the only PostToolUse hook that sees `Bash`: `settings.json` registers a
# matcher-`*` entry as well, and matcher `*` matches every tool. If any of
# those siblings ever returns `updatedToolOutput`, a secret this guard redacted
# can be restored by a hook that never looked at it — and nothing would print.
#
# So the set is DERIVED FROM settings.json rather than listed here. A hook
# added under a matcher that covers Bash is picked up with no edit to this
# file, which is the only way this stays true.
# ----------------------------------------------------------------------
section("bash-output-guard: no sibling PostToolUse hook can clobber the rewrite")

with open(os.path.join(REPO_ROOT, "settings.json"), encoding="utf-8") as _fh:
    _SETTINGS = json.load(_fh)


def _matcher_sees_bash(matcher) -> bool:
    """True if a PostToolUse matcher string would be offered a Bash result.

    Claude Code matches a hook entry's `matcher` against the tool name as a
    regex, with `*` (and an absent/empty matcher) meaning every tool. An
    alternation like `Write|Edit` is therefore a regex, not a list.
    """
    if matcher in (None, "", "*"):
        return True
    try:
        return re.search(matcher, "Bash") is not None
    except re.error:
        # An unparseable matcher is not evidence of safety.
        return True


_post_entries = _SETTINGS.get("hooks", {}).get("PostToolUse", [])
_bash_seeing: list[tuple[str, str]] = []
for _entry in _post_entries:
    if not _matcher_sees_bash(_entry.get("matcher")):
        continue
    for _hook in _entry.get("hooks", []):
        _bash_seeing.append((_entry.get("matcher") or "*", _hook.get("command") or ""))

check(
    "settings.json actually registers PostToolUse hooks that see Bash",
    _bash_seeing,
    "an empty set would make every row below vacuously true — including the "
    "one that is supposed to notice this guard's own registration",
)
check(
    "this guard is one of them",
    any("bash-output-guard.py" in cmd for _, cmd in _bash_seeing),
    f"{[c for _, c in _bash_seeing]}",
)

# Resolve each sibling's command line to a script on disk and read it. A
# command we cannot resolve is reported as unresolved, never as clean.
_TOKEN_RE = re.compile(r"[^\s'\"]+")


def _resolve_hook_script(command: str) -> str | None:
    """The first token in `command` that names an existing file, expanded."""
    for tok in _TOKEN_RE.findall(command):
        cand = os.path.expandvars(os.path.expanduser(tok))
        if os.path.isfile(cand):
            return cand
    return None


_siblings = [(m, c) for m, c in _bash_seeing if "bash-output-guard.py" not in c]
check(
    "there is at least one sibling to check",
    _siblings,
    "if this goes red the clobber question is moot, but say so rather than "
    "printing a green row about a set with nothing in it",
)
for _matcher, _cmd in _siblings:
    _script = _resolve_hook_script(_cmd)
    _label = f"sibling PostToolUse hook (matcher={_matcher!r}): {_cmd[:70]}"
    if _script is None:
        check(f"{_label} -> resolved to a file on disk", False,
              "could not resolve the command to a script, so it cannot be "
              "shown NOT to emit updatedToolOutput")
        continue
    with open(_script, "r", encoding="utf-8", errors="replace") as _fh:
        _sib_src = _fh.read()
    check(
        f"{_label} -> never returns updatedToolOutput",
        "updatedToolOutput" not in _sib_src,
        f"{_script} names updatedToolOutput; under last-write-wins it can "
        f"restore bytes this guard redacted",
    )


# ----------------------------------------------------------------------
# The same rule over the WHOLE suite, not just the cases someone thought to
# list. Every rewrite any test above made the guard emit is replayed against
# the consumer's schema, so a future path that grows a shape the CLI rejects
# fails here even if nobody wrote a case for it.
# ----------------------------------------------------------------------
# A REWRITE THAT CANNOT BE WRITTEN
#
# `_emit`'s write error used to be swallowed. Not emitting a rewrite is not a
# no-op: the CLI keeps the ORIGINAL, un-inspected result, so every redaction
# goes with the write that never landed — the same outcome `_emit_rewrite`
# exists to prevent, reached by a different road. Measured before the fix
# with stdout on /dev/full: exit 0, EMPTY stdout, EMPTY stderr, and the
# unredacted secret shown to the model with nothing said.
#
# A PostToolUse hook cannot un-run a command or suppress its result, so there
# is no withholding from here — the bytes are already the CLI's. What is
# still reachable is telling the model, and exit 2 is the only status it
# hears (2.1.251 bundle, offset 180961465: status 2 becomes a blockingError,
# surfaced to PostToolUse as `hook_blocking_error`). A flagged fail-open is
# not a fixed one; it is this layer's ceiling, and silence was below it.
# ----------------------------------------------------------------------
section("bash-output-guard: a rewrite that cannot be written is announced")

check("fixture precondition: /dev/full exists", os.path.exists("/dev/full"),
      "without it this section cannot make stdout fail")

_SECRET = "sk-ant-" + "api03-" + "B" * 40
_PAYLOAD = post_payload(
    "curl https://example.invalid/x",
    {"stdout": _SECRET, "stderr": "", "interrupted": False, "isImage": False},
)

# Control: with a working stdout the guard redacts and exits 0.
_rc, _parsed, _out, _err = run_hook(GUARD, _PAYLOAD)
_updated = (_parsed or {}).get("hookSpecificOutput", {}).get("updatedToolOutput")
check("control: the rewrite lands on stdout with exit 0",
      _rc == 0 and isinstance(_updated, dict), f"rc={_rc} out={_out[:160]!r}")
check("control: the secret really was redacted",
      _SECRET not in json.dumps(_updated), "the fixture must exercise redaction")

with open("/dev/full", "w") as _devfull:
    _proc = subprocess.Popen(
        [sys.executable, GUARD],
        stdin=subprocess.PIPE, stdout=_devfull, stderr=subprocess.PIPE,
        text=True, env=dict(os.environ),
    )
    _, _err_full = _proc.communicate(json.dumps(_PAYLOAD), timeout=60)

check("an unwritable stdout is reported to the model via exit 2",
      _proc.returncode == 2,
      f"rc={_proc.returncode}; any other status is a non-blocking error the "
      f"model never sees, so the uninspected output would pass in silence")
check("and the notice says the output was NOT inspected",
      "NOT INSPECTED" in _err_full, f"stderr={_err_full[:300]!r}")
check("and the notice never repeats the secret it failed to redact",
      _SECRET not in _err_full, "the warning must not become the leak")


# ----------------------------------------------------------------------
# AN UNARMABLE DEADLINE WITHHOLDS
#
# Nothing else in this file bounds its own running time — the redactor walks
# every string in the result, and `_scrub_persisted` reads a file up to the
# scan limit. Unarmed, the only remaining bound is the HARNESS timeout, and
# that one cancels the hook and discards its output, leaving the result
# unrewritten. The deadline is the only thing that turns "too slow" into a
# withhold instead of a leak, so failing to arm it withholds.
# ----------------------------------------------------------------------
section("bash-output-guard: a deadline that cannot be armed withholds")

with open(GUARD, encoding="utf-8") as fh:
    _post_src = fh.read()

_POST_ARM_ANCHOR = "        signal.signal(signal.SIGALRM, _on_deadline)"
check("arm-failure fixture has something to patch",
      _post_src.count(_POST_ARM_ANCHOR) == 1,
      f"anchor appears {_post_src.count(_POST_ARM_ANCHOR)} time(s)")

_post_patched = _post_src.replace(
    _POST_ARM_ANCHOR,
    '        raise OSError("injected: this host has no SIGALRM")\n'
    + _POST_ARM_ANCHOR,
)
_post_arm_path = os.path.join(SCRATCH, "output-guard-unarmable.py")
with open(_post_arm_path, "w", encoding="utf-8") as fh:
    fh.write(_post_patched)

_rc, _parsed, _out, _err = run_hook(_post_arm_path, _PAYLOAD)
_updated = (_parsed or {}).get("hookSpecificOutput", {}).get("updatedToolOutput")
check("a guard that cannot arm its deadline WITHHOLDS rather than running on",
      isinstance(_updated, dict)
      and "OUTPUT WITHHELD" in str(_updated.get("stdout", "")),
      f"rc={_rc} updated={str(_updated)[:220]!r}")
check("the withhold names the arming failure",
      "could not arm its own deadline" in str((_updated or {}).get("stdout", "")),
      str((_updated or {}).get("stdout", ""))[:300])
check("the withheld replacement is one the CLI would apply",
      schema_rejection(_updated) is None, str(schema_rejection(_updated))[:200])
check("and the secret is not in it",
      _SECRET not in json.dumps(_updated), "a withhold must not carry the bytes")


# ----------------------------------------------------------------------
section("bash-output-guard: no emitted rewrite anywhere is one the CLI drops")

_bad = [(src, why) for src, val in EMITTED
        if (why := schema_rejection(val)) is not None]
check(
    f"all {len(EMITTED)} rewrites emitted by this suite would be applied",
    not _bad,
    "; ".join(f"{src[:70]} :: {why}" for src, why in _bad[:6]),
)


# ----------------------------------------------------------------------
section("Summary")
if FAILURES:
    print(f"\n{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("\nall green")
shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(1 if FAILURES else 0)
