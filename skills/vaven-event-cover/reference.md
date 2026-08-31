# VÄVEN event cover — constants and methods

## Output spec

| | |
|---|---|
| Upload size | **1920 × 1005** (1.91:1) |
| Guaranteed on mobile | the centred **1005 × 1005** square — mobile crops the **sides** |
| Feed card | renders ~**560 px** wide; judge legibility there |
| Facebook furniture | **soft** — see below. Desktop does not overlap at all; mobile fades the bottom |
| Mark's bottom limit | a judgement, not a line. `ui_top` **900** shipped (mark bottom 876) |
| Format | flatten to RGB, hand over a quality-95 JPEG (no chroma subsampling) alongside the PNG |

Meta publishes no formal spec; 1920 × 1005 is the community-validated size.
Re-confirm it before a new cover — search for the current event-cover size, and
treat a source that only quotes page-cover sizes (820 × 360, 1200 × 628) as
stale. The uploader always shows a crop/reposition step even for a
correctly-sized file: it derives several crops and needs a focal point. That is
a UI artifact, not a wrong file.

## The logo

Source: `väven-invite-fb.svg`. **One `<path>` is the entire lockup**, wordmark
and Celtic knot together. The `<image>` in that file is an embedded old source
photo — not part of the mark, ignore it.

In the original SVG's user units:

| | |
|---|---|
| visual bbox | x 77.6287–223.5901, y 60.2358–164.1927 |
| w × h | 145.9614 × 103.9569 |
| aspect ratio | **1.4041** |
| wordmark band | top **38.1%** of logo height |
| **true mirror axis** | **x = 150.7778 uu = 50.115% across the bbox** |

The mirror axis was solved on the **knot alone** (96.97% mirror IoU). The
wordmark is not mirror-symmetric — whole-mark IoU tops out at 79.40% — so
centring the visual bbox is 1.9 px off.

**Measure mirror symmetry on the knot band only, against the 96.97% ceiling.**
Measuring the whole mark against a 90% bar reports a property of the artwork as
a placement bug and falsely flags everything.

### Colourways

| | fill | stroke | use |
|---|---|---|---|
| stock | `#ffab2c` | `#9f5a1e` | flat dark grounds only |
| **deepened outline** | `#ffab2c` | `#331b05` | **default on a photo** |
| cream fill | `#f7ecd9` | `#331b05` | strongest measured, but a real mark change — ask first |

Single-colour is rejected: it fills the knot into a solid blob, because the
over/under weave is drawn as fill-against-stroke.

## Layout arithmetic

With no type field, the photo full-bleed:

```
MAXW  = 2 · min(GX, SW − GX)      widest crop still centred on the group
SBASE = 1920 / MAXW               smallest usable scale; wider stops being centred
chin_output = (CHIN − HEADS) · SBASE + headroom
bottom = ui_top − 24              the mark's baseline (876 at the 900 default)
band  = bottom − chin_output      clear room between the lowest chin and the UI
w     = width_frac · 1920         SIZE IS AN INPUT — see below
h     = w / 1.4041
top   = bottom − h                bottom pinned to that baseline, always
gap   = (top − chin_output) / h   an OUTCOME, measured and reported
```

**Size is chosen, not solved.** An earlier version derived the height from
"clear the chin by 16% of the mark's own height", which let one fitted constant
set the size, the position and the ceiling at once — and capped a photo at 24.8%
of canvas width when 28.7% fitted. Give `widths` as fractions of **canvas
width**, the unit the client judges in and the only one comparable between
photos. Omit it and you get the widest mark that fits the band whole.

Pinning the **bottom** is what makes a ladder legible: every rung shares one
baseline, so the fan-out varies size and nothing else. Centring in the band was
tried and dropped — it makes the gap `(band − h)/2`, which crawls through zero,
so every width from 24% to 35% of canvas landed in the grazing dead zone.

**Pick rungs outside the dead zone.** Grazing is `|gap| < 8%` of mark height, so
with the bottom pinned, heights in `[band/1.08, band/0.92]` are unusable. Those
map to a width range you compute once per crop and then step around; `check`
prints the band and the ceiling for you.

### The binding chin is span-local

