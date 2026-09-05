---
name: klaffat-components
description: >
  Reusable UI partials + tokens for the Klaffat web app (formerly
  kablong-components). Use when working in the klaffat repo on templates,
  tokens, or component partials — authoring or editing a radio group, button,
  dialog, form field, or empty state; when a template lint warning points at
  a raw <input type="radio">, <button>, or <dialog>; or when writing/adjusting
  CSS custom properties in tokens.css. Also applies for destructive-POST
  dialogs and any migration from an inline control to a component include.
  Files live under crates/klaffat-web/templates/_components/ and
  crates/klaffat-web/static/tokens.css (typical paths: ~/Repos/klaffat,
  ~/worktrees/klaffat-*).
---

# klaffat-components — the design-system partial library

The askama partials under `crates/klaffat-web/templates/_components/` plus
the token file at `crates/klaffat-web/static/tokens.css`. One place per
component; ARIA rules and subtle details cited in the per-component SOTA
review: `docs/reviews/component-sota-2026-07-31.md` (read that first for the
"why" — this file is the "when + how").

## Two categories — primitives vs page patterns (2026-08-03)

The library splits into two categories (Ph-4-empty-reclassify 2026-08-03):

- **Component primitives** — reusable interactive building blocks (btn,
  dialog, radiogroup, combobox, field, timegrid). Self-contained; do not
  know about page layout; can be composed into another component. Playground
  route: `/dev/components/<slug>`.
- **Page patterns** — compositions of primitives + copy + layout that only
  make sense at page scope (currently only `c-empty`). NOT reusable
  primitives — you would not nest one inside another component; you would
  render an entire page around one. Playground route: `/dev/pages/<slug>`.

If you're building a new UI shape, ask: "would I nest this inside another
component to reuse it, or would I render a whole page around it?" — first
answer = primitive, second answer = page pattern. Partial files still live
under `_components/` in both cases (moving files is orthogonal to the
taxonomy); the split is a naming + routing + reviewer-mental-model
distinction, not a filesystem one.

## Discover the partials from a fresh session

```
ls crates/klaffat-web/templates/_components/
```

Contract for each partial lives at the top of its file in `{# ... #}`. Every
partial is included via `{% include "_components/<name>.html" %}` after the
caller sets the expected `{% let %}` variables — askama has no macro slots,
so the mini-contract at the top of each partial is authoritative.

## Locked v1 variants — visual DNA (2026-07-31)

The gallery fan-out (Claude → v2/v3/v4/v8 iterations, founder picks) settled
the per-component visual language. Every new template, new partial, and every
edit to these partials MUST stay inside these constraints. Selection
rationale + reference gallery HTML: `/tmp/vv-gallery-decisions.md` and the
per-component files under `/tmp/vv-gallery-v{2,3,4,8}/`. Design spec source
of truth: `docs/brand/klaffat-design-spec.md` §4.12 "Locked variants
2026-07-31".

- **c-btn = gallery-v2 Variant C** — Base UI + Klaffat wireframe
  (flat/sharp). Zero border-radius, mono uppercase label (letter-spacing
  0.08em), 13px font, `10px var(--s4)` padding, 44px min-height. INSET 2px
  focus ring: paper band then rail band via layered
  `box-shadow: inset 0 0 0 2px --paper, inset 0 0 0 4px --rail` — unique to
  focus so keyboard users never confuse it with hover. `--secondary` is
  transparent with a `--rule-strong` border (NOT paper+rail — that's
  `--primary`); hover shifts to wash + rail. `--ghost` breaks the mono
  spell: sans-serif 13px, underlined (offset 3px), --muted → --rail on
  hover (reads as a text link, not a button).
- **c-dialog = gallery-v3 Iteration A** — shadcn AlertDialog geometry,
  `border-radius: 0`. 1px `--rule-strong` border, `min(28rem, 100vw -
  2×gutter)` width, `--s6` padding, soft double shadow
  (`0 10px 40px + 0 2px 6px`). Form-column spacing via `gap: var(--s3)`.
  Actions right-aligned with `var(--s2)` gap. Title 16px semibold, tracking
  `-0.01em`; body 14px muted, line-height 1.5. `.c-dialog--danger` puts the
  TITLE in `--strike` (GA-06); border stays subtle — the colour cue is the
  title, not the chrome. When a `--danger` dialog has no `c-dialog__title`
  (e.g. inline delete-account exception), communicate danger via the
  destructive button, not chrome.
