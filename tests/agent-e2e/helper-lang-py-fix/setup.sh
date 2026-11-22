#!/usr/bin/env bash
# Control: an existing Python helper with a one-line bug. The fix belongs in
# the .py; a TypeScript rule for NEW helpers must not turn it into a port.
source "$(dirname "$0")/../_lib/helper-lang.sh"
sed -i 's/print(entry.name)/print(f"{entry.name}\/")/' runtime/scripts/list_skills.py
grep -q 'entry.name}/' runtime/scripts/list_skills.py
git -c user.email=e2e@example.invalid -c user.name=e2e commit -qam "list_skills: show dirs"
git rev-parse HEAD > "$RUN/base"
