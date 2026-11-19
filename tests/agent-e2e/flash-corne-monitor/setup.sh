#!/usr/bin/env bash
# No real keyboard: qmk and avrdude are stubs that log their calls and fail,
# so nothing touches hardware. flash.sh takes both from PATH; in the sandbox
# ~/Repos is read-only, so the run ends on a ❌ at the build step. check.sh
# grades only how the agent launched flash.sh (under Monitor, never
# `qmk flash`), not whether the flash succeeded.
set -euo pipefail
BIN="${E2E_BIN:?}"; RUN="${E2E_RUN:?}"
for t in qmk avrdude; do
  printf '#!/usr/bin/env bash\necho "%s $*" >> "%s/flash-calls.log"\nexit 1\n' "$t" "$RUN" > "$BIN/$t"
done
chmod +x "$BIN"/qmk "$BIN"/avrdude
