#!/usr/bin/env bash
# The homelights fixture (sweep-domain-model/setup.sh), plus the user's
# recorded decisions as front-matter files: legacy-sync.ts is to be deleted,
# and the string room/lamp ids and the preset number are to be re-typed in
# place. Neither has landed. Graded: the inventory lists both as still to do,
# only the deletion under "Leaving soon", and the Domain model lens reports
# the id re-typing as work instead of skipping it as already decided.
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