- **c-radiogroup = gallery-v4 Variant A** — ONE pill treatment, TWO flow
  directions. Structure:
  `fieldset > legend > (hint) > .c-radiogroup__segmented > label*`. Each
  label = a pill with a hidden `<input type="radio">`, a presentational
  12px `.c-radiogroup__dot` circle, and a `.c-radiogroup__label` span with
  mono uppercase text.

  **THE RULE (founder, standing, 2026-09-05 — told 4+ times): the circle is
  ALWAYS on the LEFT of its label, on ONE line, vertically centred on the
  label's FIRST line. Never above it, never a column for the control
  itself. A stacked radio is a defect, not a style choice.** It holds in
  both directions and in every variant.

  Which direction is chosen by a CONTAINER QUERY on the fieldset
  (`container-type: inline-size`), not a media query and not JS — the
  deciding width is the width available to THIS group, so a group in a
  narrow panel stacks even on a wide screen:
  - `>= 38rem` → pills flow in a ROW: the segmented rail, 1.5px `--rail`
    border around it plus a 1.5px `--rail` LEFT border on each option
    (except the first).
  - below that → the SAME pills stacked in a COLUMN, full width, with the
    hairline on the TOP edge instead. Typography, borders, colours and the
    circle are identical; only the flow direction and the border edge
    change. (An earlier attempt at a separate GOV.UK-style plain list for
    the narrow case was rejected: "it's subtly different atm, in ways that
    make it worse.")

  Zero radius. Selected pill inverts (`--rail` bg, `--paper` text) and the
  circle inverts (`--paper` ring around a solid `--paper` dot via
  `radial-gradient`). Focus ring matches c-btn v2C (inset paper+rail) and
  covers the WHOLE pill — circle and label together, never the circle
  alone. Every pill is >= 44px tall: the whole pill is the hit area, so the
  PILL is the WCAG 2.2 SC 2.5.8 target, not the 12px circle.

  **Vertical centring — the pill is a CENTRING BOX, and never a nudge.**
  Founder, 2026-09-05, on a build where the group sat 11px from the top and
  18px from the bottom: "both the ring and the text is a bit too high."
  Two things must both hold, and neither is a magic number:
  - `.c-radiogroup__option` is `align-items: center` with its height from
    `min-height` and SYMMETRIC `padding-block`. The circle is
    `align-self: center`. NO `align-items: flex-start`, NO `margin-top` on
    the dot. Every offset-based version of this drifted the moment a
    font-size, a line-height or the dot size moved on its own.
  - the label's box must equal its INK, or centring the box still leaves
    the glyphs high: `line-height: 1` plus `text-box-trim: trim-both;
    text-box-edge: cap alphabetic`. Pill labels are mono UPPERCASE with no
    descenders, so cap-height-to-baseline IS the visible glyph; centring
    the full line box reserves descender space nothing occupies. The
    `line-height: 1` fallback alone is already centred to within 0.5px, so
    the trim is a pure progressive enhancement.
  Measured result, every pill and every state: 16.0px of air above and
  16.0px below; circle centre and label text centre identical to 0.00px.
  If you change any of this, measure the two numbers again — the oracle's
  `content-centerY` rule is what holds it.

  **Nothing ever wraps.** The rail is `flex-wrap: nowrap` and pill labels
  are `white-space: nowrap`, so a group whose labels do not fit would
  OVERFLOW rather than wrap. The 38rem threshold is therefore chosen
  against the widest TRANSLATED rail of any row-eligible group (Swedish
  runs ~17% wider than English — measure before adding one). A group that
  cannot fit takes `rg_layout = "block"`, which pins the stacked column at
  every width; that is also the right call for long, consequential choices
  saved on submit, like create-poll's "Times that can't work".
  `--with-description` never flows into a row: the description sits under
  the label inside the label column, and the circle still centres on the
  label's first line.

  **The oracle enforces all of it.**
  `tests/e2e/tests/component-geometry-oracle.spec.ts` (`assertRadiogroup`)
  measures every option at both container widths and hard-fails on: circle
  and label not sharing a row; circle not before the label by the token
  gap; circle centre off the label's first-line centre by >1px; the centre
  of (circle box union label INK box) off the pill's own centre by >1px —
  the rule that catches a correctly-paired group sitting too high in an
  over-tall pill; a row that wrapped or overflowed its group; a stacked
  pill under 44px or overflowed by its label. Change the layout and you must satisfy that spec — do not
  relax it.

  Real radio input stays a11y-focusable via `opacity: 0` (NEVER
  `display: none`) and is absolutely positioned across the pill so the
  whole pill is clickable. Test cost: Playwright's `.check()` on a hidden
  radio fails the actionability visibility gate — target the enclosing
  `label.c-radiogroup__option` instead.
- **c-field = gallery-v8 D1×W1** — 1px solid `--ink` border (warm
  off-black `#1C1B18`, NOT pure `#000`), zero radius, `--paper` bg
  (`#FCFBF8`, warm — not `#FFF`). Tight Gestalt-proximity spacing: 6px
  label→input, ~24px between fields (caller sets the outer stack).
  Control padding `9px 12px`, min-height 40px, sans 14px. Focus ring =
  c-btn v2C DNA (inset paper+rail). Error state swaps border to
  `--strike`.
- **c-empty = gallery-v4 Variant C — PAGE PATTERN, not a primitive**
  (reclassified 2026-08-03 — Ph-4-empty-reclassify). Typography-only
  Superhuman "inbox zero" pattern that composes the `c-btn` primitive
  inside a centered page layout. Vercel Geist geometry: `--s12` (48px)
  vertical padding, centered. 28px (`--fs-xl`) semibold headline with
  `-0.01em` tracking and 1.15 line-height, capped at 26rem max-width; one
  14px muted supporting sentence (`--s3` gap to title); primary CTA at
  `--s5` gap (btn=v2C DNA). Icon slot is DISCOURAGED (default `""`) — the
  base surface is title+body+CTA. If used at all, brand-native
  illustration only (departure-board flap or a monoline `--ink` SVG at
  1.5px stroke); never generic clipboard/folder/looking-glass. Optional
  secondary ghost link (e.g. "Learn how it works") sits close to the
  primary CTA (`--s2`, 8px above) so the pair reads as one action
  affordance, not as body-adjacent copy (Ph-4-empty-ghost-space
  2026-08-03).

## Round-2 additions (2026-08-01, design-review round-2)

- **c-combobox = v10 Variant A** — `_components/combobox.html`. Native
  `<input list="…">` + `<datalist>` for the a11y baseline (browser gives us
  the combobox affordance without WAI-ARIA authoring; no-JS works). Two-part
  contract: (1) native baseline; (2) optional TypeScript enhancement that
  filters as the user types and computes richer display labels. The reference
  enhancement is `client/timezone-combobox.ts` — populates the datalist with
  the full IANA list from `Intl.supportedValuesOf('timeZone')`, sorted
  alphabetically by city (never by offset — research: only 34% of users know
  their UTC offset), auto-detects the viewer's zone via
  `Intl.DateTimeFormat().resolvedOptions().timeZone` on load when the field
  is empty, and displays `City (GMT±X)` with the GMT offset computed LIVE
  per zone via `timeZoneName: 'shortOffset'` (DST-correct because offsets
  recompute at render time, not bundled). Submitted value stays the IANA
  identifier — server validation contract unchanged. Same 6px
  label→control proximity gap as c-field; same c-btn=v2C focus ring DNA.

  Contract: `cb_id` / `cb_name` / `cb_label` / `cb_options` (`&[(&str value,
  &str display_label)]`) / `cb_current` / `cb_hint` / `cb_placeholder` /
  `cb_required` (bool) / `cb_extra` (|safe raw HTML, compile-time literal
  only — enforced by the safe_marked_partial_vars_bind_string_literals_only
  lint).

  Example include:
  ```
  {% let cb_id = "timezone" %}
  {% let cb_name = "timezone" %}
  {% let cb_label = loc.t("new-timezone-label") %}
  {% let cb_current = timezone.clone() %}
  {% let cb_hint = String::new() %}
  {% let cb_placeholder = String::from("Europe/Stockholm") %}
  {% let cb_required = true %}
  {% let cb_extra = "data-tz-combobox" %}
  {% let cb_options = [("UTC", "UTC (GMT)"), ("Europe/Stockholm", "Stockholm")] %}
  {% include "_components/combobox.html" %}
  ```

  When to reach for c-combobox: bounded-list selection with >8 options
  where users know the value or a substring (timezones, currencies,
  countries, languages). NOT for fixed choice sets ≤3 — those are
  c-radiogroup. NOT for booleans — those are a checkbox in c-field.

- **c-dialog--scope-choice = v3 Variant B** — dialogue variant of c-dialog
  for pre-OAuth or pre-action tier choice. Wider than the confirm-shape
  default (`min(34rem, 100vw - 2×gutter)` vs `min(28rem, …)`) to fit two
  option cards + descriptions without wrapping. Inline `<fieldset
  class="scope-choice">` layout stacks option cards with radios LEFT of
  label/description (FB-F9 principle applied — circle-above-text breaks
  scan order for sentence labels). Proximity inequality preserved:
  label→description gap (`--s1`) < option→option gap (`--s3`).

  Reference use: `calendar_settings.html` pre-OAuth scope-tier choice
  (availability-only vs availability + event details). Rationale: research
  topic 2 — no competitor gives users this choice; data-minimization
  default + Google-review-friendly non-sensitive scope path + GDPR
  compliance anchor.

  When to reach for c-dialog--scope-choice: any pre-OAuth or pre-action
  choice where the user picks between 2–3 tiers of a durable permission
  grant. NOT for confirm-cancel — that's plain c-dialog. NOT for form
  submission — that's dialog_form.html.

Compounding intent: the c-combobox partial is the first "search-over-
bounded-set" control the library ships. Future currency / country /
language pickers should extend the SAME partial with an equivalent TS
enhancement — one datalist shape, one enhancement contract, one render
pattern.

## What to use when

| Partial | Use when | Anti-slop rules |
|---|---|---|
| `radio_group.html` | A fixed-choice set of ≤3 options (design spec §4.11). Two options for a boolean-ish either/or, three for a small enum. Renders as a segmented pill ROW when the group has ≥38rem, and as the same pills STACKED below that — circle left of the label on one line in both. | ≥4 fixed options → use a `<select>` (dropdown) instead. Never hand-roll a segmented control: this component IS one when it has room. Long labels or a consequential saved-on-submit choice → `rg_layout = "block"` (needs a `radio-block-ok:` marker). |
| `button.html` | Any tap/click affordance that submits, opens a dialog, or triggers a client action. Variants: primary / secondary / destructive / ghost. | Never `<div role="button">`. Never a bare `<a>` styled to look like a button. Always `type="button"` unless inside a form where you actually want submit. |
| `dialog.html` | JS-only confirm/cancel modal with no server action (uses `<form method="dialog">`). | Never a JS modal library — native `<dialog>` gives us focus containment + ESC + `aria-modal` free. |
| `dialog_form.html` | Destructive server-POST modal (delete a resource, revoke a token). Wraps `<form method="post" action="…">` inside `<dialog class="c-dialog c-dialog--danger">`. Params include `dlg_action`, `dlg_method`, `dlg_confirm_variant` (usually `"destructive"`), and `dlg_extra` for hidden inputs / phrase-confirm markup. | `dlg_extra` is rendered `|safe` — it MUST be a compile-time string literal (enforced by the `safe_marked_partial_vars_bind_string_literals_only` test in `tests/template_lint.rs`). If the extra markup needs LOCALIZED text (`loc.t(…)` calls), `dlg_extra` won't work — see the account-settings exception below. |
| `form_field.html` | Standard labeled input with optional hint and error. | Never wrap the input with the label without also using `for="..."` (double-association bug). Hint goes OUTSIDE the label (GOV.UK rule — inside gets re-announced on focus). |
| `empty_state.html` | List/table/dashboard has no items yet. | Never blame the user ("You haven't done X" → "Get started by X"). CTA verb matches the primary action. No stock isometric illustrations, no purple-blue gradients, no glassmorphism. Icon slot discouraged per locked v1 (typography-only default). |
| `combobox.html` | Bounded-list selection with >8 options where users know the value or a substring (timezones, currencies, countries, languages). Native `<input list>` + `<datalist>` baseline; optional TypeScript enhancement layers on richer filtering. | Never for fixed choice sets ≤3 (use `radio_group.html`). Never for booleans (use a checkbox in `form_field.html`). The submitted value is the raw `option value=` — display labels are UI-only; keep server-side value validation on the canonical identifier. |

## Tokens — one file, every scale value declared

`crates/klaffat-web/static/tokens.css` holds every custom property the app
consumes. Imported at the top of `styles.css` so downstream rules resolve
`var(--…)`.

**FB-017 rule (load-bearing):** every 8pt-scale ordinal that any file uses
gets a real definition (`--s1` through `--s12`). An undeclared `var(--sN)`
resolves to nothing at browser level — the CSS declaration silently becomes
invalid and elements render with zero padding/margin. If a new component
wants an intermediate stop, ADD IT TO `tokens.css` FIRST.

## Forbidden patterns (anti-slop guardrails)

From the per-component SOTA doc's "Anti-slop guardrails" section — reject
these in review, don't ship them:

- Generic-blue CTA (default shadcn/Tailwind blue). Klaffat uses `--rail` only.
- Purple → blue gradient buttons or hero backgrounds.
- Glassmorphism (frosted-glass backdrop-filter on cards).
- Stock isometric illustrations (person-with-laptop, "abstract shapes").
- Emoji-in-heading substituting for a real icon.
- Card grid where a list would do.
- "Coming soon" placeholder that isn't a real feature.
- Center-everything default (Klaffat is left-aligned unless there's a reason).
- User-blaming empty-state copy ("You haven't done X" ← rewrite to
  "Get started by X").
- Raw hex or px in new component CSS — always `var(--…)` from tokens.css.

## The soft-warning lint

`crates/klaffat-web/tests/template_lint.rs` has two checks:

- **hard fail** — `no_fixed_choice_select_ships_in_a_template`: a `<select>`
  with ≤3 static `<option>`s blocks the suite. Fix by converting to
  `radio_group.html`, or (rare) add an `<!-- select-ok: reason -->` marker.
- **soft warn** — `warn_on_raw_control_used_outside_component_partial`:
  a raw `<input type="radio">`, `<button>`, or `<dialog>` outside the
  partial prints a stderr nudge but does not block. Migrate to the partial
  via `{% include %}`, or mark with `<!-- component-inline-ok: reason -->`
  IN HTML COMMENT SYNTAX (not `{# ... #}` — the lint strips askama comments
  before scanning, so an askama-comment marker will not be seen).

## When to reach for something else

- If the design calls for a new component shape not in this set, DO NOT
  hand-roll it inline. Add a new partial under `_components/` + its
  `c-<name>` styles + update this skill file.
- If a rare surface genuinely needs to bypass a partial, the allow-marker
  is the escape hatch — but the marker MUST cite the actual reason.
  "Because" is not a reason.
- **Known exception — `account_settings.html`:** the delete-account dialog
  uses an inline `<dialog class="c-dialog c-dialog--danger">` rather than
  `dialog_form.html` because it needs to RELOCATE a single localized form
  (with `loc.t(...)` phrase-label text) from the JS-off inline position
  into the dialog on click. `dialog_form.html`'s `dlg_extra` requires a
  string literal, which forbids the required `loc.t(...)` calls. New
  destructive-POST dialogs without a localization-relocation requirement
  MUST use `dialog_form.html`.

## `|safe` variables — literal-only contract

Five partials render caller-supplied HTML raw (`|safe`):

- `_components/button.html` → `btn_extra`
- `_components/form_field.html` → `ff_extra`
- `_components/empty_state.html` → `es_icon`
- `_components/dialog_form.html` → `dlg_extra`
- `_components/combobox.html` → `cb_extra`

The corresponding `{% let X = ... %}` in the caller MUST be a compile-time
string literal (a `"…"` with no embedded `{{ … }}`). Enforced by the
`safe_marked_partial_vars_bind_string_literals_only` test in
`tests/template_lint.rs` — the alternative is silent template-injection XSS.
If you need user-derived data in one of these slots, refactor the partial
so the field gets ESCAPED rendering instead.

## Reference

- **Locked variants (2026-07-31)** — `docs/brand/klaffat-design-spec.md`
  §4.12 "Locked variants 2026-07-31"; per-component visual DNA that every
  new template must stay inside.
- **Deep rationale** — `docs/reviews/component-sota-2026-07-31.md` (per-
  component ARIA APG rules + which established design systems each subtle
  detail is copied from).
- **Design spec §4.12** — component library index.
- **The tokens** — `crates/klaffat-web/static/tokens.css`.
- **The lint** — `crates/klaffat-web/tests/template_lint.rs`.
