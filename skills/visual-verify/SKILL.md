---
name: visual-verify
description: Use whenever a subagent or the parent session has edited a rendered-view file (HTML template, htmx partial, TypeScript/JavaScript rendering component, Rust HTML-returning handler) and the UI has not yet been looked at by human eyes. Enforces a 4-step review rubric with anti-rubber-stamp discipline. Triggers automatically on Write/Edit against `**/*.{html,htm,htmx}`, `templates/**`, `client/**/*.{ts,tsx,jsx}`, and Rust files matching `askama::Template`, `maud::html!`, `axum::response::Html`, `Response::builder()` returning HTML.
---

# visual-verify

Code review, tests, type-check cannot see visual defects. Misalignments,
inconsistent row heights, elements not reaching full width, clipping behind
fixed bars, awkward text wrapping, unreviewed desktop screens, skipped
pages — all pass green and look wrong. **Most of these defects are obvious
once a screenshot is in front of you; the failure mode is not capturing and
looking, not inability to notice.**

This skill is the **review discipline**: a matrix that guarantees coverage,
a checklist applied to every capture, a coverage ledger that proves it. It
is not a capture tool alone. It handles the *looking* — defect detection,
not taste. For opinionated design/taste, request a dedicated UI review;
`advice-refine-test-loop` only consumes this skill's empirical evidence at close-out.
(taste-critic round).

Adapted from HartreeWorks/skill--visual-review and mickzijdel/vischeck.
Both independently converged on this design 2026-06 to 2026-07.

## When to run

**Auto-trigger** whenever a Write/Edit lands on any of:

- `**/*.{html,htm,htmx}` — HTML templates and htmx partials
- `templates/**` — any templating directory (askama, maud, tera, jinja, ERB, EEx, HEEx, templ)
- `client/**/*.{ts,tsx,jsx}` — JS/TS rendering code
- Rust files containing `askama::Template`, `maud::html!`, `axum::response::Html`,
  or `Response::builder()` returning `text/html`

**Skip** for:

- Architecture diagrams, HTML-format explainers, mockups, slides, planning docs,
  Roughdraft artifacts, one-off scratch HTML. These get a quick render smoke-check
  only (Read the file, confirm it looks like a document not a broken page).
- README/CHANGELOG/docs with inline HTML.
- Test fixtures that aren't user-facing.

**Explicit user request overrides every exclusion.**

## First-run install (once per machine)

### NixOS (preferred — flake ships matched python + playwright + browsers)

```
nix run ~/.claude/skills/visual-verify -- --help          # first run cold-builds env (~1 min)
nix run ~/.claude/skills/visual-verify                    # thereafter — reads .claude/visual-verify.yml
```

The skill's `flake.nix` pins `nixpkgs#python3Packages.playwright` +
`nixpkgs#playwright-driver.browsers` to the same nixpkgs commit → no version
mismatch, no `libglib` error, no `uv` needed. First run cold-builds a small
env; subsequent runs are instant.

Interactive iteration on the CLI itself: `cd ~/.claude/skills/visual-verify && nix develop`.

### Non-NixOS (`uv` path)

`bin/screenshot` shebang is `#!/usr/bin/env -S uv run --script` — PEP-723
declared deps (PyYAML, playwright) auto-install on first invocation. Chromium
itself is a separate binary Playwright ships:

```
uv run --script ~/.claude/skills/visual-verify/bin/screenshot -- --help  # bootstrap uv env
uv tool run --from playwright playwright install chromium              # one-time
```

Or, if `uv` isn't available: `pip install PyYAML playwright && playwright install chromium`.

### NixOS uv failure mode (documented for search)

If you try `uv run` on NixOS directly, downloaded chromium fails with
`libglib-2.0.so.0: cannot open shared object file` — non-FHS glibc. The flake
above sidesteps it entirely.

## Setup — generate the project config if none exists

If `bin/screenshot` reports `no project config` and no URL was passed, or you're
setting up a new project, follow this procedure to generate
`<repo>/.claude/visual-verify.yml`. All values discoverable from the repo — do
not guess or ask the user unless a step fails.

**Step 1 — Detect the rendering stack.** Grep for:
- Rust axum: `axum::response::Html`, `askama::Template`, `maud::html!`, `tera::`,
  `minijinja::`, `handlebars::`
