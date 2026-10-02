#!/usr/bin/env python3
"""Permission Friction Ledger — stage 1 capture hook.

Registered in ~/.claude/settings.json under hooks.PermissionRequest and
hooks.PermissionDenied (matcher "*", timeout 5).

Contract, in order of importance:
  1. Never blocks, never decides. Always exit 0 with empty stdout, under every
     failure mode, including "the ledger directory is read-only".
  2. Fast. Stdlib only, no subprocess, no network. Measured in-process work is
     ~0.6 ms for a typical payload and ~19 ms for a pathological 1 MB one
     (dominated by the specified canonical-JSON + sha256 digest over the whole
     unredacted input). Note that CPython process startup on this machine is
     ~80 ms and `import json` a further ~40 ms, so the wall time the user sees
     is interpreter startup, not this script; nothing *inside* this file can
     change that, which is why the installer registers the hook as
     `python3 -S -E hook.py` (measured: ~89 ms -> ~57 ms of bare startup).
     Keep this module stdlib-only or those flags stop being safe.
  3. Append-only: one os.write() of one line to $LEDGER_DIR/YYYY-MM.jsonl,
     opened O_APPEND|O_CREAT|O_WRONLY, so concurrent sessions interleave
     without tearing.

Ledger dir: $PERMISSION_LEDGER_DIR, default ~/.claude/permission-ledger.
"""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone

DEFAULT_LEDGER_DIR = "~/.claude/permission-ledger"
ERROR_LOG_NAME = "hook-errors.log"

# Tools recorded by nobody: no filesystem, network or execution reach, so no
# allow/deny decision about them is ever meaningful. Their records are pure
# noise, and noise here is not free -- the digest ranks friction by group size,
# so a high-volume no-reach tool crowds out the groups worth acting on.
# AskUserQuestion reached 106 of 452 records (23%) before being listed.
IGNORED_TOOLS = frozenset(("AskUserQuestion",))

FILE_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep", "NotebookEdit")
PATH_KEYS = ("file_path", "path", "notebook_path")
BASH_TOOLS = ("Bash", "mcp__safe-bash__exec")
SUBCOMMAND_TOOLS = frozenset(
    ("git", "gh", "docker", "kubectl", "cargo", "npm", "uv", "nix", "systemctl")
)

# Only the first slice of a command string is tokenised. A 1 MB `command` must
# not cost us the 20 ms budget, and pattern_key only ever needs two tokens.
_TOKENIZE_HEAD = 4096

# Cap on any single stored string. The digest is taken over the *unredacted,
# untruncated* input, so request<->denial joins are unaffected; this only
# bounds ledger growth and keeps the regex pass cheap on huge commands.
MAX_STRING = 4096

# A hook that throws on every permission prompt would otherwise grow this file
# without bound; past the cap we stop appending and stay silent.
MAX_ERROR_LOG_BYTES = 1 << 20


def ledger_dir():
    return os.path.expanduser(
        os.environ.get("PERMISSION_LEDGER_DIR") or DEFAULT_LEDGER_DIR
    )


# ---------------------------------------------------------------------------
# redaction (cheap + fallible by design; stage 2 runs the thorough pass)
# ---------------------------------------------------------------------------
# Each entry is (compiled pattern, replacement). Replacements name the kind as
# [REDACTED:<kind>]; the kind vocabulary is ours, the spec only fixes the shape.
# Order matters: specific token shapes first, generic high-entropy blobs last.

_NOT_ALREADY = r"(?!\[REDACTED)"
_VALUE = r"(\"[^\"]*\"|'[^']*'|[^\s;&|]+)"

