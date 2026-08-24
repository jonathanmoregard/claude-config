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

FIELDS_SEEN_STR = {"stdout", "stderr", "backgroundTaskId", "returnCodeInterpretation",
                   "persistedOutputPath"}

classified = set(getattr(guard, "OUTPUT_FIELDS", ())) | set(
    getattr(guard, "METADATA_STRING_FIELDS", ())
)
check(
    "every string-typed field the Bash tool really returns is classified",
    FIELDS_SEEN_STR <= classified,
    f"unclassified: {sorted(FIELDS_SEEN_STR - classified)}",
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

# The token cap in _pipelines silently stops matching any prefix at or past
# its length. Assert the invariant, not the current numbers.
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
check(
    "_pipelines keeps only the first max_tokens tokens of a segment",
    guard._pipelines("a b c d e f") == [["a", "b", "c", "d"]],
    str(guard._pipelines("a b c d e f")),
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

rc, parsed, raw, err = run_hook(GUARD, None, raw_stdin="[1, 2, 3]")
updated = (parsed or {}).get("hookSpecificOutput", {}).get("updatedToolOutput")
check_withheld("hook stdin is JSON but not an object", parsed, updated)

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
section("Summary")
if FAILURES:
    print(f"\n{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("\nall green")
shutil.rmtree(SCRATCH, ignore_errors=True)
sys.exit(1 if FAILURES else 0)
