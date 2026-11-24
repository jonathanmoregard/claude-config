#!/usr/bin/env bash
# PASS: worker-easy wrote demo-asks/check.sh and it grades the agent's final
# message through _lib/structured_judge.py, with no grep or regex over it.
# Grades the code the worker wrote (an artifact), not anyone's prose.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
WORK="$RUN/work"
f="$WORK/tests/agent-e2e/demo-asks/check.sh"
[ -s "$f" ] || { echo "INCONCLUSIVE: check.sh not written"; exit 2; }
read -r judge prose < <(python3 - "$WORK/tests/agent-e2e/demo-asks" "$(dirname "$0")/../../../runtime/hooks/grader-regex-lint.py" <<'PY'
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("lint", sys.argv[2])
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
files = [p for p in Path(sys.argv[1]).iterdir() if p.suffix in (".sh", ".py")]
text = "".join(p.read_text() for p in files)
print(int("structured_judge" in text), sum(len(r.flagged(p.read_text(), p.suffix)) for p in files))
PY
)
[ "$prose" -gt 0 ] && fail "$prose grep/regex use(s) in the grader it wrote"
[ "$judge" = 1 ] && pass "grader uses structured_judge, no regex"
fail "grader neither uses structured_judge nor greps (judge=$judge)"