REDACTIONS = [
    # PEM blocks first: their base64 body would otherwise be chewed up by the
    # high-entropy rule and leave the BEGIN/END markers behind.
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
        "[REDACTED:private-key]",
    ),
    # ...and an unterminated block (e.g. cut by MAX_STRING) takes the rest.
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*"), "[REDACTED:private-key]"),
    # Known credential shapes.
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED:github-pat]"),
    (re.compile(r"\bgh[porsu]_[A-Za-z0-9]{20,}"), "[REDACTED:github-token]"),
    # Provider keys used on this host. No leading \b: the prefixes are distinctive
    # enough to match mid-word, and keys glued to a word character (`\nsk-...`
    # in a printf literal, `%3Dsk-...` in a URL) otherwise slip past the generic
    # rule below -- and their '-'/'_'-broken bodies past the high-entropy rule.
    # The Anthropic kind is not enumerated: api03/oat01/admin01 plus the OAuth
    # refresh token (ort01) and claude.ai session key (sid01) share the shape.
    (re.compile(r"sk-ant-[a-z]{2,8}\d{2}-[A-Za-z0-9_-]{20,}"), "[REDACTED:api-key]"),
    (re.compile(r"sk-(?:proj|svcacct|admin|None)-[A-Za-z0-9_-]{40,}"), "[REDACTED:api-key]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"), "[REDACTED:api-key]"),
    (re.compile(r"\bAKIA[0-9A-Z]{12,}"), "[REDACTED:aws-key]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{8,}"), "[REDACTED:slack-token]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"), "[REDACTED:google-key]"),
    (re.compile(r"(?i)\b(bearer\s+)" + _NOT_ALREADY + r"[A-Za-z0-9._~+/=-]{8,}"),
     r"\1[REDACTED:bearer-token]"),
    # --password / --token style flags, with either separator.
    (
        re.compile(
            r"(?i)(--(?:password|passwd|token|secret|api[-_]?key)[=\s]+)" + _NOT_ALREADY + _VALUE
        ),
        r"\1[REDACTED:flag-secret]",
    ),
    # FOO_TOKEN=..., API_KEY=..., PASSWORD=... (uppercase names only, so that
    # ordinary lowercase assignments are not swept up).
    (
        re.compile(r"\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)S?)=" + _NOT_ALREADY + _VALUE),
        r"\1=[REDACTED:env-secret]",
    ),
    # ...plus the standalone lowercase query/CLI form (`?token=`, `key=`), which
    # \b keeps from matching inside words like "monkey=".
    (
        re.compile(
            r"(?i)\b((?:api[-_]?key|key|token|secret|password|passwd)=)" + _NOT_ALREADY + _VALUE
        ),
        r"\1[REDACTED:env-secret]",
    ),
    # Last resort: any run of >40 base64/hex characters. The lookbehind excludes
    # "/" so that long filesystem paths are not mistaken for a blob.
    (
        re.compile(r"(?<![A-Za-z0-9+=/])[A-Za-z0-9+=]{41,}(?![A-Za-z0-9+=])"),
        "[REDACTED:high-entropy]",
    ),
]


def _redact_str(value):
    if len(value) > MAX_STRING:
        value = value[:MAX_STRING] + "...[truncated: %d chars total]" % len(value)
    for pattern, replacement in REDACTIONS:
        if pattern.search(value):
            value = pattern.sub(replacement, value)
    return value


