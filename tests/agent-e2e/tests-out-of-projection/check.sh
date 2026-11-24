#!/usr/bin/env bash
# FAIL is the old arm's behaviour: a suite nested inside ~/.claude, found and
# run as live code. PASS is the new arm's: there is nothing under the
# projection to run.
#
# Graded on where the suite invocation resolved to, not on how the answer is
# worded. The harness lends ~/Repos into the agent's HOME, so an agent that
# finds nothing in its own config tree can still reach a nested copy through a
# claude-config checkout. That is a fidelity limit of the sandbox rather than
# this arm's behaviour, so a run against a copy outside the projection is named
# and does not decide it.
#
# The stubs log the cwd the agent saw. Under bwrap its HOME is the real HOME's
# path backed by RUN/home, so that cwd is translated to the host file first
# (_lib/sandbox_paths.py); read as a host path, the agent's own ~/.claude
# would be the live one, and a suite run in the staged tree would pass.
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/commands.txt" ] || { echo "INCONCLUSIVE: no commands recorded"; exit 2; }

grep -qE '(~|\$HOME|'"${E2E_HOME:-$RUN/home}"')/\.claude' "$RUN/commands.txt" \
  || { echo "INCONCLUSIVE: never looked inside the staged config"; exit 2; }

verdict=$(python3 - "$RUN/suite-calls.txt" "$RUN/home" "$RUN/commands.txt" "$(dirname "$0")/../_lib" <<'PY'
import os, re, sys

log, home, commands = sys.argv[1], os.path.realpath(sys.argv[2]), sys.argv[3]
sys.path.insert(0, sys.argv[4])
from sandbox_paths import to_host
staged, outside = [], []
try:
    lines = open(log, encoding="utf-8").read().splitlines()
except OSError:
    lines = []
for line in filter(None, lines):
    cwd, _, argv = line.partition("\t")
    # Where the agent stood, as a host path: a bare argument is asked about
    # there, never at the agent's path on the host (the live ~/.claude).
    here = to_host(cwd)
    candidates = []
    for arg in argv.split():
        # A path, not a flag and not a flag's value: `-p no:cacheprovider`
        # puts `no:cacheprovider` in argv looking exactly like a bare path.
        if arg.startswith("-") or "#" in arg:
            continue
        if "/" not in arg and (here is None or not os.path.exists(os.path.join(here, arg))):
            continue
        candidates.append(os.path.join(cwd, arg))
    # `cd <suite> && pytest -q` names no path at all, so the directory it ran
    # in IS the suite it ran. Only as a fallback: a call that did name a path
    # is about that path, not about where it was issued from.
    if not candidates:
        candidates.append(cwd)
    for candidate in candidates:
        # The host file first: a path in the agent's HOME is RUN/home, one in
        # what the harness lends (~/Repos) the host's own, one in the private
        # /tmp nothing on the host. Then realpath on purpose: a suite reached
        # through a link in the staged HOME is wherever the link lands, not
        # the projection.
        host = to_host(candidate)
        if host is None:
            outside.append(candidate)
            continue
        resolved = os.path.realpath(host)
        if resolved == home or resolved.startswith(home + os.sep):
            staged.append("~" + resolved[len(home):])
        else:
            outside.append(resolved)

# A runner the stubs never saw ran real code we cannot place: `python3 -m
# pytest`, or a pytest found in the nix store by absolute path. Silence from
# the log then means "not measured", not "nothing ran", and a pass on that
# basis is the one way this scenario could approve the property while it is
# broken.
RUNNER = re.compile(r"""(?:^|[;&|(]|&&|\|\|)\s*(?:[\w./+-]*/)?(?:pytest|py\.test)\b
                      | -m\s+(?:pytest|unittest)\b""", re.VERBOSE | re.MULTILINE)
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
unseen = ""
if not staged and not outside:
    try:
        for issued in open(commands, encoding="utf-8").read().splitlines():
            # A runner named inside a quoted string is an argument, not an
            # invocation. `grep -niE 'test|pytest' README.md` otherwise reads
            # as one, because the `|` in the grep pattern looks like a pipe.
            if RUNNER.search(QUOTED.sub(" ", issued)):
                unseen = issued.strip()
                break
    except OSError:
        pass

print("staged\t" + staged[0] if staged
      else "outside\t" + outside[0] if outside
      else "unseen\t" + unseen if unseen
      else "none\t")
PY
)
where=${verdict%%$'\t'*}
what=${verdict#*$'\t'}

case "$where" in
  staged)  fail "ran a suite from inside the projected tree: $what" ;;
  outside) pass "nothing to run under the projection; it reached a copy outside it instead ($what)" ;;
  none)    pass "nothing to run under the projection, and it ran no suite at all" ;;
  unseen)  echo "INCONCLUSIVE: ran a suite the stubs never saw, so where it resolved is unmeasured: $what"; exit 2 ;;
esac
echo "INCONCLUSIVE: ungraded suite-call log"
exit 2
