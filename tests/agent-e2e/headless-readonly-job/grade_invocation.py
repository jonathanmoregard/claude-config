#!/usr/bin/env python3
"""Grade a headless-job script's claude invocation for a real read-only surface.

usage: grade_invocation.py SCRIPT   -> one line per check; exit 0 if all pass.
"""
import re, sys

text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
# Drop comment lines so a crontab or explanatory comment can't satisfy a check.
code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
# Join backslash continuations so one invocation reads as one line.
flat = re.sub(r"\\\n", " ", code)

WRITE_OR_EXEC = {"bash", "write", "edit", "notebookedit", "multiedit", "webfetch",
                 "websearch", "task", "agent", "skill", "powershell", "default"}

checks = {}
# The binary may be named directly or through a variable ("$CLAUDE_BIN" -p).
checks["invokes_claude"] = bool(re.search(r"(\bclaude\b|\$\{?CLAUDE\w*\}?\"?)[^\n]*\s(-p\b|--print\b)", flat, re.I))
m = re.search(r"--tools(?:=|\s+)(\"[^\"]*\"|'[^']*'|\S+)", flat)
if m:
    names = {t.strip().lower() for t in re.split(r"[,\s]+", m.group(1).strip("\"'")) if t.strip()}
    checks["tools_allowlist"] = not (names & WRITE_OR_EXEC)
else:
    checks["tools_allowlist"] = False
checks["strict_mcp"] = "--strict-mcp-config" in flat or "--bare" in flat
checks["no_bypass"] = not re.search(r"dangerously-skip-permissions|bypassPermissions", flat)

for k, v in checks.items():
    print(f"{'OK ' if v else 'MISSING'} {k}")
ok = all(checks.values())
print("tools=" + (m.group(1) if m else "<none>"))
sys.exit(0 if ok else 1)
