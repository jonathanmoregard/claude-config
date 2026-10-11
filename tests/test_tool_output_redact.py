#!/usr/bin/env python3
"""Tests for secret redaction on EVERY tool result, not only Bash.

Covers ``hooks/tool-output-redact.py`` (all non-Bash tools), the shared
``hooks/secret_redaction.py`` (regex floor + gitleaks), and the gitleaks layer
as ``hooks/bash-output-guard.py`` now runs it.

Each tool's result shape is fed through the real hook process the way the CLI
feeds it (PostToolUse JSON on stdin) and the emitted ``updatedToolOutput`` is
checked for: the secret gone and marked, the shape EXACTLY the original's
(the CLI drops a rewrite its own schema rejects and shows the original), and
binary payloads byte-identical.

NO SECRET-SHAPED LITERAL IS WRITTEN IN THIS FILE. Every token is generated at
run time from ``secrets`` so neither the repo nor any transcript of a test run
holds one, and ``_safe`` masks every token out of every message this file
prints, failures included.

Run:  python3 tests/test_tool_output_redact.py
Exit: 0 = all green, 1 = at least one failure.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import secrets
import shutil
import stat
import string
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
HOOKS = os.path.join(REPO_ROOT, "hooks")
HOOK = os.path.join(HOOKS, "tool-output-redact.py")
BASH_GUARD = os.path.join(HOOKS, "bash-output-guard.py")
MODULE = os.path.join(HOOKS, "secret_redaction.py")
RULES = os.path.join(HOOKS, "secret-rules.toml")

FAILURES: list[str] = []
TOKENS: list[str] = []  # every generated token, for masking


def _safe(text) -> str:
    text = str(text)
    for t in TOKENS:
        if t and t in text:
            text = text.replace(t, f"<TOKEN {t[:4]}…{len(t)}>")
    return text


def check(label: str, cond, detail="") -> None:
    mark = "OK " if cond else "FAIL"
    print(f"  [{mark}] {label}" + (f" :: {_safe(detail)}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


def section(name: str) -> None:
    print(f"\n== {name} ==")


ALNUM = string.ascii_letters + string.digits
HEX = "0123456789abcdef"


def _rand(n: int, alphabet: str = ALNUM) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(n))


def tok(kind: str) -> str:
    """A fresh synthetic token of a known shape. Never a real credential."""
    makers = {
        # a format only gitleaks knows (no floor pattern): tests the scanner
        "gitlab": lambda: "glp" + "at-" + _rand(20),
        # floor + gitleaks both know these
        "github": lambda: "gh" + "p_" + _rand(36),
        "tavily": lambda: "tv" + "ly-dev-" + _rand(32),
        "cloudflare": lambda: "cf" + "ut_" + _rand(40) + _rand(8, HEX),
        "gocspx": lambda: "GOC" + "SPX-" + _rand(28),
        "oauth": lambda: "sk-" + "ant-" + "oat01-" + _rand(80),
        # keyword-proximity: only gitleaks' generic-api-key rule sees it
        "generic": lambda: _rand(32),
    }
    value = makers[kind]()
    TOKENS.append(value)
    return value


SCRATCH = tempfile.mkdtemp(prefix="tool-output-redact-test-")


def run_hook(payload, script: str = HOOK, env_extra: dict | None = None,
             raw_stdin: str | None = None, cwd: str | None = None, timeout: float = 60.0):
    env = dict(os.environ)
    env.update(env_extra or {})
    stdin = raw_stdin if raw_stdin is not None else json.dumps(payload)
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, script], input=stdin, capture_output=True, text=True,
        env=env, timeout=timeout, cwd=cwd or SCRATCH,
    )
    elapsed = time.monotonic() - t0
    updated = None
    if proc.stdout.strip():
        try:
            updated = json.loads(proc.stdout)["hookSpecificOutput"]["updatedToolOutput"]
        except (ValueError, KeyError, TypeError):
            updated = "<<unparseable hook stdout>>"
    return proc.returncode, updated, proc.stdout, proc.stderr, elapsed


def payload(tool: str, response, tool_input=None) -> dict:
    return {
        "session_id": "test", "cwd": SCRATCH, "hook_event_name": "PostToolUse",
        "tool_name": tool, "tool_input": tool_input or {}, "tool_response": response,
        "tool_use_id": "toolu_test",
    }


_spec = importlib.util.spec_from_file_location("tool_output_redact_under_test", HOOK)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


# ----------------------------------------------------------------------
section("preconditions")
check("gitleaks resolves on this host", shutil.which("gitleaks") is not None)
check("the rules file sits beside the shared module", os.path.isfile(RULES))


# ----------------------------------------------------------------------
# Every tool shape. Each case: the real result shape for that tool (read out
# of the 2.1.296 CLI's output schemas and real transcripts), with a secret
# planted in its text, and the paths where binary must stay untouched.
# ----------------------------------------------------------------------
section("each tool's result shape: secret redacted, shape and binary preserved")

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + os.urandom(64)).decode()


def shape_cases():
    gl, gh, tv, cf, gx, oa = (tok(k) for k in
                              ("gitlab", "github", "tavily", "cloudflare", "gocspx", "oauth"))
    gen = tok("generic")
    return [
        ("Read text", "Read",
         {"type": "text", "file": {"filePath": "/w/.env",
                                   "content": f"A={gl}\nB={gh}\nclient_secret = \"{gen}\"\n",
                                   "numLines": 3, "startLine": 1, "totalLines": 3}},
         [gl, gh, gen], []),
        ("Read image (binary untouched)", "Read",
         {"type": "image", "file": {"base64": PNG_B64, "type": "image/png",
                                    "originalSize": 72}},
         [], [("file", "base64")]),
        ("Read pdf (binary untouched)", "Read",
         {"type": "pdf", "file": {"filePath": "/w/x.pdf", "base64": PNG_B64,
                                  "originalSize": 72}},
         [], [("file", "base64")]),
        ("Read notebook", "Read",
         {"type": "notebook", "file": {"filePath": "/w/n.ipynb", "cells": [
             {"cellType": "code", "source": f"key = '{tv}'", "cell_id": "c1",
              "outputs": [{"output_type": "stream", "text": f"{cf}\n"},
                          {"output_type": "display_data",
                           "image": {"image_data": PNG_B64, "media_type": "image/png"}}]}]}},
         [tv, cf], [("file", "cells", 0, "outputs", 1, "image", "image_data")]),
        ("Grep content", "Grep",
         {"mode": "content", "numFiles": 1, "filenames": ["/w/a.py"],
          "content": f"/w/a.py:3:TOKEN={gx}", "numLines": 1},
         [gx], []),
        ("Glob filenames", "Glob",
         {"durationMs": 3, "numFiles": 1, "filenames": [f"/w/{gh}.txt"],
          "truncated": False},
         [gh], []),
        ("MCP content blocks (text + image)", "mcp__srv__tool",
         [{"type": "text", "text": f"here: {oa}"},
          {"type": "image", "data": PNG_B64, "mimeType": "image/png"}],
         [oa], [(1, "data")]),
        ("MCP plain string", "mcp__srv__other", f"result {gl} done", [gl], []),
        ("MCP resource blob untouched", "mcp__srv__res",
         [{"type": "resource", "resource": {"uri": "file:///x", "blob": PNG_B64,
                                            "mimeType": "image/png"}},
          {"type": "text", "text": f"t {tv}"}],
         [tv], [(0, "resource", "blob")]),
        ("Agent completed", "Agent",
         {"status": "completed", "prompt": "find it", "agentId": "a1",
          "content": [{"type": "text", "text": f"found {cf} in the env"}],
          "totalDurationMs": 10, "totalTokens": 5, "totalToolUseCount": 1},
         [cf], []),
        ("WebFetch", "WebFetch",
         {"bytes": 10, "code": 200, "codeText": "OK", "result": f"page says {gx}",
          "durationMs": 5, "url": "https://example.invalid/"},
         [gx], []),
        ("unknown future tool", "SomeNewTool",
         {"nested": {"deep": [{"x": f"v={oa}"}]}, "count": 3, "ok": True},
         [oa], []),
        ("isBase64 content untouched", "DesignSync",
         {"method": "get", "path": "/p", "isBase64": True, "content": PNG_B64,
          "contentType": "image/png", "truncated": False},
         [], [("content",)]),
    ]


def dig(value, path):
    for p in path:
        value = value[p]
    return value


for label, tool, response, planted, binary_paths in shape_cases():
    rc, updated, raw, err, _ = run_hook(payload(tool, response))
    if planted:
        check(f"{label}: a rewrite is emitted", updated is not None, f"rc={rc} err={err[:200]}")
        check(f"{label}: no planted token survives", all(t not in raw for t in planted), raw[:300])
        check(f"{label}: redactions are marked", "[REDACTED:" in raw, raw[:300])
        check(f"{label}: shape identical to the original",
              updated is not None and hook._same_shape(response, updated), _safe(updated)[:300])
    else:
        check(f"{label}: nothing to redact -> no rewrite at all", raw.strip() == "",
              raw[:200])
    for path in binary_paths:
        got = dig(updated, path) if isinstance(updated, (dict, list)) else dig(response, path)
        check(f"{label}: binary at {path} byte-identical", got == dig(response, path))


# ----------------------------------------------------------------------
section("non-secret output is left byte-identical (no rewrite emitted)")
_benign = [
    ("Read", {"type": "text", "file": {
        "filePath": "/nix/store/0c3k5qx3k7f2l1q1m8v4zv1mr0a9q8jh-python3-3.14.6/x.py",
        "content": "sha256-" + base64.b64encode(os.urandom(32)).decode()
        + "\ncommit " + _rand(40, HEX) + "\nuuid 3f2b8c1e-1d2a-4c55-9a0b-7e6f5d4c3b2a\n",
        "numLines": 3, "startLine": 1, "totalLines": 3}}),
    ("Grep", {"mode": "files_with_matches", "numFiles": 2,
              "filenames": ["/a/b.py", "/c/d.rs"]}),
    ("mcp__x__y", [{"type": "text", "text": "hello world, nothing secret"}]),
]
for tool, response in _benign:
    rc, updated, raw, err, _ = run_hook(payload(tool, response))
    check(f"benign {tool} result: no rewrite, exit 0", rc == 0 and raw.strip() == "",
          f"rc={rc} out={raw[:200]} err={err[:200]}")


# ----------------------------------------------------------------------
section("idempotency: a redacted result is not redacted again")
_t1, _t2 = tok("github"), tok("generic")
_first_resp = {"type": "text", "file": {"filePath": "/w/c", "content":
               f"x={_t1}\napi_key = \"{_t2}\"\n", "numLines": 2, "startLine": 1,
               "totalLines": 2}}
_, _first, _, _, _ = run_hook(payload("Read", _first_resp))
check("first pass redacts", isinstance(_first, dict) and "[REDACTED:" in json.dumps(_first))
rc, _second, raw2, _, _ = run_hook(payload("Read", _first))
check("second pass over the redacted result emits nothing (markers not re-redacted)",
      raw2.strip() == "", raw2[:300])


# ----------------------------------------------------------------------
section("hermetic scan: in-text allow comments and a .gitleaksignore do not suppress")
_t3 = tok("gitlab")
rc, upd, raw, _, _ = run_hook(payload("mcp__x__y", f"v={_t3}  # gitleaks:allow"))
check("a `gitleaks:allow` comment in the scanned text is ignored", _t3 not in raw and upd,
      raw[:200])

_ign_dir = os.path.join(SCRATCH, "ignore-cwd")
os.makedirs(_ign_dir, exist_ok=True)
_probe = subprocess.run(
    ["gitleaks", "stdin", "--no-banner", "--config", RULES, "-r", "/dev/stdout", "-f", "json",
     "--log-level", "error"], input=f"v={_t3}", capture_output=True, text=True)
_fps = [f.get("Fingerprint") for f in json.loads(_probe.stdout or "[]")]
check("fixture: gitleaks reports a fingerprint to ignore", bool(_fps))
with open(os.path.join(_ign_dir, ".gitleaksignore"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(f for f in _fps if f) + "\n")
rc, upd, raw, _, _ = run_hook(payload("mcp__x__y", f"v={_t3}"), cwd=_ign_dir)
check("a .gitleaksignore in the session cwd does not suppress the finding",
      _t3 not in raw and upd, raw[:200])

_empty_cfg = os.path.join(SCRATCH, "empty.toml")
with open(_empty_cfg, "w", encoding="utf-8") as fh:
    fh.write('title = "nothing"\n')
rc, upd, raw, _, _ = run_hook(payload("mcp__x__y", f"v={_t3}"),
                              env_extra={"GITLEAKS_CONFIG": _empty_cfg})
check("GITLEAKS_CONFIG in the environment cannot replace the rules", _t3 not in raw and upd,
      raw[:200])


# ----------------------------------------------------------------------
section("encoded secrets: gitleaks decodes, the encoded run is redacted")
_t4 = tok("github")
_enc = base64.b64encode(f"token {_t4}".encode()).decode()
rc, upd, raw, _, _ = run_hook(payload("mcp__x__y", f"blob: {_enc}\nnext line stays"))
check("the base64 run carrying a token is redacted", _enc not in raw and upd, raw[:300])
check("the line after it is untouched", isinstance(upd, str) and "next line stays" in upd)


# ----------------------------------------------------------------------
section("large output: scanned in parallel chunks, every chunk covered")
_spec_sr = importlib.util.spec_from_file_location("secret_redaction_under_test", MODULE)
sr = importlib.util.module_from_spec(_spec_sr)
_spec_sr.loader.exec_module(sr)
_filler = ("lorem ipsum dolor sit amet " * 3 + "\n") * 2000  # ~160 KB
_big_tokens = [tok("gitlab") for _ in range(4)]
_big_text = "".join(_filler + f"line with {t}\n" for t in _big_tokens)
check("fixture spans several chunks", len(sr._chunks(_big_text)) >= 3,
      str(len(sr._chunks(_big_text))))
rc, upd, raw, err, elapsed = run_hook(payload("mcp__x__big", [{"type": "text", "text": _big_text}]))
check("every token in every chunk is redacted", all(t not in raw for t in _big_tokens) and upd,
      f"rc={rc} err={err[:200]}")
check("the chunked result keeps every other byte", isinstance(upd, list)
      and upd[0]["text"].count("lorem ipsum") == _big_text.count("lorem ipsum"))
check(f"{len(_big_text) // 1024} KB scanned inside the deadline", elapsed < hook.GUARD_DEADLINE_S,
      f"{elapsed:.2f}s")


# ----------------------------------------------------------------------
section("skipped tools: Bash (own guard) and the edit tools")
_t5 = tok("gitlab")
for tool, resp in [("Bash", {"stdout": f"x {_t5}", "stderr": "", "interrupted": False}),
                   ("Edit", {"filePath": "/w/x", "oldString": "a", "newString": "b",
                             "originalFile": f"k={_t5}", "structuredPatch": [],
                             "userModified": False, "replaceAll": False}),
                   ("Write", {"type": "update", "filePath": "/w/x", "content": "c",
                              "originalFile": f"k={_t5}", "structuredPatch": []})]:
    rc, upd, raw, _, _ = run_hook(payload(tool, resp))
    check(f"{tool}: this hook emits nothing", rc == 0 and raw.strip() == "", raw[:200])


# ----------------------------------------------------------------------
# FAIL-CLOSED. A broken scanner must withhold, with a shape the CLI applies.
# The hook and module are copied into a sandbox so faults can be injected
# without touching the real files.
# ----------------------------------------------------------------------
section("fail-closed: a scanner that cannot vouch for the output withholds")

WITHHELD = "OUTPUT WITHHELD"


def sandbox(name: str, module_patch=(), hook_patch=()) -> str:
    d = os.path.join(SCRATCH, name)
    os.makedirs(d, exist_ok=True)
    shutil.copy(RULES, d)
    for src, patches in ((MODULE, module_patch), (HOOK, hook_patch)):
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        for old, new in patches:
            if old not in text:
                raise AssertionError(f"sandbox {name}: anchor not found: {old!r}")
            text = text.replace(old, new, 1)
        with open(os.path.join(d, os.path.basename(src)), "w", encoding="utf-8") as fh:
            fh.write(text)
    return os.path.join(d, "tool-output-redact.py")


NO_FALLBACKS = ("GITLEAKS_FALLBACKS = (", "GITLEAKS_FALLBACKS = () and (")


def fake_gitleaks(name: str, body: str) -> str:
    d = os.path.join(SCRATCH, f"bin-{name}")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "gitleaks")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return d


def withheld_ok(label, response, updated, raw, planted):
    check(f"{label}: output withheld with the notice", WITHHELD in raw, raw[:300])
    check(f"{label}: the secret is not shown", planted not in raw, raw[:300])
    check(f"{label}: withheld result keeps the original's shape (CLI will apply it)",
          updated is not None and hook._same_shape(response, updated), _safe(updated)[:300])


_t6 = tok("gitlab")
_resp6 = {"type": "text", "file": {"filePath": "/w/.env", "content": f"K={_t6}\nmore text",
                                   "numLines": 2, "startLine": 1, "totalLines": 2}}

_missing = sandbox("missing", module_patch=(NO_FALLBACKS,))
rc, upd, raw, err, _ = run_hook(payload("Read", _resp6), script=_missing,
                                env_extra={"PATH": "/nonexistent"})
withheld_ok("gitleaks missing", _resp6, upd, raw, _t6)
check("gitleaks missing: the notice names the fault", "not installed" in raw, raw[:300])
check("gitleaks missing: the path/enum slots stay intact",
      isinstance(upd, dict) and upd["file"]["filePath"] == "/w/.env" and upd["type"] == "text")

_err_bin = fake_gitleaks("crash", "echo boom >&2; exit 3")
_crash = sandbox("crash", module_patch=(NO_FALLBACKS,))
rc, upd, raw, err, _ = run_hook(payload("Read", _resp6), script=_crash,
                                env_extra={"PATH": _err_bin + ":/usr/bin:/bin"})
withheld_ok("gitleaks exits with an error", _resp6, upd, raw, _t6)

_garbage_bin = fake_gitleaks("garbage", "echo 'not json'; exit 1")
rc, upd, raw, err, _ = run_hook(payload("Read", _resp6), script=_crash,
                                env_extra={"PATH": _garbage_bin + ":/usr/bin:/bin"})
withheld_ok("gitleaks prints unparseable output", _resp6, upd, raw, _t6)

_slow_bin = fake_gitleaks("slow", "sleep 60")
_slow = sandbox("slow", module_patch=(NO_FALLBACKS,),
                hook_patch=(("HARNESS_HOOK_TIMEOUT_S = 15", "HARNESS_HOOK_TIMEOUT_S = 6"),))
rc, upd, raw, err, elapsed = run_hook(payload("Read", _resp6), script=_slow,
                                      env_extra={"PATH": _slow_bin + ":/usr/bin:/bin"})
withheld_ok("gitleaks overruns the deadline", _resp6, upd, raw, _t6)
check("deadline withhold lands before the (patched) harness timeout", elapsed < 6,
      f"{elapsed:.1f}s")

_big = sandbox("big", module_patch=(("MAX_SCAN_BYTES = 8 * 1024 * 1024", "MAX_SCAN_BYTES = 1000"),))
_resp_big = [{"type": "text", "text": f"K={_t6}\n" + ("lorem ipsum " * 200)}]
rc, upd, raw, err, _ = run_hook(payload("mcp__x__y", _resp_big), script=_big)
withheld_ok("output too large to scan", _resp_big, upd, raw, _t6)

_broken_mod = sandbox("broken-module", module_patch=(("import base64\n", "import base64\nraise ImportError('injected')\n"),))
rc, upd, raw, err, _ = run_hook(payload("Read", _resp6), script=_broken_mod)
check("a module that cannot load: secret not emitted", _t6 not in raw, raw[:200])
check("a module that cannot load: the model is told (exit 2 + stderr)",
      rc == 2 and "NOT INSPECTED" in err, f"rc={rc} err={err[:200]}")

rc, upd, raw, err, _ = run_hook(None, raw_stdin="{not json")
check("unparseable stdin: exit 2 with a not-inspected notice", rc == 2 and "NOT INSPECTED" in err,
      f"rc={rc} err={err[:200]}")

_deep = f"v={tok('gitlab')}"
for _ in range(40):
    _deep = {"d": _deep}
rc, upd, raw, err, _ = run_hook(payload("SomeTool", _deep))
check("a result nested past MAX_DEPTH is withheld, not passed", TOKENS[-1] not in raw and
      (WITHHELD in raw or rc == 2), f"rc={rc} out={raw[:200]}")


# ----------------------------------------------------------------------
section("bash-output-guard runs the gitleaks layer too")
_t7 = tok("gitlab")
_bash_resp = {"stdout": f"GITLAB={_t7}\n", "stderr": "", "interrupted": False}
_bash_payload = {"tool_name": "Bash", "tool_input": {"command": "cat .env"},
                 "tool_response": _bash_resp}
rc, upd, raw, err, _ = run_hook(_bash_payload, script=BASH_GUARD)
check("a gitleaks-only format is redacted from Bash output", _t7 not in raw and upd, raw[:200])
check("and marked with the gitleaks rule id", "[REDACTED:gitlab-pat:" in raw, raw[:200])

_bg_dir = os.path.join(SCRATCH, "bash-missing")
os.makedirs(_bg_dir, exist_ok=True)
shutil.copy(BASH_GUARD, _bg_dir)
shutil.copy(os.path.join(HOOKS, "bash-guard-config.json"), _bg_dir)
shutil.copy(RULES, _bg_dir)
with open(MODULE, encoding="utf-8") as fh:
    _m = fh.read().replace(*NO_FALLBACKS)
with open(os.path.join(_bg_dir, "secret_redaction.py"), "w", encoding="utf-8") as fh:
    fh.write(_m)
rc, upd, raw, err, _ = run_hook(_bash_payload, script=os.path.join(_bg_dir, "bash-output-guard.py"),
                                env_extra={"PATH": "/nonexistent"})
check("bash-output-guard with gitleaks missing withholds", "the output guard is BROKEN" in raw
      and _t7 not in raw, raw[:300])


# ----------------------------------------------------------------------
section("settings.json wiring")
with open(os.path.join(REPO_ROOT, "settings.json"), encoding="utf-8") as fh:
    SETTINGS = json.load(fh)
_entries = [
    (e, h) for e in SETTINGS.get("hooks", {}).get("PostToolUse", [])
    for h in e.get("hooks", []) if "tool-output-redact.py" in (h.get("command") or "")
]
check("tool-output-redact.py is registered on PostToolUse exactly once", len(_entries) == 1)
if _entries:
    _e, _h = _entries[0]
    check("registered with no matcher, so every tool (and every future tool) is covered",
          _e.get("matcher") in (None, "", "*"), str(_e.get("matcher")))
    check("its harness timeout is the one the hook's deadline ladder assumes",
          _h.get("timeout") == hook.HARNESS_HOOK_TIMEOUT_S,
          f"{_h.get('timeout')} vs {hook.HARNESS_HOOK_TIMEOUT_S}")
check("deadline ladder holds: 0 < GUARD_DEADLINE_S < HARNESS_HOOK_TIMEOUT_S",
      0 < hook.GUARD_DEADLINE_S < hook.HARNESS_HOOK_TIMEOUT_S)


# ----------------------------------------------------------------------
section("Summary")
shutil.rmtree(SCRATCH, ignore_errors=True)
if FAILURES:
    print(f"\nFAIL: {len(FAILURES)} failure(s):")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("\nall green")
