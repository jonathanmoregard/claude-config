#!/usr/bin/env python3
"""End-to-end tests for secret redaction in tool results, and the write guard.

Covers ``hooks/tool-output-redact.py`` (every non-Bash tool), the gitleaks
layer as ``hooks/bash-output-guard.py`` runs it, ``hooks/secret_redaction.py``
and ``hooks/redaction-marker-write-guard.py``. Each hook runs as a real
process on the JSON the CLI sends. gitleaks' own quirks are tested once, in
``tests/test_gitleaks_driver.py``; here only their end-to-end effect is.

No secret-shaped literal is written in this file: every token is generated at
run time, and ``_safe`` masks tokens out of everything printed.

Run:  python3 tests/test_tool_output_redact.py
Exit: 0 = all green, 1 = at least one failure.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import secrets
import shutil
import stat
import string
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
HOOKS = os.path.join(REPO_ROOT, "hooks")
HOOK = os.path.join(HOOKS, "tool-output-redact.py")
BASH_GUARD = os.path.join(HOOKS, "bash-output-guard.py")
WRITE_GUARD = os.path.join(HOOKS, "redaction-marker-write-guard.py")
MODULE = os.path.join(HOOKS, "secret_redaction.py")
RULES = os.path.join(HOOKS, "secret-rules.toml")
SCRATCH = tempfile.mkdtemp(prefix="tool-output-redact-test-")
WITHHELD = "OUTPUT WITHHELD"
BASH_WITHHELD = "the output guard is BROKEN"
NO_GITLEAKS = {"SECRET_REDACTION_GITLEAKS": "/nonexistent/gitleaks"}

_spec = importlib.util.spec_from_file_location("tool_output_redact_under_test", HOOK)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)
sys.path.insert(0, HOOKS)
import gitleaks_driver  # noqa: E402
import secret_redaction as sr  # noqa: E402

with open(os.path.join(REPO_ROOT, "settings.json"), encoding="utf-8") as _fh:
    SETTINGS = json.load(_fh)

# ---- reporting ---------------------------------------------------------
FAILURES: list[str] = []
TOKENS: list[str] = []


def _safe(text) -> str:
    text = str(text)
    for t in TOKENS:
        if t in text:
            text = text.replace(t, f"<TOKEN {t[:4]}…{len(t)}>")
    return text


def check(label: str, cond, detail="") -> None:
    print(f"  [{'OK ' if cond else 'FAIL'}] {label}" + (f" :: {_safe(detail)}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


def section(name: str) -> None:
    print(f"\n== {name} ==")


# ---- fixtures -----------------------------------------------------------
ALNUM = string.ascii_letters + string.digits


def _rand(n: int, alphabet: str = ALNUM) -> str:
    """n random chars, the last a digit: gitleaks ignores letters-only values."""
    return "".join(secrets.choice(alphabet) for _ in range(n - 1)) + secrets.choice(string.digits)


TOKEN_MAKERS = {
    "gitlab": lambda: "glp" + "at-" + _rand(20),  # only gitleaks knows it: no floor to hide behind
    "github": lambda: "gh" + "p_" + _rand(36),
    "tavily": lambda: "tv" + "ly-dev-" + _rand(32),
    "cloudflare": lambda: "cf" + "ut_" + _rand(40) + _rand(8, "0123456789abcdef"),
    "gocspx": lambda: "GOC" + "SPX-" + _rand(28),
    "oauth": lambda: "sk-" + "ant-" + "oat01-" + _rand(80),
    "generic": lambda: _rand(31) + "k",  # generic-api-key: needs a keyword next to it
    "fireworks": lambda: "fw_" + _rand(24),
    "oauth-long": lambda: "sk-" + "ant-oat01-" + _rand(95) + "AA",
}


def tok(kind: str = "gitlab") -> str:
    """A fresh synthetic token of a known shape. Never a real credential."""
    value = TOKEN_MAKERS[kind]()
    TOKENS.append(value)
    return value


def b64(n: int) -> str:
    return base64.b64encode(os.urandom(n)).decode()


PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + os.urandom(64)).decode()


# ---- running hooks --------------------------------------------------------
class Result(NamedTuple):
    rc: int
    updated: object  # the emitted updatedToolOutput, or None
    stdout: str
    stderr: str
    seconds: float


def run_hook(payload, script: str = HOOK, env: dict | None = None,
             raw_stdin: str | None = None, cwd: str | None = None) -> Result:
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, script], input=raw_stdin if raw_stdin is not None else json.dumps(payload),
        capture_output=True, text=True, env={**os.environ, **(env or {})}, cwd=cwd or SCRATCH,
        timeout=120,
    )
    updated = None
    if proc.stdout.strip():
        try:
            updated = json.loads(proc.stdout)["hookSpecificOutput"]["updatedToolOutput"]
        except (ValueError, KeyError, TypeError):
            updated = "<<unparseable hook stdout>>"
    return Result(proc.returncode, updated, proc.stdout, proc.stderr, time.monotonic() - t0)


def post(tool: str, response, tool_input=None) -> dict:
    return {"session_id": "test", "cwd": SCRATCH, "hook_event_name": "PostToolUse",
            "tool_name": tool, "tool_input": tool_input or {}, "tool_response": response,
            "tool_use_id": "toolu_test"}


def bash(stdout: str, command: str = "cat .env") -> dict:
    return post("Bash", {"stdout": stdout, "stderr": "", "interrupted": False}, {"command": command})


def fake_gitleaks(name: str, body: str) -> dict:
    path = os.path.join(SCRATCH, f"gitleaks-{name}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return {"SECRET_REDACTION_GITLEAKS": path}


def sandbox(name: str, module_patch=(), hook_patch=()) -> str:
    """A copy of the hook and its modules with source patches applied."""
    d = os.path.join(SCRATCH, name)
    os.makedirs(d, exist_ok=True)
    shutil.copy(RULES, d)
    shutil.copy(os.path.join(HOOKS, "gitleaks_driver.py"), d)
    for src, patches in ((MODULE, module_patch), (HOOK, hook_patch)):
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        for old, new in patches:
            assert old in text, f"sandbox {name}: anchor not found: {old!r}"
            text = text.replace(old, new, 1)
        with open(os.path.join(d, os.path.basename(src)), "w", encoding="utf-8") as fh:
            fh.write(text)
    return os.path.join(d, "tool-output-redact.py")


def dig(value, path):
    for p in path:
        value = value[p]
    return value


# =========================================================================
section("preconditions")
check("gitleaks resolves on this host", gitleaks_driver.gitleaks_binary() is not None)
check("the rules file sits beside the shared module", os.path.isfile(RULES))

# -------------------------------------------------------------------------
# Every tool's result shape (from the 2.1.296 CLI's output schemas and real
# transcripts): the secret goes, the shape stays, binary stays byte-identical.
section("each tool's result shape")
gl, gh, tv, cf, gx, oa, gen = (tok(k) for k in
                               ("gitlab", "github", "tavily", "cloudflare", "gocspx", "oauth", "generic"))
SHAPES = [  # label, tool, response, planted tokens, binary paths
    ("Read text", "Read", {"type": "text", "file": {
        "filePath": "/w/.env", "content": f"A={gl}\nB={gh}\nclient_secret = \"{gen}\"\n",
        "numLines": 3, "startLine": 1, "totalLines": 3}}, [gl, gh, gen], []),
    ("Read image", "Read", {"type": "image", "file": {
        "base64": PNG_B64, "type": "image/png", "originalSize": 72}}, [], [("file", "base64")]),
    ("Read pdf", "Read", {"type": "pdf", "file": {
        "filePath": "/w/x.pdf", "base64": PNG_B64, "originalSize": 72}}, [], [("file", "base64")]),
    ("Read notebook", "Read", {"type": "notebook", "file": {"filePath": "/w/n.ipynb", "cells": [
        {"cellType": "code", "source": f"key = '{tv}'", "cell_id": "c1", "outputs": [
            {"output_type": "stream", "text": f"{cf}\n"},
            {"output_type": "display_data", "image": {"image_data": PNG_B64, "media_type": "image/png"}}]}]}},
     [tv, cf], [("file", "cells", 0, "outputs", 1, "image", "image_data")]),
    ("Grep content", "Grep", {"mode": "content", "numFiles": 1, "filenames": ["/w/a.py"],
                              "content": f"/w/a.py:3:TOKEN={gx}", "numLines": 1}, [gx], []),
    ("Glob filenames", "Glob", {"durationMs": 3, "numFiles": 1, "filenames": [f"/w/{gh}.txt"],
                                "truncated": False}, [gh], []),
    ("MCP text + image blocks", "mcp__srv__tool", [
        {"type": "text", "text": f"here: {oa}"},
        {"type": "image", "data": PNG_B64, "mimeType": "image/png"}], [oa], [(1, "data")]),
    ("MCP plain string", "mcp__srv__other", f"result {gl} done", [gl], []),
    ("MCP resource blob", "mcp__srv__res", [
        {"type": "resource", "resource": {"uri": "file:///x", "blob": PNG_B64, "mimeType": "image/png"}},
        {"type": "text", "text": f"t {tv}"}], [tv], [(0, "resource", "blob")]),
    ("Agent completed", "Agent", {"status": "completed", "prompt": "find it", "agentId": "a1",
                                  "content": [{"type": "text", "text": f"found {cf} in the env"}],
                                  "totalDurationMs": 10, "totalTokens": 5, "totalToolUseCount": 1}, [cf], []),
    ("WebFetch", "WebFetch", {"bytes": 10, "code": 200, "codeText": "OK", "result": f"page says {gx}",
                              "durationMs": 5, "url": "https://example.invalid/"}, [gx], []),
    ("unknown future tool", "SomeNewTool", {"nested": {"deep": [{"x": f"v={oa}"}]}, "count": 3,
                                            "ok": True}, [oa], []),
    ("isBase64 content", "DesignSync", {"method": "get", "path": "/p", "isBase64": True,
                                        "content": PNG_B64, "contentType": "image/png",
                                        "truncated": False}, [], [("content",)]),
]
for label, tool, response, planted, binary_paths in SHAPES:
    r = run_hook(post(tool, response))
    if planted:
        check(f"{label}: no planted token survives, redactions marked",
              r.updated is not None and all(t not in r.stdout for t in planted) and "[REDACTED:" in r.stdout,
              f"rc={r.rc} {r.stdout[:300]} {r.stderr[:200]}")
        check(f"{label}: shape identical to the original",
              r.updated is not None and hook._same_shape(response, r.updated), r.updated)
    else:
        check(f"{label}: nothing to redact -> no rewrite", r.stdout.strip() == "", r.stdout[:200])
    for path in binary_paths:
        got = dig(r.updated if isinstance(r.updated, (dict, list)) else response, path)
        check(f"{label}: binary at {path} byte-identical", got == dig(response, path))

# -------------------------------------------------------------------------
section("benign output and idempotency")
BENIGN = [
    ("Read", {"type": "text", "file": {
        "filePath": "/nix/store/0c3k5qx3k7f2l1q1m8v4zv1mr0a9q8jh-python3-3.14.6/x.py",
        "content": f"sha256-{b64(32)}\ncommit {_rand(40, '0123456789abcdef')}\n"
                   "uuid 3f2b8c1e-1d2a-4c55-9a0b-7e6f5d4c3b2a\n",
        "numLines": 3, "startLine": 1, "totalLines": 3}}),
    ("Grep", {"mode": "files_with_matches", "numFiles": 2, "filenames": ["/a/b.py", "/c/d.rs"]}),
    ("mcp__x__y", [{"type": "text", "text": "hello world, nothing secret"}]),
]
for tool, response in BENIGN:
    r = run_hook(post(tool, response))
    check(f"benign {tool} result: no rewrite, exit 0", r.rc == 0 and r.stdout.strip() == "", r)
first = run_hook(post("Read", {"type": "text", "file": {
    "filePath": "/w/c", "content": f"x={tok('github')}\napi_key = \"{tok('generic')}\"\n",
    "numLines": 2, "startLine": 1, "totalLines": 2}})).updated
check("first pass redacts", "[REDACTED:" in json.dumps(first))
check("a second pass over the redacted result emits nothing",
      run_hook(post("Read", first)).stdout.strip() == "")

# -------------------------------------------------------------------------
section("encoded secrets: the base64 run carrying a token is redacted")
for label, make in (("on line 1", lambda e: f"blob: {e}\nnext line stays"),
                    ("on line 3", lambda e: f"x\ny\nblob: {e}\nnext line stays"),
                    ("at the end of ~330 KB", lambda e: ("lorem ipsum dolor sit amet " * 3 + "\n") * 4000
                     + f"blob: {e}\nnext line stays")):
    enc = base64.b64encode(f"token {tok('github')}".encode()).decode()
    r = run_hook(post("mcp__x__y", make(enc)))
    check(f"{label}: run redacted, the next line kept",
          enc not in r.stdout and isinstance(r.updated, str) and "blob: [REDACTED:" in r.updated
          and r.updated.endswith("\nnext line stays"), r.stdout[:200])

# -------------------------------------------------------------------------
section("large and odd inputs end to end (quirk mechanics: test_gitleaks_driver.py)")
for magic in ("%PDF-1.4\n", "MZ\n"):
    t = tok()
    check(f"output starting {magic.strip()!r} is scanned (generic hook)",
          t not in run_hook(post("mcp__x__y", [{"type": "text", "text": f"{magic}K={t}"}])).stdout)
    check(f"output starting {magic.strip()!r} is scanned (bash guard)",
          t not in run_hook(bash(f"{magic}K={t}\n"), script=BASH_GUARD).stdout)
big_tokens = [tok() for _ in range(4)]
big = "".join(("lorem ipsum dolor sit amet " * 3 + "\n") * 2000 + f"line {t}\n" for t in big_tokens)
check("fixture spans several driver pieces", len(gitleaks_driver.pieces(big)) >= 3)
r = run_hook(post("mcp__x__big", [{"type": "text", "text": big}]))
check(f"{len(big) // 1024} KB: every token redacted, every other byte kept, inside the deadline",
      all(t not in r.stdout for t in big_tokens) and isinstance(r.updated, list)
      and r.updated[0]["text"].count("lorem") == big.count("lorem") and r.seconds < hook.GUARD_DEADLINE_S,
      f"rc={r.rc} {r.seconds:.1f}s")
many = [TOKEN_MAKERS["gitlab"]() for _ in range(12000)]  # not in TOKENS: none is printed
r = run_hook(post("mcp__x__y", " ".join(many)))
check(f"12,000 distinct tokens on one line: all redacted, not withheld ({r.seconds:.1f}s)",
      r.updated is not None and WITHHELD not in r.stdout and not any(t in r.stdout for t in many),
      f"rc={r.rc} {r.seconds:.1f}s")

# -------------------------------------------------------------------------
# secret-rules.toml: prefix rules fire on a token, not inside a base64 blob
# (`\b` holds after `/` and `+`; 8 `fw_` hits in Codex reasoning blobs).
section("custom prefix rules: token boundary, not mid-base64")


def rule_hits(text: str) -> list[str]:
    return list(gitleaks_driver.find_secrets([text], rules=RULES, binary=gitleaks_driver.gitleaks_binary())[0].values())


for rule, value in (("fireworks-api-key", tok("fireworks")), ("tavily-api-key", tok("tavily")),
                    ("anthropic-oauth-token", tok("oauth-long"))):
    for label, text in (("env assignment", f"KEY={value}\n"), ("quoted value", f'key: "{value}"'),
                        ("start of input", value)):
        check(f"{rule}: fires on {label}", rule in rule_hits(text))
    for sep in ("/", "+", ""):
        check(f"{rule}: does NOT fire mid-base64 (after {sep or 'an alnum char'!r})",
              rule not in rule_hits(b64(60)[:40] + sep + value + b64(30)))
check("fireworks-api-key: an over-long fw_ run (a blob, not a key) does not fire",
      "fireworks-api-key" not in rule_hits(f"x fw_{_rand(80)} y"))

# -------------------------------------------------------------------------
section("skipped tools: Bash (own guard) and the edit tools")
t = tok()
for tool, response in (("Bash", {"stdout": f"x {t}", "stderr": "", "interrupted": False}),
                       ("Edit", {"filePath": "/w/x", "oldString": "a", "newString": "b",
                                 "originalFile": f"k={t}", "structuredPatch": [],
                                 "userModified": False, "replaceAll": False}),
                       ("Write", {"type": "update", "filePath": "/w/x", "content": "c",
                                  "originalFile": f"k={t}", "structuredPatch": []})):
    r = run_hook(post(tool, response))
    check(f"{tool}: this hook emits nothing", r.rc == 0 and r.stdout.strip() == "", r.stdout[:200])

# -------------------------------------------------------------------------
# Fail closed: a scanner that cannot vouch for the output withholds, in a shape
# the CLI applies (a refused rewrite shows the original).
section("fail-closed: the scanner cannot vouch -> withheld")
t = tok()
READ = {"type": "text", "file": {"filePath": "/w/.env", "content": f"K={t}\nmore text",
                                 "numLines": 2, "startLine": 1, "totalLines": 2}}
MCP_BIG = [{"type": "text", "text": f"K={t}\n" + "lorem ipsum " * 200}]
slow_hook = sandbox("slow", hook_patch=(("HARNESS_HOOK_TIMEOUT_S = 15", "HARNESS_HOOK_TIMEOUT_S = 6"),))
big_limit_hook = sandbox("big", module_patch=(("MAX_SCAN_BYTES = 8 * 1024 * 1024", "MAX_SCAN_BYTES = 1000"),))
FAULTS = [  # label, script, env, response
    ("gitleaks missing", HOOK, NO_GITLEAKS, READ),
    ("gitleaks exits with an error", HOOK, fake_gitleaks("crash", "echo boom >&2; exit 3"), READ),
    ("gitleaks prints unparseable output", HOOK, fake_gitleaks("garbage", "echo 'not json'"), READ),
    ("gitleaks overruns the deadline", slow_hook, fake_gitleaks("slow", "sleep 60"), READ),
    ("output too large to scan", big_limit_hook, None, MCP_BIG),
]
for label, script, env, response in FAULTS:
    r = run_hook(post("mcp__x__y" if response is MCP_BIG else "Read", response), script=script, env=env)
    check(f"{label}: withheld, secret not shown, original shape kept (exit 0)",
          r.rc == 0 and WITHHELD in r.stdout and t not in r.stdout
          and r.updated is not None and hook._same_shape(response, r.updated), f"rc={r.rc} {r.stdout[:200]}")
    if script is slow_hook:
        check("the deadline withhold lands before the (patched) 6 s harness timeout", r.seconds < 6,
              f"{r.seconds:.1f}s")
r = run_hook(post("Read", READ), env=NO_GITLEAKS)
check("withhold: the notice names the fault", "not installed" in r.stdout)
check("withhold: path and enum slots keep their values",
      isinstance(r.updated, dict) and r.updated["file"]["filePath"] == "/w/.env"
      and r.updated["type"] == "text")

# A token that is the WHOLE string (no whitespace) is withheld too, as a rewrite.
for label, tool, make in (
        ("Read of a token-only file", "Read", lambda v: {"type": "text", "file": {
            "filePath": "/w/key", "content": v, "numLines": 1, "startLine": 1, "totalLines": 1}}),
        ("MCP bare-token block", "mcp__x__y", lambda v: [{"type": "text", "text": v}]),
        ("MCP bare-token string", "mcp__x__z", lambda v: v)):
    v = tok()
    r = run_hook(post(tool, make(v)), env=NO_GITLEAKS)
    check(f"gitleaks missing, {label}: withheld as a rewrite (exit 0)",
          r.rc == 0 and WITHHELD in r.stdout and v not in r.stdout, f"rc={r.rc}")

# Depth is not cut: a deep token is redacted in place, or withheld with the rest.
deep_tok = tok()
deep: object = f"v={deep_tok}"
for _ in range(40):
    deep = {"d": deep}
deep = {"deep": deep, "note": "shallow free text"}
for label, env, want in (("gitleaks present", None, "[REDACTED:gitlab-pat:"),
                         ("gitleaks missing", NO_GITLEAKS, WITHHELD)):
    r = run_hook(post("SomeTool", deep), env=env)
    check(f"{label}: a token 41 levels down is not emitted, shape kept",
          r.rc == 0 and deep_tok not in r.stdout and want in r.stdout and hook._same_shape(deep, r.updated),
          f"rc={r.rc} {r.stdout[:200]}")

r = run_hook(post("Read", READ), script=sandbox(
    "broken-module", module_patch=(("import re\n", "import re\nraise ImportError('injected')\n"),)))
check("a module that cannot load: not emitted, the model is told (exit 2)",
      r.rc == 2 and t not in r.stdout and "NOT INSPECTED" in r.stderr, f"rc={r.rc} {r.stderr[:200]}")
r = run_hook(None, raw_stdin="{not json")
check("unparseable stdin: exit 2 with a not-inspected notice", r.rc == 2 and "NOT INSPECTED" in r.stderr)

# -------------------------------------------------------------------------
section("bash-output-guard runs the same detector")
t = tok()
r = run_hook(bash(f"GITLAB={t}\n"), script=BASH_GUARD)
check("a gitleaks-only format is redacted from Bash output, marked with its rule id",
      t not in r.stdout and "[REDACTED:gitlab-pat:" in r.stdout, r.stdout[:200])
r = run_hook(bash(f"GITLAB={t}\n"), script=BASH_GUARD, env=NO_GITLEAKS)
check("bash-output-guard with gitleaks missing withholds", BASH_WITHHELD in r.stdout and t not in r.stdout)

# -------------------------------------------------------------------------
# The regression redaction creates: the model sees a marker where the file holds
# a secret, and writing back what it saw must not replace the secret.
section("write guard: a marker is never written over a secret")
wdir = os.path.join(SCRATCH, "wg")
os.makedirs(wdir)
env_file = os.path.join(wdir, "app.env")
notes = os.path.join(wdir, "notes.md")
secret_tok = tok()
env_text = f"APP_ENV=staging\nGITLAB_TOKEN={secret_tok}\nLOG_LEVEL=info\n"
LONG = "[REDACTED:" + "github-token:40chars]"
SHORT = "[REDACTED:" + "github-token]"  # the aggregator's historic form
with open(env_file, "w", encoding="utf-8") as fh:
    fh.write(env_text)
with open(notes, "w", encoding="utf-8") as fh:
    fh.write(f"Redaction looks like {LONG} in output.\nOlder form: {SHORT}\n")

seen = run_hook(post("Read", {"type": "text", "file": {
    "filePath": env_file, "content": env_text, "numLines": 3, "startLine": 1, "totalLines": 3}})).updated
seen = seen["file"]["content"] if isinstance(seen, dict) else ""
marker = seen.splitlines()[1].split("=", 1)[1] if "[REDACTED:" in seen else "?"
check("fixture: the model's view of the file is redacted", marker.startswith("[REDACTED:") and secret_tok not in seen)


def decide(tool: str, tool_input: dict | None = None, raw_stdin: str | None = None) -> str | None:
    r = run_hook({"tool_name": tool, "tool_input": tool_input, "hook_event_name": "PreToolUse"},
                 script=WRITE_GUARD, raw_stdin=raw_stdin)
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] if r.stdout.strip() else None


new_md = os.path.join(wdir, "new.md")
WRITES = [  # label, tool, input, expected decision
    ("Read -> Write of the redacted view", "Write",
     {"file_path": env_file, "content": seen.replace("LOG_LEVEL=info", "LOG_LEVEL=debug")}, "deny"),
    ("Write with the SHORT marker over the secret", "Write",
     {"file_path": env_file, "content": f"GITLAB_TOKEN={SHORT}\n"}, "deny"),
    ("Edit that leaves the secret line alone", "Edit",
     {"file_path": env_file, "old_string": "LOG_LEVEL=info", "new_string": "LOG_LEVEL=debug"}, None),
    ("Edit that adds a marker line", "Edit",
     {"file_path": env_file, "old_string": "LOG_LEVEL=info", "new_string": f"LOG_LEVEL=info\nB={marker}"}, "deny"),
    ("Edit that adds a SHORT marker", "Edit",
     {"file_path": env_file, "old_string": "LOG_LEVEL=info", "new_string": f"LOG_LEVEL=info\nK={SHORT}"}, "deny"),
    ("Edit whose old_string is the redacted view (fails on its own)", "Edit",
     {"file_path": env_file, "old_string": f"GITLAB_TOKEN={marker}", "new_string": f"GITLAB_TOKEN={marker}\nX=1"},
     None),
    ("MultiEdit adding a marker in any step", "MultiEdit", {"file_path": env_file, "edits": [
        {"old_string": "APP_ENV=staging", "new_string": "APP_ENV=prod"},
        {"old_string": "LOG_LEVEL=info", "new_string": f"LOG_LEVEL=info\nK={marker}"}]}, "deny"),
    ("Edit around a marker the file already holds", "Edit",
     {"file_path": notes, "old_string": f"like {LONG} in", "new_string": f"like {LONG} in tool"}, None),
    ("Edit around a SHORT marker the file already holds", "Edit",
     {"file_path": notes, "old_string": f"form: {SHORT}", "new_string": f"form, still: {SHORT}"}, None),
    ("Write creating a new file with a marker", "Write", {"file_path": new_md, "content": f"x {LONG}"}, "deny"),
    ("placeholder spellings are not markers", "Write",
     {"file_path": new_md, "content": "[REDACTED:<rule>:<n>chars] and [REDACTED:<rule>]"}, None),
    ("NotebookEdit adding a marker", "NotebookEdit",
     {"notebook_path": os.path.join(wdir, "n.ipynb"), "cell_id": "c1", "new_source": f"k = '{LONG}'"}, "deny"),
    ("no marker in the input (the file is never read)", "Write",
     {"file_path": env_file, "content": "no markers here\n" * 2000}, None),
]
for label, tool, tool_input, want in WRITES:
    check(f"{label}: {want or 'allowed'}", decide(tool, tool_input) == want)
with open(env_file, encoding="utf-8") as fh:
    check("the secret is still on disk", secret_tok in fh.read())
locked = os.path.join(wdir, "locked.env")
with open(locked, "w", encoding="utf-8") as fh:
    fh.write("x\n")
os.chmod(locked, 0)
check("an unreadable target with a marker in the input is denied (fail-safe)",
      decide("Write", {"file_path": locked, "content": f"T={LONG}"}) == "deny" or os.access(locked, os.R_OK))
os.chmod(locked, 0o600)
check("unparseable input carrying a marker is denied",
      decide("Write", raw_stdin='{"tool_name": "Write", [REDACTED:x:3chars] broken') == "deny")

# -------------------------------------------------------------------------
section("settings.json wiring")
post_hooks = [(e, h) for e in SETTINGS["hooks"]["PostToolUse"] for h in e["hooks"]
              if "tool-output-redact.py" in h.get("command", "")]
pre_hooks = [(e, h) for e in SETTINGS["hooks"]["PreToolUse"] for h in e["hooks"]
             if "redaction-marker-write-guard.py" in h.get("command", "")]
check("tool-output-redact.py registered once on PostToolUse, no matcher (every future tool too)",
      len(post_hooks) == 1 and post_hooks[0][0].get("matcher") in (None, "", "*"))
check("its harness timeout is the one the hook's deadline assumes",
      post_hooks[0][1].get("timeout") == hook.HARNESS_HOOK_TIMEOUT_S > hook.GUARD_DEADLINE_S > 0)
check("the write guard registered once on PreToolUse, covering every file tool",
      len(pre_hooks) == 1 and all(re.search(pre_hooks[0][0]["matcher"], t)
                                  for t in ("Write", "Edit", "MultiEdit", "NotebookEdit")))

# =========================================================================
section("Summary")
shutil.rmtree(SCRATCH, ignore_errors=True)
if FAILURES:
    print(f"\nFAIL: {len(FAILURES)} failure(s):")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("\nall green")
