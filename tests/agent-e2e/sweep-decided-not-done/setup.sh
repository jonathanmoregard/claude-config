#!/usr/bin/env bash
# The homelights fixture (sweep-domain-model/setup.sh), plus the user's
# recorded decisions as front-matter files: legacy-sync.ts is to be deleted,
# and the string room/lamp ids and the preset number are to be re-typed in
# place. Neither has landed. Also a keep decision (brightness stays a number)
# and a finished change (scripts/check). Graded (grade.py): only the deletion
# under "Leaving soon", the id re-typing under "Decided, not done" and
# reported by the Domain model lens instead of skipped as already decided;
# the keep and the finished decision listed nowhere and not reported.
set -euo pipefail
bash "$(dirname "$0")/../sweep-domain-model/setup.sh"
WORK="${E2E_WORK:?}"
cat > "$WORK/src/adapter/legacy-sync.ts" <<'T'
import type { Hass } from './ha.js';

/** The old one-lamp-at-a-time sync, still called by the morning routine. */
export async function syncLamp(hass: Hass, lamp: string, brightness: number): Promise<void> {
  await hass.callService('light', 'turn_on', { entity_id: 'light.' + lamp, brightness });
}
T
git -C "$WORK" add -A
git -C "$WORK" commit -qm "chore: keep the old lamp sync for the morning routine"
notes="$HOME/.local/state/claude-tasks/$(basename "$WORK")"
mkdir -p "$notes/decisions"
cat > "$notes/session-constraints.md" <<'M'
# Session constraints

## 2026-11-17 — keep the core free of Home Assistant calls
User, verbatim: "the adapter talks to Home Assistant, nothing else does"
Rule: only `src/adapter` calls Home Assistant.
M
cat > "$notes/decisions/2026-11-18-delete-legacy-sync.md" <<'M'
---
date: 2026-11-18
title: Delete legacy-sync.ts once the morning routine uses ha.ts
kind: change
status: pending
where:
  - src/adapter/legacy-sync.ts
---
User, verbatim: "the morning routine moves to ha.ts next week, then delete legacy-sync.ts"

Reading: `src/adapter/legacy-sync.ts` is deleted once the morning routine calls `send` in `ha.ts`.
M
cat > "$notes/decisions/2026-11-19-room-lamp-preset-types.md" <<'M'
---
date: 2026-11-19
title: Room and lamp ids and the preset get proper types
kind: change
status: pending
where:
  - src/core/remote.ts
  - src/core/lighting.ts
---
User, verbatim: "room and lamp shouldn't be plain strings, and the preset shouldn't be a number. Type them properly, same code otherwise."

Reading: `room` and `lamp` get their own id types and `preset` becomes a labelled type, in place in `src/core`; the functions stay.
M
cat > "$notes/decisions/2026-11-16-brightness-stays-a-number.md" <<'M'
---
date: 2026-11-16
title: "Lamp brightness stays a plain number"
kind: keep
status: done
where:
  - src/core/lighting.ts
---
User, verbatim: "brightness is just Home Assistant's 0-255, leave it a number"

Reading: `brightness: number` in `LampArgs` stays as it is on purpose.
M
cat > "$notes/decisions/2026-11-15-gate-in-scripts-check.md" <<'M'
---
date: 2026-11-15
title: "The gate runs from scripts/check"
kind: change
status: done
where:
  - scripts/check
---
User, verbatim: "put the gate in one script"

Reading: `scripts/check` runs the type check and the tests. Landed.
M
# For check.sh: the sweep must leave every decision file as it is.
( cd "$notes/decisions" && sha256sum ./*.md ) > "${E2E_RUN:?}/decisions.sha256"
