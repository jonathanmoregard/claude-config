#!/usr/bin/env bash
# A small personal home-lighting app in TypeScript with a type-checker gate.
# Planted, for the grader only (nothing in the fixture's text names them):
#   P1 a closed set as a number: the remote preset is 0..4 (`preset: number`)
#   P2 a sentinel: -1 means "no preset seen", '' means "no room"
#   P3 a string id: `room: string`, `lamp: string` everywhere
#   P4 adapter vocabulary in the core: Home Assistant entity and service names
#      ('input_number.remote_preset', 'light.turn_on') and a list of service
#      calls returned from the core (`plan`)
#   P5 a mechanism name where a domain word exists: `Plan`, `Args`
# The README is the user's recorded vocabulary: "rooms", "lamps", "remote",
# "presets", "lighting state".
set -euo pipefail
WORK="${E2E_WORK:?}"
git -C "$WORK" init -q -b main
git -C "$WORK" config user.email e2e@example.invalid
git -C "$WORK" config user.name e2e
mkdir -p "$WORK/src/core" "$WORK/src/adapter" "$WORK/test" "$WORK/scripts"

cat > "$WORK/package.json" <<'J'
{ "name": "homelights", "private": true, "type": "module",
  "scripts": { "check": "tsc --noEmit", "test": "node --test" },
  "devDependencies": { "typescript": "5.9.3" } }
J
cat > "$WORK/tsconfig.json" <<'J'
{ "compilerOptions": { "strict": true, "noEmit": true, "target": "es2022", "module": "nodenext" }, "include": ["src"] }
J
cat > "$WORK/README.md" <<'M'
# homelights

My own lighting setup at home (just me and a flatmate). Each room has lamps;
each room has one remote. The remote's arrow buttons step through five
colour-temperature presets, coldest to warmest. The core works out the
lighting state for every lamp; the adapter talks to Home Assistant.

Gate: `npm run check && npm test`.
M
cat > "$WORK/scripts/check" <<'S'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
npm run -s check && npm test
S
chmod +x "$WORK/scripts/check"

cat > "$WORK/src/core/remote.ts" <<'T'
export const PRESET_MIREDS = [200, 285, 370, 454, 588];

export interface RemoteArgs {
  room: string;
  preset: number;      // 0..4
  lastPreset: number;  // -1 when none seen yet
}

/** Which way the arrows moved: 1 right, -1 left, 0 neither. */
export function direction(args: RemoteArgs): number {
  if (args.lastPreset === -1) return args.preset === 0 ? 1 : args.preset === 4 ? -1 : 0;
  const d = (args.preset - args.lastPreset + 5) % 5;
  return d === 1 ? 1 : d === 4 ? -1 : 0;
}

export function presetFromMired(mired: number): number {
  return PRESET_MIREDS.indexOf(mired);
}
T
cat > "$WORK/src/core/lighting.ts" <<'T'
import { direction, type RemoteArgs } from './remote.js';

export interface Plan { service: string; entity_id: string; data: Record<string, unknown> }

export interface LampArgs { lamp: string; room: string; brightness: number; on: boolean }

/** What to send after a remote press: brightness up or down by 25 on every lamp in the room. */
export function planRemote(args: RemoteArgs, lamps: LampArgs[]): Plan[] {
  if (args.room === '') return [];
  const step = direction(args) * 25;
  const plans: Plan[] = [{ service: 'input_number.set_value', entity_id: 'input_number.remote_preset_' + args.room, data: { value: args.preset } }];
  for (const l of lamps) {
    if (l.room !== args.room || !l.on) continue;
    const b = Math.max(1, Math.min(255, l.brightness + step));
    plans.push({ service: 'light.turn_on', entity_id: 'light.' + l.lamp, data: { brightness: b } });
  }
  return plans;
}
T
cat > "$WORK/src/adapter/ha.ts" <<'T'
import type { Plan } from '../core/lighting.js';

export interface Hass { callService(domain: string, service: string, data: Record<string, unknown>): Promise<void> }

export async function send(hass: Hass, plans: Plan[]): Promise<void> {
  for (const p of plans) {
    const [domain, service] = p.service.split('.');
    await hass.callService(domain, service, { entity_id: p.entity_id, ...p.data });
  }
}
T
cat > "$WORK/test/remote.test.js" <<'J'
import { test } from 'node:test';
import assert from 'node:assert';
test('placeholder: the suite runs', () => { assert.ok(true); });
J
echo 'node_modules/' >> "$WORK/.gitignore"
# The installed toolchain: the real tsc, as `npm install` would leave it.
TSC=$(ls -d /nix/store/*-typescript-5.9.3 | head -n1)
mkdir -p "$WORK/node_modules/.bin"
ln -s "$TSC/bin/tsc" "$WORK/node_modules/.bin/tsc"
git -C "$WORK" add -A
git -C "$WORK" commit -qm "feat: remote presets step lamp brightness"
# The gate the sweep's entry check needs really is green.
( cd "$WORK" && ./scripts/check >> "$E2E_RUN/setup-gate.log" 2>&1 )

notes="$HOME/.local/state/claude-tasks/$(basename "$WORK")"
mkdir -p "$notes"
printf 'date\tsource\trun\tcheck\ttrigger\twhere\tclaim\toutcome\tfixed\tfp_reason\n' > "$notes/review-ledger.tsv"