- Rust actix/rocket: `HttpResponse::Ok().body`, `rocket::response::content`
- htmx (any language): `hx-get=`, `hx-post=`, `hx-target=`, `hx-swap=`
- Django/Flask/FastAPI: `templates/`, `jinja2`, `render_template`, `Jinja2Templates`
- Rails: `.html.erb`, `render partial:`
- Phoenix: `.heex`, `~H"""`
- Go: `html/template`, `templ.Component`

Record which templating primitives are in use — feeds `app.paths` globs.

**Step 2 — Find the dev-server URL and port.** In priority order:
1. Explicit `.env`, `.env.example`, `.env.dev`: grep for `PORT`, `HOST`, `URL`
2. `Cargo.toml` / `package.json` / `pyproject.toml` for framework config
3. Config files: `axum::Router::bind`, `TcpListener::bind`, `app.run(port=)`
4. `Playwright` config (`playwright.config.ts` / `.js`): `webServer[].url`
5. `docker-compose.yml` `ports:` mapping
6. README for a "how to run" section

Default: `http://localhost:3000` (Node), `http://localhost:8000` (Python),
`http://localhost:3000` (Rails), `http://localhost:8080` (Java/Go), or
whatever the framework's convention is. Never guess a port from thin air.

**Step 3 — Find view-file paths.** Glob for the templating extensions
identified in step 1. Common patterns:
- `templates/**/*.html` (many)
- `crates/*/templates/**` (Rust workspace)
- `app/views/**/*.erb` (Rails)
- `client/**/*.{ts,tsx}` (SPA client rendering)
- Any source file matching stack primitives from step 1

Enumerate concrete paths; do not use `**/*` (too broad, matches vendored code).

**Step 4 — Find the dev-auth mechanism.** For signed-in surfaces:
1. Grep for an existing dev/test-auth bypass: `DEV_AUTH`, `TEST_AUTH`,
   `debug_assertions`, `#[cfg(feature = "test-endpoints")]`, `if env::var("BYPASS")`
2. If none exists, note it: agent should build one (an axum extractor gated by
   `cfg(debug_assertions)` OR an env-check-guarded feature). Config `dev_auth`
   section can be omitted; screenshot CLI will hit the auth wall and exit non-zero.
3. Convention: header `X-Dev-Auth`, env var `<PROJECT>_DEV_AUTH_TOKEN`.

**Step 5 — Find the readiness selector.** Not `networkidle`. Look for:
1. Templates that mark hydration: `data-ready`, `data-hydrated`, `#app.ready`
2. A stable landmark that appears on every page: `main`, `#main`, `[role="main"]`
3. If no explicit ready state exists, default to `main`.

**Step 6 — Find ports to free before capture.** Grep tests + Playwright config
for hardcoded ports: `fuser -k`, `webServer[].port`, `TcpListener::bind(":XXXX")`,
integration-test port constants. Include the dev-server port and any mock/side-service ports.

**Step 7 — Find known-good components dir + design docs.** Check:
1. `templates/_components/`, `templates/partials/`, `templates/shared/`, `app/components/`
2. `docs/design/`, `docs/brand/`, `docs/reviews/` for a component or design SOTA doc
3. A design-tokens file: `tokens.css`, `tokens.json`, `styles/tokens.*`

If none present, omit the `known_good:` section — agent can add it later once
a component library exists.

**Step 8 — Write the file.** Path: `<repo>/.claude/visual-verify.yml`. Use the
shape below. Only include sections whose values you actually derived; omit the
rest (defaults kick in). Add a one-line comment above each section citing the
grep or file the value came from — makes it maintainable.

**Step 9 — Verify.** Run `~/.claude/skills/visual-verify/bin/screenshot` with
no URL. It should print `[visual-verify] loaded project config: <path>` and
either capture (if server up + auth wired) or fail with the specific reason
(server down / auth wall / bad selector). Fix the config until it captures a
recognizable public page.

## Project config (composition pattern)

Skills at user scope override same-named project skills — so per-project
overrides can't work via a name-collision. Instead, this global skill READS
a per-project config file:

**Path**: `<repo>/.claude/visual-verify.yml` (project root, gitignored or
tracked per project preference).

