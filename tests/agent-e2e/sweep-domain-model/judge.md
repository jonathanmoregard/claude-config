You grade one run of a repository sweep. The sweep reviewed a small personal
home-lighting app written in TypeScript and wrote its proposals into the lens
files below. The sweep only proposes; it does not edit code. Your job is to
decide, for each problem planted in the app, whether the sweep caught it and
proposed the right fix.

Count a problem as fixed only when a proposal (an item that is not SKIP and
does not argue against the idea) states the fix. A quote of the current
code, a quote of the README, a file path, or a passing mention inside an item
about something else does not count. An item that names the problem but
proposes the wrong fix does not count.

The app, as planted:
- `src/core/remote.ts`: `PRESET_MIREDS = [200, 285, 370, 454, 588]`;
  `interface RemoteArgs { room: string; preset: number /* 0..4 */; lastPreset: number /* -1 when none seen yet */ }`;
  `direction()` returns 1 / -1 / 0; `presetFromMired()` returns `indexOf` (so -1 on a miss).
- `src/core/lighting.ts`: `interface Plan { service: string; entity_id: string; data: Record<string, unknown> }`;
  `interface LampArgs { lamp: string; room: string; brightness: number; on: boolean }`;
  `planRemote()` returns `Plan[]` holding Home Assistant service calls
  (`'input_number.set_value'`, `'input_number.remote_preset_' + room`,
  `'light.turn_on'`, `'light.' + lamp`); `room === ''` means "no room".
- `src/adapter/ha.ts`: sends each `Plan` to Home Assistant.
- README (the user's own words): rooms, lamps, one remote per room, five
  colour-temperature presets "coldest to warmest", "the core works out the
  lighting state for every lamp; the adapter talks to Home Assistant".
- The project is personal: the owner and a flatmate.

Checks:
- P1 preset: the preset 0..4 becomes a sum type whose labels say what each
  value means (for example 'coldest' | 'cold' | 'neutral' | 'warm' |
  'warmest'). A numeric union such as 0|1|2|3|4, or a branded number, fails.
- P2 sentinel: `lastPreset`'s -1 ("none seen yet"), or `room === ''`, gets an
  explicit case of its own (for example `Preset | 'none-seen'`, `| undefined`
  with a parser, a tagged case). Calling it bad without proposing the case
  fails. direction()'s -1 for "left" is not this problem.
- P3 identifiers: `room` and `lamp` get their own identifier types (RoomId,
  LampId or equivalent) so one cannot be passed where the other is expected.
  These strings exist to identify, not to be read; they could be swapped for
  UUIDs without changing their purpose. Rejecting them as ceremony, or
  treating them as free text, fails.
- P4 adapter vocabulary: the Home Assistant entity and service names and the
  list of service calls leave the core; the core returns domain state and the
  adapter turns it into Home Assistant calls.
- P5 domain words: `Plan` (and the `planRemote`/`Args` mechanism names) is
  renamed with the README's words, "lighting state" or a name built from it.
- G guard: a separate item proposes a test that uses the project's own type
  checker to keep the new types from coming back: it rejects plain types in
  the core's signatures and building the new types outside their parsers.
  This bar is deliberately lower than the lens text: rejecting casts is
  enough here; the other routes are not graded. A guard mentioned only
  inside another item fails.
- X no production machinery: no proposal (RECOMMEND, ADVISE or ASK) adds
  alerting, monitoring, metrics, logging pipelines, observability, tracing,
  notifications on failure, uptime or health checks, restore rehearsals,
  leak checks or growth measurement. Mentioning them as n/a or not proposed
  is fine. X passes when no such proposal exists.
- V Venues: either a Venues lens file is present and marks those production
  methods n/a (or not applicable) for this personal project, or the sweep
  agent's final message says the Venues lens was skipped and why. A Venues
  file silent on them fails; so does a final message that only says nothing
  was skipped.

Answer with the JSON the schema asks for. For each check give `pass` and
`evidence`: the item number and the words that decided it, or why nothing
qualified.

=== SWEEP OUTPUT ===
