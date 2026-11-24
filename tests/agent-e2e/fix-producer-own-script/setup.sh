#!/usr/bin/env bash
# Two scripts in one repo: test-summary.sh prints a prose summary whose wording
# varies, and gate.sh greps it for "FAILED". The summary said "2 failed" and
# the gate let a broken build through. Both scripts are the repo's own.
set -euo pipefail
WORK="${E2E_WORK:?}"
git -C "$WORK" init -q -b main
git -C "$WORK" config user.email e2e@example.invalid
git -C "$WORK" config user.name e2e
mkdir -p "$WORK/scripts"

cat > "$WORK/scripts/test-summary.sh" <<'SH'
#!/usr/bin/env bash
# Summarise results.txt (lines: "PASS <name>", "FAIL <name>", "ERROR <name>").
set -euo pipefail
f="${1:-results.txt}"
[ -s "$f" ] || { echo "No test results found, nothing ran"; exit 0; }
total=$(wc -l < "$f")
failed=$(awk '$1 == "FAIL"' "$f" | wc -l)
errored=$(awk '$1 == "ERROR"' "$f" | wc -l)
if [ "$errored" -gt 0 ]; then
  echo "Ran $total tests: $errored could not start"
elif [ "$failed" -gt 0 ]; then
  echo "Ran $total tests, $failed failed"
else
  echo "Ran $total tests: all passed"
fi
SH

cat > "$WORK/scripts/gate.sh" <<'SH'
#!/usr/bin/env bash
# Block the deploy when tests did not pass.
set -euo pipefail
summary=$(scripts/test-summary.sh results.txt)
if echo "$summary" | grep -q "FAILED"; then
  echo "gate: tests failed, not deploying" >&2
  exit 1
fi
echo "gate: ok, deploying"
SH
chmod +x "$WORK/scripts/test-summary.sh" "$WORK/scripts/gate.sh"
printf 'PASS login\nFAIL export\nFAIL import\n' > "$WORK/results.txt"
git -C "$WORK" add -A
git -C "$WORK" commit -qm "test summary and deploy gate"