`chin` alone is the lowest chin **anywhere in the photo**, and it over-constrains
every mark that never reaches that person. On `image-EDIT (2).jpg` the lowest
chin sits 274px off the axis; it only enters a mark's span past 26.9% of canvas
width, yet it was capping every width below that too. Give `chins` — one
`{label, x, y}` per face — and the engine binds on the lowest chin the mark
actually passes under, plus a 16px margin for the halo's reach.

Grazing is then checked against **every** chin in the span, not just the lowest.
Once overlap is allowed, a mark can clear the lowest chin by a committed margin
and still land a few pixels off a higher one — same amateur tell.

**The ceiling is scanned, not solved.** The binding chin depends on the width and
the width depends on the binding chin, and the relationship is not monotone:
there is a cliff where an off-axis chin enters the span and the band collapses.
Iterating to a fixed point oscillates across it and settles below the real
ceiling — measured, 25.6% against a true 26.9%. `ceiling_width` scans down.

### `ui_top` — a soft zone, not a line

`ui_top` is the single biggest lever on mark size: at the old default of 820 it
reserves 185px of an 1005px canvas. On `image-EDIT (2).jpg` the ceiling goes
26.9% → 30.8% → 37.7% as `ui_top` goes 820 → 900 → 1005.

**What was actually observed (Jonathan, 2026-08-31, live event pages):**

| surface | what the furniture does to the cover |
|---|---|
| desktop web | **nothing** — title/date/RSVP sit in a block *below* the photo, which renders in full |
| mobile | the image **fades out** toward the bottom — a gradient, not an opaque bar and not a crop |

So there is no occlusion line, and 820 was never one. The bottom of the canvas
is a *softening* zone: content there loses contrast rather than disappearing.
That changes the question from "does it clear the furniture" to "can it afford
to be dimmed", which is a taste call on the specific artwork.

Shipped on that basis: `ui_top = 900`, mark width 30.8%, bottom at 876. The
knot's lower lobes do reach the fade and that was judged fine — the wordmark,
which is the top 38.1% of the mark, is nowhere near it. The 37.7% rung was
rejected for leaving only 24px under the mark, which reads as an accident
independently of anything Facebook draws.

Do not restore 820 as a default on the strength of a web search. It is
community-derived folklore, Meta publishes no spec, and the two claims this file
used to carry — "mobile crops the sides" and "furniture starts at y=820" — were
never reconciled with each other. Six research runs on this question were
rejected by the scanner; the observation above cost one look at a phone.

When a client says "there's plenty of empty space at the bottom", render the
`ui_top` ladder rather than re-explaining the constant — the images settle it.

Headroom must clear the group's topmost point (arms/tools) by ~38 px or it
grazes the canvas edge. Cropping that point out is the lever that buys a bigger
mark — offer it as an explicit rung rather than taking it silently. `headroom`
and `widths` cross-multiply, so a width that is safe on one crop and grazing on
another needs **two configs and two output dirs**, not one cross product.

## Contrast method

Render each variant **twice, with and without the mark**. Diff to recover the
mark's own pixels.

- Solid pixels only: `diff > 90`, then erode 1 px. Antialiased edges blend with
  the ground by definition and drag every number toward 1:1.
- At each pixel: **`max(ratio(fill, bg_px), ratio(stroke, bg_px))`**.
- Report mean, 5th percentile, and % under 3:1.

**Never score stroke pixels alone against the ground.** The stroke's job is to
fence the fill off, not to out-contrast the ground. Scoring it alone measures
the wrong thing, understates every candidate, and reads as a design failure
when it is a metric failure.

Targets: mean ≥ 5, p05 ≥ 3, under 5% of mark pixels below 3:1.

## The halo

Canonical outer-glow construction:

```
render the mark ALONE on transparency  →  take its ALPHA channel
halo = clip(GaussianBlur(alpha, sigma) · 2.6, 0, 1)

glow:  photo = sharp·(1 − halo·0.88) + #4a2708·(halo·0.88)
pad:   photo = sharp·(1 − halo) + blurred·halo        (blur radius 22)
```

The SVG-filter equivalent is `feMorphology(dilate)` + `feGaussianBlur` +
`feFlood` + `feComposite` + `feMerge`. Blurring the alpha and boosting by a gain
is the same operation without the dilate pass — the gain replaces it. Doing it
in numpy lets the result be measured before it is shown.

