#!/usr/bin/env bash
# An orchestrated mission whose main session has nothing left to do while a
# background subagent runs a ~5 min check, longer than the main session's own
# work, so the main session has to stop and wait. The Ralph Stop hook re-prompted the
# session every few seconds in exactly that state; check.sh grades whether it
# still does.
set -euo pipefail
WORK="${E2E_WORK:?}"
RUN="${E2E_RUN:?}"
BIN="${E2E_BIN:?}"

cp "$(dirname "$0")/../_lib/gh" "$BIN/gh"
chmod +x "$BIN/gh"

# Per-run token: the agent can only report it by actually waiting for the
# background subagent's result.
token="r$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n')"
printf '%s\n' "$token" > "$RUN/expected-result.txt"

cd "$WORK"
git init -q -b main
git config user.email e2e@example.invalid
git config user.name e2e

cat > textutil.py <<'PY'
"""Small text helpers."""


def shout(text: str) -> str:
    return text.upper()
PY

cat > test_textutil.py <<'PY'
from textutil import shout


def test_shout():
    assert shout("hi") == "HI"
PY

cat > integration.sh <<SH
#!/usr/bin/env bash
# Slow integration check. Prints one RESULT line at the end.
printf 'start %s\n' "\$(date +%s)" >> "$RUN/integration.log"
for i in \$(seq 1 20); do
  sleep 15
  echo "integration: step \$i/20"
done
printf 'done %s\n' "\$(date +%s)" >> "$RUN/integration.log"
echo "RESULT: $token"
SH
chmod +x integration.sh

git add -A
git commit -qm "initial"