**Shape** (YAML, all fields optional; defaults sensible):

```yaml
app:
  base_url: http://localhost:3737         # dev server URL
  ready_selector: "main"                  # readiness (NOT networkidle)
  paths:                                  # globs classifying this project's rendered-view files
    - "templates/**/*.html"
    - "client/**/*.ts"
    - "src/**/*.rs"                       # e.g. Rust HTML-returning handlers
dev_auth:
  header: X-Dev-Auth                      # or `cookie: name=value`
  token_env: KLAFFAT_DEV_AUTH_TOKEN       # env var the CLI + wrapper read
free_ports:                               # ports to kill before capture (surgical, never pkill)
  - 3737
  - 13737
screenshots:
  batch_file: screenshots.yml             # relative to repo root; drives batch groups
  out_dir: docs/brand/screenshots/vv      # where captures land
known_good:
  components_dir: crates/klaffat-web/templates/_components
  reference_doc: docs/reviews/component-sota-2026-07-31.md
```

`bin/screenshot` reads this file if present in cwd or any ancestor. Callers
can still pass explicit flags to override. Nothing is required — a fresh
project with no config uses defaults + a `main` readiness selector.

## Trigger mechanism

Use both:

### 1. Bake into subagent briefings (primary)

Every implementer subagent prompt orchestrating UI work must include this
paragraph verbatim near the top:

> **Visual-verify discipline (mandatory):** After any Write/Edit against a
> file matching `**/*.{html,htm,htmx}`, `templates/**`, `client/**/*.{ts,tsx,jsx}`,
> or a Rust HTML-returning handler, invoke the `visual-verify` skill on the
> touched surface before proceeding to the next file. A bare "looks good" is a
> failure. Report severity-tagged findings and either fix or explicitly defer.

### 2. Parent post-return git-diff verify (defense in depth)

When a subagent returns, the parent runs:

```
git diff --name-only <parent-branch>..HEAD -- \
  '**/*.html' '**/*.htm' '**/*.htmx' 'templates/**' \
  'client/**/*.ts' 'client/**/*.tsx' 'client/**/*.jsx'
```

If any matching path appears, invoke `visual-verify` on the affected surfaces
before marking the subagent's work done. Rust handlers grep separately for
`askama::Template|maud::html!|axum::response::Html`. This is the enforcement
that CANNOT be skipped by an inattentive subagent.

## The 4-step rubric

Apply to EVERY captured surface. A bare verdict of "looks good" is a **failure
of this skill.** Your default instinct will be to approve — resist it. UIs that
are subtly wrong (inputs that don't match the house style, cluttered cards,
low-contrast text) look fine at a glance and are exactly what this step exists
to catch.

### Step 1 — Zoom in

A full-page shot is too small to judge detail. Take a focused shot of the
component you just changed:

```
visual-verify screenshot <url> --selector "<css>" --out <path>
```

Read the resulting PNG with the Read tool. Do not skip this — the Read is what
forces the model to actually see the pixels.

### Step 2 — Compare against a known-good component

You cannot know what "correct" looks like from one screenshot in isolation.
Screenshot an existing, known-good component of the same kind (a radio group
elsewhere in the app for a new radio group; the existing button for a new
button; etc.) and compare side by side. **Any divergence is a finding.**

If the project defines a component library or partials directory, prefer
those as the reference. Project-scoped skills or CLAUDE.md sections should
name the canonical path.

### Step 3 — Walk the checklist

Every check named explicitly. Skipping is a failure.

- **House-style conformance** — Does spacing, typography, color, radius match
  the existing app? Divergences that aren't intentional are findings.
- **Contrast & legibility** — WCAG AA at minimum (4.5:1 for body text, 3:1
  for large text and UI). Use browser devtools if unsure.
- **Spacing & alignment** — Elements share a spacing scale? Rows same height?
  Grid columns aligned? Nothing off-baseline?
- **Clutter & density** — Too many colors, too many weights, information not
  organized into hierarchy?
- **Typography** — Font stack loaded (not FOUT), line-height readable, no
  orphans/widows in headings, letter-spacing consistent.
- **Truncation & overflow** — Text wraps sensibly, no clipping, no
  horizontal scroll, no text spilling out of buttons/cards.
