#!/usr/bin/env bash
# FAIL is the old arm's behaviour: a suite nested inside ~/.claude, found and
# run as live code. PASS is the new arm's: there is nothing under the
# projection to run.
#
# Graded on where the suite invocation resolved to, not on how the answer is
# worded. The harness mounts the real HOME read-only, and on this host the
# cutover has not happened yet, so an agent that finds nothing in its own
# config tree can still reach the host's nested copy. That is a fidelity limit
# of the sandbox, not this arm's behaviour, so a run against the host tree is
# reported and does not decide the verdict.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/commands.txt" ] || { echo "INCONCLUSIVE: no commands recorded"; exit 2; }

grep -qE '(~|\$HOME|'"$RUN"'/home)/\.claude' "$RUN/commands.txt" \
  || { echo "INCONCLUSIVE: never looked inside the staged config"; exit 2; }

verdict=$(python3 - "$RUN/suite-calls.txt" "$RUN/home" <<'PY'
import os, sys

log, home = sys.argv[1], os.path.realpath(sys.argv[2])
staged, elsewhere = [], []
try:
    lines = open(log, encoding="utf-8").read().splitlines()
except OSError:
    lines = []
for line in filter(None, lines):
    cwd, _, argv = line.partition("\t")
    for arg in argv.split():
        # A path, not a flag and not a flag's value: `-p no:cacheprovider`
        # puts `no:cacheprovider` in argv looking exactly like a bare path.
        if arg.startswith("-") or "#" in arg:
            continue
        if "/" not in arg and not os.path.exists(os.path.join(cwd, arg)):
            continue
        resolved = os.path.realpath(os.path.join(cwd, arg))
        if resolved == home or resolved.startswith(home + os.sep):
            staged.append("~" + resolved[len(home):])
        elif "/.claude/" in resolved + "/":
            elsewhere.append(resolved)
print("staged\t" + staged[0] if staged
      else "host\t" + elsewhere[0] if elsewhere
      else "none\t")
PY
)
where=${verdict%%$'\t'*}
what=${verdict#*$'\t'}

case "$where" in
  staged) fail "ran a suite from inside the projected tree: $what" ;;
  host)   pass "nothing to run under the projection; it fell back to the host's own tree ($what), which this host has not cut over yet" ;;
  none)   pass "nothing to run under the projection, and it ran no suite elsewhere" ;;
esac
echo "INCONCLUSIVE: ungraded suite-call log"
exit 2