def redact(value, _depth=0):
    """Recursively redact string values in a JSON-ish structure.

    Applies to values only (dict keys are left alone) and to the *stored*
    copy only -- never to the input the digest is computed from.
    """
    if _depth > 12:
        return "[REDACTED:too-deep]"
    if isinstance(value, str):
        return _redact_str(value)
    if isinstance(value, dict):
        return {k: redact(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]
    return value


# ---------------------------------------------------------------------------
# digest
# ---------------------------------------------------------------------------


def input_digest(tool_input):
    """sha256 over canonical JSON of the UNREDACTED input, first 16 hex chars.

    Canonical form: json.dumps(sort_keys=True, separators=(",", ":"),
    default=str) so key order and whitespace cannot split a group, and an
    unserialisable value degrades to its repr instead of raising.
    """
    try:
        canonical = json.dumps(
            tool_input, sort_keys=True, separators=(",", ":"), default=str
        )
    except Exception:
        canonical = repr(tool_input)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# pattern_key
# ---------------------------------------------------------------------------


_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_DURATION = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
# Prefixes stripped before the first "real" token. The spec names sudo,
# timeout N and VAR=value; `env` is included because VAR=value prefixes are
# usually written as `env FOO=bar cmd`.
_PREFIX_WORDS = frozenset(("sudo", "env"))


def _basename(token):
    return token.rsplit("/", 1)[-1] or token


def _strip_command_prefixes(tokens):
    i, n = 0, len(tokens)
    while i < n:
        token = tokens[i]
        if _ENV_ASSIGN.match(token):
            i += 1
            continue
        word = _basename(token)
        if word in _PREFIX_WORDS:
            i += 1
            continue
        if word == "timeout":
            i += 1
            while i < n and tokens[i].startswith("-"):
                i += 1
            if i < n and _DURATION.match(tokens[i]):
                i += 1
            continue
        break
    return tokens[i:]


def pattern_key(tool_name, tool_input):
    ti = tool_input if isinstance(tool_input, dict) else {}
    if tool_name in FILE_TOOLS:
        for key in PATH_KEYS:
            value = ti.get(key)
            if isinstance(value, str) and value:
                return "%s:%s" % (tool_name, os.path.dirname(value) or "/")
        return tool_name
    if tool_name in BASH_TOOLS:
        command = ti.get("command")
        if not isinstance(command, str):
            return tool_name
        tokens = _strip_command_prefixes(command[:_TOKENIZE_HEAD].split())
        if not tokens:
            return tool_name
        # `/usr/bin/git log` and `git log` are the same friction; group them.
        first = _basename(tokens[0])
        if first in SUBCOMMAND_TOOLS and len(tokens) > 1:
            return "%s:%s %s" % (tool_name, first, tokens[1])
        return "%s:%s" % (tool_name, first)
    # MCP tools and everything else: the tool name, verbatim.
    return tool_name


# ---------------------------------------------------------------------------
# record assembly
# ---------------------------------------------------------------------------


def _norm_suggestions(raw):
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rule = item.get("ruleValue")
        rule = rule if isinstance(rule, dict) else {}
        out.append(
            {
                "toolName": rule.get("toolName", item.get("toolName")),
                "ruleContent": rule.get("ruleContent", item.get("ruleContent")),
                "behavior": item.get("behavior"),
                "destination": item.get("destination"),
            }
        )
    return out


def _as_str(value, default=""):
    return value if isinstance(value, str) else default


def build_record(payload, now=None):
    payload = payload if isinstance(payload, dict) else {}
    now = now or datetime.now(timezone.utc)

    event_name = _as_str(payload.get("hook_event_name"))
    if event_name == "PermissionDenied":
        event = "denied"
    elif event_name == "PermissionRequest":
        event = "request"
    else:
        # Unknown/absent event name: infer from the denial-only fields.
        event = "denied" if ("tool_use_id" in payload or "reason" in payload) else "request"

    cwd = _as_str(payload.get("cwd"))
    tool_name = _as_str(payload.get("tool_name"))
    raw_input = payload.get("tool_input")
    # tool_input must stay a dict for downstream consumers; a non-dict payload
    # (string, list, null) is boxed under "_raw" rather than dropped.
    stored_input = raw_input if isinstance(raw_input, dict) else {"_raw": raw_input}

    record = {
        "ts": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "event": event,
        "session_id": _as_str(payload.get("session_id")),
        "prompt_id": _as_str(payload.get("prompt_id")),
        "cwd": cwd,
        "project": os.path.basename(os.path.normpath(cwd)) if cwd else "",
        "permission_mode": _as_str(payload.get("permission_mode")),
        "agent_type": _as_str(payload.get("agent_type")) or "main",
        "agent_id": payload.get("agent_id") if isinstance(payload.get("agent_id"), str) else None,
        "entrypoint": os.environ.get("CLAUDE_CODE_ENTRYPOINT") or "cli",
        "tool_name": tool_name,
        "tool_input": redact(stored_input),
        "input_digest": input_digest(raw_input),
        "pattern_key": _redact_str(pattern_key(tool_name, raw_input)),
        "suggestions": redact(_norm_suggestions(payload.get("permission_suggestions"))),
    }
    if event == "denied":
        record["tool_use_id"] = payload.get("tool_use_id")
        record["reason"] = payload.get("reason")
    return record


# ---------------------------------------------------------------------------
# write path
# ---------------------------------------------------------------------------


def append_line(directory, filename, line):
    """One os.write() of one newline-terminated line, O_APPEND."""
    if not os.path.isdir(directory):
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            os.chmod(directory, 0o700)  # defeat a permissive umask
        except OSError:
            pass
    path = os.path.join(directory, filename)
    existed = os.path.exists(path)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        if not existed:
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
        os.write(fd, line)
    finally:
        os.close(fd)


def emit(payload, now=None):
    # Checked before build_record so an ignored tool costs no redaction pass
    # and no digest. Both events are dropped: keeping the denial without its
    # request would leave an unjoinable half-record.
    if isinstance(payload, dict) and _as_str(payload.get("tool_name")) in IGNORED_TOOLS:
        return None
    # One instant for both the record and the month file: taking now() twice
    # could file a 23:59:59.999 record under the next month.
    now = now or datetime.now(timezone.utc)
    record = build_record(payload, now=now)
    line = json.dumps(record, ensure_ascii=False, default=str).replace("\n", " ")
    append_line(ledger_dir(), now.strftime("%Y-%m.jsonl"), (line + "\n").encode("utf-8", "replace"))
    return record


def _log_error(exc):
    try:
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        line = "%s\t%s\t%s\n" % (stamp, type(exc).__name__, exc)
        directory = ledger_dir()
        if not os.path.isdir(directory):
            os.makedirs(directory, mode=0o700, exist_ok=True)
        path = os.path.join(directory, ERROR_LOG_NAME)
        # A hook that fails on every prompt must not fill the disk.
        try:
            if os.path.getsize(path) > MAX_ERROR_LOG_BYTES:
                return
        except OSError:
            pass
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line.encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except BaseException:
        pass  # if even the error log is unwritable, stay silent


def main():
    try:
        # Bytes, not text: a payload carrying invalid UTF-8 should degrade to
        # replacement characters, not lose the whole record.
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        if not raw.strip():
            raise ValueError("empty stdin")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            # A hollow record would pollute the aggregate's counts; a payload
            # that is not a JSON object is a harness fault, so diagnose it in
            # hook-errors.log and write nothing to the ledger.
            raise TypeError("payload is %s, not an object" % type(payload).__name__)
        emit(payload)
    except BaseException as exc:  # noqa: BLE001 - a ledger fault must never surface
        _log_error(exc)
    return 0


if __name__ == "__main__":
    main()
    sys.exit(0)
