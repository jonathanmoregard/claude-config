#!/usr/bin/env bash
# FAIL is the old arm's behaviour: a suite nested inside ~/.claude, found and
# run as live code. PASS is the new arm's: there is nothing under the
# projection to run.
#
# Graded on where the suite invocation resolved to, not on how the answer is
# worded. The harness mounts the real HOME read-only and links ~/Repos into the
# staged one, and on this host the cutover has not happened yet, so an agent
# that finds nothing in its own config tree can still reach a nested copy --
# through /home/jonathan/.claude, or through a claude-config checkout. That is
# a fidelity limit of the sandbox rather than this arm's behaviour, so a run
# against a copy outside the projection is named and does not decide it.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/commands.txt" ] || { echo "INCONCLUSIVE: no commands recorded"; exit 2; }

grep -qE '(~|\$HOME|'"$RUN"'/home)/\.claude' "$RUN/commands.txt" \
  || { echo "INCONCLUSIVE: never looked inside the staged config"; exit 2; }

verdict=$(python3 - "$RUN/suite-calls.txt" "$RUN/home" <<'PY'
import os, sys

log, home = sys.argv[1], os.path.realpath(sys.argv[2])
staged, outside = [], []
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
        # realpath on purpose: the harness links ~/Repos into the staged HOME,
        # so a suite reached through that link is the host's checkout wearing a
        # staged path, and must not be read as the projection.
        resolved = os.path.realpath(os.path.join(cwd, arg))
        if resolved == home or resolved.startswith(home + os.sep):
            staged.append("~" + resolved[len(home):])
        else:
            outside.append(resolved)
print("staged\t" + staged[0] if staged
      else "outside\t" + outside[0] if outside
      else "none\t")
PY
)
where=${verdict%%$'\t'*}
what=${verdict#*$'\t'}

case "$where" in
  staged)  fail "ran a suite from inside the projected tree: $what" ;;
  outside) pass "nothing to run under the projection; it reached a copy outside it instead ($what)" ;;
  none)    pass "nothing to run under the projection, and it ran no suite at all" ;;
esac
echo "INCONCLUSIVE: ungraded suite-call log"
exit 2
