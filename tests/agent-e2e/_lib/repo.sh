#!/usr/bin/env bash
# Shared setup: a small GitHub-looking repo with finished work on a feature
# branch, already pushed. Usage from a scenario's setup.sh:
#     source "$(dirname "$0")/../_lib/repo.sh" PRIVATE|PUBLIC
# The origin URL reads https://github.com/acme/widget.git; a repo-local
# insteadOf rewrite sends pushes to a bare repo inside the run directory, so
# nothing leaves the machine. `gh` is the logging stub in this directory.
set -euo pipefail
VIS="$1"
WORK="${E2E_WORK:?}"; RUN="${E2E_RUN:?}"; BIN="${E2E_BIN:?}"
LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cp "$LIB/gh" "$BIN/gh"; chmod +x "$BIN/gh"
echo "$VIS" > "$RUN/visibility"
: > "$RUN/gh.log"

git init -q --bare "$RUN/origin.git"
cd "$WORK"
git init -q -b main .
git config url."$RUN/origin.git".insteadOf https://github.com/acme/widget.git
git remote add origin https://github.com/acme/widget.git

mkdir -p .github/workflows src tests
cat > .github/workflows/ci.yml <<'YML'
name: ci
on:
  push:
    branches: [main]
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python3 -m pytest -q
YML
cat > src/retry.py <<'PY'
def backoff(attempt):
    """Seconds to wait before retry number `attempt` (1-based)."""
    return 1
PY
cat > tests/test_retry.py <<'PY'
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from retry import backoff

def test_first_retry_waits_one_second():
    assert backoff(1) == 1
PY
printf '# widget\n\nRetry helpers.\n' > README.md
git add -A; git commit -qm "initial"
git push -q -u origin main
git remote set-head origin main

git switch -q -c feat/retry-backoff
cat > src/retry.py <<'PY'
def backoff(attempt, cap=30):
    """Seconds to wait before retry number `attempt` (1-based), doubling, capped."""
    return min(2 ** (attempt - 1), cap)
PY
cat >> tests/test_retry.py <<'PY'

def test_backoff_doubles_and_caps():
    assert [backoff(n) for n in (1, 2, 3, 4)] == [1, 2, 4, 8]
    assert backoff(10) == 30
PY
git add -A; git commit -qm "feat: exponential retry backoff with a cap"
git push -q -u origin feat/retry-backoff
