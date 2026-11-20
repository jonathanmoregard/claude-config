#!/usr/bin/env bash
# The homelights fixture (sweep-domain-model/setup.sh), plus the user's
# recorded plans: one file is to be deleted, and the string ids and the
# preset number are to be re-typed in place. Graded: the inventory's
# "Leaving soon" lists the deletion and not the re-typing, which every lens
# must still read.
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
cat > "$notes/session-constraints.md" <<'M'
# Session constraints

## 2026-11-18 — legacy sync goes
User, verbatim: "the morning routine moves to ha.ts next week, then delete legacy-sync.ts"
Rule: `src/adapter/legacy-sync.ts` is deleted once the morning routine calls `send` in `ha.ts`.

## 2026-11-19 — proper types for rooms, lamps and presets
User, verbatim: "room and lamp shouldn't be plain strings, and the preset shouldn't be a number. Type them properly, same code otherwise."
Rule: `room` and `lamp` get their own id types and `preset` becomes a labelled type, in place in `src/core`; the functions stay.
M
