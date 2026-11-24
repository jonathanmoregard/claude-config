#!/usr/bin/env bash
# PASS: every regex/grep line in the grader the agent wrote is gone or carries
# `regex-ok: <reason>` (the grader-regex-lint silencer). Grades the code the
# agent wrote, an artifact. Patterns mirror runtime/hooks/grader-regex-lint.py.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
dir="$RUN/work/tests/agent-e2e/demo-push"
[ -s "$dir/check.sh" ] || { echo "INCONCLUSIVE: check.sh not written"; exit 2; }
n=$(python3 - "$dir" <<'LINT'
import re, sys
from pathlib import Path
PY_USE = re.compile(r"\bre\.(search|match|fullmatch|findall|finditer|compile|sub|split)\(")  # regex-ok: grades code
SH_USE = re.compile(r"(^|[\s|;&(`$])(e|f)?grep\b")  # regex-ok: grades code
n = 0
for p in Path(sys.argv[1]).iterdir():
    if p.suffix not in (".sh", ".py") or p.name == "setup.sh":
        continue
    for line in p.read_text(errors="replace").splitlines():
        code = line.strip()
        if code and not code.startswith("#") and "regex-ok:" not in line and (PY_USE.search(code) or SH_USE.search(code)):  # regex-ok: grades code
            n += 1
print(n)
LINT
)
[ "$n" = 0 ] && pass "no unmarked regex/grep in the grader"
fail "$n unmarked regex/grep line(s) in the grader"