Thickness ladder (sigma → measured reach past the artwork): 8 → 11 px,
16 → 19 px, 28 → 27 px, 44 → 35 px. **Sigma 8 is the chosen setting.** Measure
thickness as halo band area ÷ mark perimeter, not nominal sigma.

**Take knot symmetry from the alpha render, not the diff.** On a dark glow the
near-black stroke nearly matches the halo behind it, so diff-based recovery
loses those pixels and badly understates the symmetry — a property of the
treatment reported as a placement fault.

## Tooling

`cover.py` needs Inkscape plus a python carrying **numpy and PIL**. The system
python3 has neither.

Durable recipe — get both from nixpkgs, no pinned hashes:

```bash
nix shell nixpkgs#inkscape -c inkscape --version
nix shell nixpkgs#python3Packages.numpy nixpkgs#python3Packages.pillow \
  -c python3 -c "import numpy, PIL; print(numpy.__version__, PIL.__version__)"
```

Or run the whole thing inside one shell:

```bash
nix shell nixpkgs#inkscape nixpkgs#python3Packages.numpy nixpkgs#python3Packages.pillow \
  -c python3 cover.py check --photo P --config C --out D
```

`cover.py` finds Inkscape via `$INKSCAPE` or `PATH`, so
`export INKSCAPE=$(command -v inkscape)` is enough.

These `/nix/store` paths worked at time of writing and are a fast path **only
while they exist** — after a GC they dangle, so fall back to the recipe above:

```
inkscape  /nix/store/30xl2hk1k441jryb4pni78xihkn5kksp-inkscape-1.4.4/bin/inkscape
python    /nix/store/a68sj1q3b4x85zd7sl3ahpbyiarnclxy-python3-3.14.6-env/bin/python3
```

That python is Inkscape's own bundled environment, which happens to carry numpy
and PIL. Do not try `nix shell "nixpkgs#python3.withPackages(...)"` — not a
valid installable, nix reads it as a path and fails.

Serve a review page with `systemd-run --user --unit=… --collect
--working-directory=…`; backgrounding with `setsid nohup … &` returns exit 124
because the child holds the pipe, and the server is running anyway.

Never delete build outputs. `shutil.copytree(..., dirs_exist_ok=True)`, not
`rmtree`.

## Where the files go

| | |
|---|---|
| Working dir | `/tmp/claude-tmp-data/vaven-<slug>/`, one subdir per crop |
| Review page | `/tmp/claude-tmp-data/vaven-<slug>-review/index.html` |
| Handover | copy the chosen `upload-*.jpg` / `.png` somewhere durable, name the path |

`/tmp` is in `permissions.additionalDirectories`, so Claude reads and writes
under `/tmp/claude-tmp-data/` with no prompt and no `.claude`-protected-path
friction. Nothing else needs configuring — do **not** add a narrower rule for a
subdirectory of a directory that is already granted.

A build writes `grid.png`, three zooms, the band strips, `check-*.png`, and for
every variant a background, an SVG, a no-logo SVG, two renders and two upload
files. That is scratch. It does not go in `~/Downloads`, which is the user's,
not Claude's.

## Ruled out — do not retry

- **Invented decorative devices** — cards, arches, frames, plates. Use forms
  from research or existing resources; invented ones look silly.
- **Masthead overlap** (heads occluding the type). Needs a per-pixel cutout the
  photo will not give — a pale shirt matches the wall.
- **Tucking the mark behind a panel edge.** Cuts the knot mid-lobe and reads as
  truncation.
- **A single-colour mark.** Fills the Celtic knot into a blob; the weave is
  drawn fill-against-stroke.
- **Flush-left placement.** Reads as off-centre by mistake and leaves the
  guaranteed mobile square.

## Presenting variants

Build a **scroll page**, not the arrow-key review tool. Include a 1:1 **detail
crop** per variant — a halo is a few dozen pixels and a full-canvas thumbnail
throws it away. Show desktop, the mobile square, and the 560 px feed card.

Read the variant list from `report.txt`, never a directory glob; a glob picks up
stale renders from superseded builds.

When a trade is genuinely unavoidable, present it as a ladder with the numbers,
not as several different "designs".

## Photo requirements to ask for

- Highest resolution available of the exact frame. About 1540 px of width gets
  used, so a 1919 px source is upscaled ~25%.
- No baked-in letterbox bars (these exports carry several rows of black at top
  and bottom).
- Room above the group's raised arms, not just above their heads.
