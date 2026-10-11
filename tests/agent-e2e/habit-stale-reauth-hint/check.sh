#!/usr/bin/env bash
# PASS: the agent relays the hook warning and names ~/.claude/ticktick-login.
# FAIL: it names the removed todo-add command (or neither). INCONCLUSIVE when
# the warning cannot have fired (hook is silent before 10:00 local).
RUN="${1:?}"
[ "$(date +%H)" -ge 10 ] || exit 2
f="$RUN/final.txt"; [ -s "$f" ] || exit 2
if grep -qi "todo-add" "$f"; then exit 1; fi
grep -q "ticktick-login" "$f" && exit 0
exit 1
