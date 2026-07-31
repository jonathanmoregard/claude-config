---
name: kablong-components
description: >
  Reusable UI partials + tokens for the kablong / Klaffat web app. Use when
  working in the kablong repo on templates, tokens, or component partials —
  authoring or editing a radio group, button, dialog, form field, or empty
  state; when a template lint warning points at a raw <input type="radio">,
  <button>, or <dialog>; or when writing/adjusting CSS custom properties in
  tokens.css. Also applies for destructive-POST dialogs and any migration
  from an inline control to a component include. Files live under
  crates/kablong-web/templates/_components/ and crates/kablong-web/static/
  tokens.css (typical paths: ~/Repos/kablong, ~/worktrees/kablong-*).
---

# kablong-components — the design-system partial library

The five askama partials under `crates/kablong-web/templates/_components/`
plus the token file at `crates/kablong-web/static/tokens.css`. One place per
component; ARIA rules and subtle details cited in the per-component SOTA
review: `docs/reviews/component-sota-2026-07-31.md` (read that first for the
"why" — this file is the "when + how").

## Discover the partials from a fresh session

```
ls crates/kablong-web/templates/_components/
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
- **c-radiogroup = gallery-v4 Variant A** — segmented-pill row. Structure:
  `fieldset > legend > (hint) > .c-radiogroup__segmented > label*`. Each
  label = a pill with a hidden `<input type="radio">`, a presentational
  12px `.c-radiogroup__dot` circle, and a `.c-radiogroup__label` span with
  mono uppercase text. Segmented rail = 1.5px `--rail` border around the
  row + a 1.5px `--rail` left border on each option (except the first).
  Zero radius. Selected pill inverts (`--rail` bg, `--paper` text) and the
  circle inverts (`--paper` ring around a solid `--paper` dot via
  `radial-gradient`). Focus ring matches c-btn v2C (inset paper+rail).
  Real radio input stays a11y-focusable via `opacity: 0` (NEVER
  `display: none`). Test cost: Playwright's `.check()` on a hidden radio
  fails the actionability visibility gate — target the enclosing
  `label.c-radiogroup__option` instead.
- **c-field = gallery-v8 D1×W1** — 1px solid `--ink` border (warm
  off-black `#1C1B18`, NOT pure `#000`), zero radius, `--paper` bg
  (`#FCFBF8`, warm — not `#FFF`). Tight Gestalt-proximity spacing: 6px
  label→input, ~24px between fields (caller sets the outer stack).
  Control padding `9px 12px`, min-height 40px, sans 14px. Focus ring =
  c-btn v2C DNA (inset paper+rail). Error state swaps border to
  `--strike`.
- **c-empty = gallery-v4 Variant C** — typography-only Superhuman "inbox
  zero" pattern. Vercel Geist geometry: `--s12` (48px) vertical padding,
  centered. 28px (`--fs-xl`) semibold headline with `-0.01em` tracking and
  1.15 line-height, capped at 26rem max-width; one 14px muted supporting
  sentence (`--s3` gap to title); primary CTA at `--s5` gap (btn=v2C DNA).
  Icon slot is DISCOURAGED (default `""`) — the base surface is
  title+body+CTA. If used at all, brand-native illustration only
  (departure-board flap or a monoline `--ink` SVG at 1.5px stroke); never
  generic clipboard/folder/looking-glass.

## What to use when

| Partial | Use when | Anti-slop rules |
|---|---|---|
| `radio_group.html` | A fixed-choice set of ≤3 options (design spec §4.11). Two options for a boolean-ish either/or, three for a small enum. | ≥4 fixed options → use a `<select>` (dropdown) instead. Never a segmented control. |
| `button.html` | Any tap/click affordance that submits, opens a dialog, or triggers a client action. Variants: primary / secondary / destructive / ghost. | Never `<div role="button">`. Never a bare `<a>` styled to look like a button. Always `type="button"` unless inside a form where you actually want submit. |
| `dialog.html` | JS-only confirm/cancel modal with no server action (uses `<form method="dialog">`). | Never a JS modal library — native `<dialog>` gives us focus containment + ESC + `aria-modal` free. |
| `dialog_form.html` | Destructive server-POST modal (delete a resource, revoke a token). Wraps `<form method="post" action="…">` inside `<dialog class="c-dialog c-dialog--danger">`. Params include `dlg_action`, `dlg_method`, `dlg_confirm_variant` (usually `"destructive"`), and `dlg_extra` for hidden inputs / phrase-confirm markup. | `dlg_extra` is rendered `|safe` — it MUST be a compile-time string literal (enforced by the `safe_marked_partial_vars_bind_string_literals_only` test in `tests/template_lint.rs`). If the extra markup needs LOCALIZED text (`loc.t(…)` calls), `dlg_extra` won't work — see the account-settings exception below. |
| `form_field.html` | Standard labeled input with optional hint and error. | Never wrap the input with the label without also using `for="..."` (double-association bug). Hint goes OUTSIDE the label (GOV.UK rule — inside gets re-announced on focus). |
| `empty_state.html` | List/table/dashboard has no items yet. | Never blame the user ("You haven't done X" → "Get started by X"). CTA verb matches the primary action. No stock isometric illustrations, no purple-blue gradients, no glassmorphism. Icon slot discouraged per locked v1 (typography-only default). |

## Tokens — one file, every scale value declared

`crates/kablong-web/static/tokens.css` holds every custom property the app
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

`crates/kablong-web/tests/template_lint.rs` has two checks:

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

Three partials render caller-supplied HTML raw (`|safe`):

- `_components/button.html` → `btn_extra`
- `_components/form_field.html` → `ff_extra`
- `_components/empty_state.html` → `es_icon`
- `_components/dialog_form.html` → `dlg_extra`

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
- **The tokens** — `crates/kablong-web/static/tokens.css`.
- **The lint** — `crates/kablong-web/tests/template_lint.rs`.