- **States** — focus, hover, disabled, error, loading — **exercise them
  with playwright-cli, don't assume.**
- **Responsive** — Boundary widths `[390, 767, 768, 1280]`. Capture at both
  breakpoint edges, not only representative widths.
- **Anti-slop** — No generic-AI aesthetic (see below).

### Step 4 — Report findings, then verdict

Every finding tagged one of:

- **blocker** — ship-stops. Broken layout, unreadable text, missing action.
- **should-fix** — obvious visual defect that the founder would flag.
- **nit** — subjective polish; do only if trivial.

Only after walking the checklist may you conclude "no findings" — and say
**which checks you ran** to get there. "Looked at it, seems fine" is a fail.

## Anti-slop guardrails

Per Anthropic `frontend-design` skill ("avoid generic AI aesthetics") and
practitioner blogs (design-bootcamp, aidesigner.ai) 2026, flag these as
should-fix unless deliberately chosen and documented:

- Generic-blue CTA (default `#3B82F6` shadcn/Tailwind blue)
- Purple → blue gradient CTAs or backgrounds
- Glassmorphism (frosted-glass `backdrop-filter` on cards)
- Stock illustration (isometric-purple person-with-laptop)
- Emoji-in-heading substituting for icon
- Card grid where a list would do
- "Coming soon" placeholder that isn't
- Everything-center-aligned (no left-alignment for prose)

## Capture — see `references/capture-recipes.md`

Load the recipes file for the Python Playwright matrix loop (boundary widths,
DPR=2, fresh context per state, `document.fonts.ready`, readiness selectors
not `networkidle`). CLI wrapper at `bin/screenshot`.

## Observability artifacts (per shot)

Each capture emits six files (default: all on; opt-out via `--no-aria` /
`--no-dom` / `--no-source`):

- `<shot>.png` — DPR=2 image, founder-review primary
- `<shot>.aria.yaml` — Playwright's `page.locator(sel).ariaSnapshot()` output; the
  accessibility tree the critic reads INSTEAD of guessing structure from pixels.
  Per 2026 SOTA research (`docs/reviews/sota-ai-design-web-research-2026-07-30.md`
  recommendation #1): agents drive real browser + read ARIA snapshots as primary
  signal, not pixels. Cuts hallucinated findings.
- `<shot>.dom.json` — box-model dump of every landmark/heading/control in view:
  `{tag, role, id, class, rect{x,y,w,h}, font, color, bg, pad, border, label}`.
  Grounds VLM critique in real geometry — spacing/alignment/contrast findings
  can be verified against numeric bboxes, not hallucinated.

- `<shot>.source.html` — post-render serialized DOM (the browser's actual tree,
  not the served template). With `--selector`, scoped to that element.
- `<shot>.source.css` — every stylesheet that applied, rules as parsed,
  `/* ===== <href> ===== */` separated. Cross-origin sheets are re-fetched;
  anything unreadable is recorded as `NOT CAPTURED: <reason>` instead of
  silently vanishing.
- `<shot>.source.js` — every script that shipped: inline bodies verbatim,
  external bodies fetched, `// ===== <src> =====` separated.

The three `source.*` files are what let a review agent map per-page feedback
onto real markup — geometry (`dom.json`) says WHERE, source says WHAT TO EDIT.
Any UI review handed to a human must ship them alongside the images.

Contact-sheet `index.html` links `aria.yaml`, `dom.json` and `src: html/css/js`
next to each thumbnail. CLI markers: `[aria] [dom] [src]`, failures as `[src!]`.

Critic prompts should include the aria + dom paths and instruct: "cite specific
`role` and `rect` when flagging alignment / spacing; do not claim misalignment
without a bbox delta from `dom.json`."

## What this skill is NOT

- Not a taste critic. Request a dedicated UI review when taste judgment is required.
- Not a pixel-baseline visual regression tool. We don't run those (see
  `docs/reviews/sota-ai-power-users-cohort-2026-07-30.md` gap analysis).
- Not an a11y auditor. `axe-core` optional; run it separately if you want
  programmatic WCAG assertions.
- Not a substitute for founder review. Filters obvious defects so the founder
  sees only surfaces worth their eye.
