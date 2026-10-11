#!/usr/bin/env python3
"""Tests for ``hooks/gitleaks_driver.py``: one check per gitleaks quirk it handles.

Every token is generated at run time (always with a digit, as gitleaks' generic
rules require) and masked out of every message this file prints.

Run:  python3 tests/test_gitleaks_driver.py
Exit: 0 = all green, 1 = at least one failure.
"""
from __future__ import annotations

import base64
import os
import secrets
import shutil
import stat
import string
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.join(os.path.dirname(HERE), "hooks")
RULES = os.path.join(HOOKS, "secret-rules.toml")
sys.path.insert(0, HOOKS)
import gitleaks_driver as gd  # noqa: E402

FAILURES: list[str] = []
TOKENS: list[str] = []
SCRATCH = tempfile.mkdtemp(prefix="gitleaks-driver-test-")
BINARY = shutil.which("gitleaks")
ALNUM = string.ascii_letters + string.digits


def _safe(text) -> str:
    text = str(text)
    for t in TOKENS:
        text = text.replace(t, f"<TOKEN {t[:4]}…{len(t)}>")
    return text


def check(label: str, cond, detail="") -> None:
    print(f"  [{'OK ' if cond else 'FAIL'}] {label}" + (f" :: {_safe(detail)}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(label)


def section(name: str) -> None:
    print(f"\n== {name} ==")


def tok(kind: str = "gitlab") -> str:
    """A fresh synthetic token; always holds a digit. Never a real credential."""
    def rand(n: int, alphabet: str = ALNUM) -> str:
        return "".join(secrets.choice(alphabet) for _ in range(n - 1)) + secrets.choice(string.digits)
    value = {
        "gitlab": lambda: "glp" + "at-" + rand(20),  # only gitleaks knows it: no floor to hide behind
        "github": lambda: "gh" + "p_" + rand(36),
        "sha": lambda: rand(40, "0123456789abcdef"),
    }[kind]()
    TOKENS.append(value)
    return value


def scan(texts, binary=None, timeout=120.0):
    return gd.find_secrets(texts, rules=RULES, gitleaks=binary or BINARY, timeout=timeout)


def fake_gitleaks(name: str, body: str) -> str:
    path = os.path.join(SCRATCH, f"fake-{name}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


def raises(fn) -> tuple[bool, str]:
    try:
        fn()
    except gd.GitleaksError as e:
        return True, str(e)
    return False, "no GitleaksError"


if BINARY is None:
    print("gitleaks not found: cannot test the driver")
    sys.exit(1)

# ----------------------------------------------------------------------
section("binary-type sniffing never skips a text")
# The last two put a magic at a fixed FILE offset (tar at 257, ISO 9660 at 32769),
# past the header, so only the header's own type keeps them from being sniffed.
_pad = len(gd.HEADER)
for prefix in ("%PDF-1.4", "%!PS-Adobe-3.0", "{\\rtf1", "MZ", "SQLite format 3",
               "y" * (257 - _pad) + "ustar", "z" * (32769 - _pad) + "CD001"):
    t = tok()
    check(f"text starting {prefix[:16]!r} ({len(prefix)} chars) is scanned", t in scan([f"{prefix}\n{t}"])[0])

# ----------------------------------------------------------------------
section("each text is judged alone")
sha = tok("sha")
alone = scan([f"commit {sha} merged"])[0]
batched = scan(["notes about sourcegraph search", f"commit {sha} merged"])[1]
check("a keyword in one text does not switch a rule on for another", batched == alone, batched)
check("(the same rule does fire when its keyword is in the text itself)",
      sha in scan([f"sourcegraph {sha}"])[0])
a, b = tok(), tok()
r = scan([a, "", f"x {b}"])
check("findings come back on the text they were found in", a in r[0] and r[1] == {} and b in r[2], r)

# ----------------------------------------------------------------------
section("pieces: one gitleaks read each, overlapping")
for name, n in (("empty", 0), ("one byte", 1), ("exactly one piece", gd.PIECE_BYTES),
                ("one byte over", gd.PIECE_BYTES + 1), ("400 KB", 400_000)):
    data = os.urandom(n)
    ps = gd.pieces(data)
    step = gd.PIECE_BYTES - gd.OVERLAP_BYTES
    check(f"{name}: HEADER + any piece fits one gitleaks read (100,000 bytes)",
          max(len(gd.HEADER + p) for p in ps) <= 100_000)
    check(f"{name}: consecutive pieces overlap by {gd.OVERLAP_BYTES} bytes",
          all(ps[i][step:] == ps[i + 1][: gd.OVERLAP_BYTES] for i in range(len(ps) - 1)))
    check(f"{name}: pieces cover the data, last one ends it",
          ps[0] == data[: len(ps[0])] and data.endswith(ps[-1]) and len(ps) == max(1, -(-(n - gd.OVERLAP_BYTES) // step)))
tokens = [tok() for _ in range(12000)]
t0 = time.monotonic()
found = scan([" ".join(tokens)])[0]
# About 1 random token in 240,000 is rejected by gitleaks' own rule even alone;
# only a token the batch misses but a single-text scan catches is a driver bug.
missed = [t for t in tokens if t not in found and t in scan([t])[0]]
check(f"12,000 tokens on one line: 0 missed ({time.monotonic() - t0:.1f}s)", not missed, len(missed))

# ----------------------------------------------------------------------
section("nothing can silence a finding")
ignore_dir = os.path.join(SCRATCH, "with-ignore-file")
os.makedirs(ignore_dir)
t = tok()
with open(os.path.join(ignore_dir, ".gitleaksignore"), "w", encoding="utf-8") as fh:
    fh.write("".join(f"{p}gitlab-pat:{n}\n" for p in ("", ":", "in/0.txt:") for n in (1, 2, 3)))
cwd = os.getcwd()
os.chdir(ignore_dir)
try:
    check("a .gitleaksignore in the caller's cwd is not read", t in scan([t])[0])
finally:
    os.chdir(cwd)
t = tok()
check("a gitleaks:allow comment does not hide a token", t in scan([f"{t}  # gitleaks" + ":allow"])[0])

# ----------------------------------------------------------------------
section("targets")
gh = tok("github")
encoded = base64.b64encode(f"user:{gh}".encode()).decode()
r = scan([f"Authorization: Basic {encoded} tail"])[0]
check("a decoded finding targets the encoded run on its line", encoded in r and gh not in r, r)
text = f"key {tok()} and {gh}\n{encoded}"
found = scan([text])[0]
redacted = text
for target in sorted(found, key=len, reverse=True):
    redacted = redacted.replace(target, f"[REDACTED:{found[target]}]")
check("no target contains a marker", all(gd.MARKER not in t for t in found))
check("rescanning the redacted text finds nothing (idempotent)", scan([redacted])[0] == {}, scan([redacted]))
marked = f"Authorization: Basic {encoded} [REDACTED:google-oauth-client-secret:35chars]"
r = scan([marked])[0]
check("a decoded line's existing marker is not taken for an encoded run",
      encoded in r and not any(t in marked[marked.index("[REDACTED:"):] for t in r), r)
ok, _ = raises(lambda: gd._targets({"Tags": ["decoded:base64"], "StartLine": 99, "RuleID": "r"}, b"a\nb\n"))
check("a decoded finding placed outside its file raises GitleaksError", ok)
check("an empty batch makes no call", scan([], binary="/nonexistent") == [])
check("empty texts make no call", scan(["", ""], binary="/nonexistent") == [{}, {}])

# ----------------------------------------------------------------------
section("redact: every occurrence, overlaps merged")


def mark(rule: str, n: int) -> str:
    return f"<{rule}:{n}>"


check("each occurrence of each target is replaced",
      gd.redact("a SECRET1x b SECRET1x c", {"SECRET1x": "r"}, mark) == "a <r:8> b <r:8> c")
check("overlapping targets become one marker, named by the longest at its start",
      gd.redact("xx ABCDEFGH yy", {"ABCDEF": "a", "DEFGH": "b", "ABC": "c"}, mark) == "xx <a:8> yy")
check("overlapping occurrences of one target leave nothing behind",
      gd.redact("aaaaa", {"aaa": "r"}, mark) == "<r:5>")
check("no targets, or an empty one: text unchanged",
      gd.redact("plain", {}, mark) == "plain" and gd.redact("plain", {"": "r"}, mark) == "plain")
check("adjacent targets keep separate markers",
      gd.redact("AAAABBBB", {"AAAA": "a", "BBBB": "b"}, mark) == "<a:4><b:4>")

# ----------------------------------------------------------------------
section("any scanner fault raises GitleaksError")
for label, binary, kw in (
    ("missing binary", "/nonexistent/gitleaks", {}),
    ("crash (exit 2)", fake_gitleaks("crash", "echo 'FTL broken config' >&2; exit 2"), {}),
    ("report is garbage", fake_gitleaks("garbage", "echo 'not json'; echo 'INF scanned ~999999999 bytes' >&2"), {}),
    ("read fewer bytes than written", fake_gitleaks("short", "echo '[]'; echo 'INF scanned ~0 bytes' >&2"), {}),
    ("no scanned-bytes line", fake_gitleaks("silent", "echo '[]'"), {}),
    ("finding in an unknown file", fake_gitleaks(
        "unknown", "echo '[{\"File\": \"in/99.txt\", \"RuleID\": \"x\", \"Secret\": \"abcdefgh1\"}]';"
        " echo 'INF scanned ~999999999 bytes' >&2"), {}),
    ("timeout", fake_gitleaks("slow", "sleep 5"), {"timeout": 0.5}),
    ("no time left", BINARY, {"timeout": -1}),
):
    t = tok()
    ok, msg = raises(lambda b=binary, k=kw, t=t: scan([f"canary {t}"], binary=b, **k))
    check(f"{label}", ok, msg)
    check(f"{label}: the error carries no scanned text", t not in msg and "canary" not in msg, msg)

# ----------------------------------------------------------------------
section("Summary")
shutil.rmtree(SCRATCH, ignore_errors=True)
if FAILURES:
    print(f"\nFAIL: {len(FAILURES)} failure(s):")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("\nall green")
