---
name: vaven-event-cover
description: Use when making a Facebook event cover, invite image, or any photo-plus-logo artwork for the VÄVEN collective — including "new VÄVEN event image", "put the VÄVEN logo on this photo", being handed a fresh group photo to lay the mark over, or replacing an existing cover with a new photo.
---

# VÄVEN Event Cover

## Overview

The mark goes **on the group**, below the lowest chin, on the canvas centre
line, with a soft halo following its own outline. The photo stays whole and
full-bleed.

Every number below is measured, never estimated. Two things decide whether the
result is professional or amateur, and both are checked before anything is
rendered: **symmetry** (the mark on the group's axis, and the mark's own mirror
axis intact) and **placement** (where it sits relative to the chins, the crop,
and Facebook's furniture).

## Workflow

Run all three. Do not skip to `build`.

```bash
export INKSCAPE=/nix/store/…/bin/inkscape     # see reference.md for both paths
PY=/nix/store/…-python3-…-env/bin/python3     # needs numpy + PIL
D=/tmp/claude-tmp-data/vaven-<slug>           # working dir; see below
mkdir -p "$D"

$PY cover.py grid  --photo P --out "$D"                 # 1. read landmarks
$PY cover.py check --photo P --config C --out "$D"      # 2. GATE
$PY cover.py build --photo P --config C --out "$D"      # 3. render + verify
```

**Work under `/tmp/claude-tmp-data/`, not `~/Downloads`.** A run leaves dozens of
grids, zooms, band strips, per-variant backgrounds and no-logo renders; that is
scratch, and it does not belong in the user's downloads folder. `/tmp` is already
an additional working directory, so Claude can read and write there without a
permission prompt, and the dir is Claude's to manage. It is also **ephemeral** —
copy the chosen `upload-*.jpg` / `upload-*.png` somewhere durable and hand over
that path, along with the review page.

**1. Measure.** `grid` writes a percentage overlay and 1%-gridline zooms. Read
every landmark off the zooms. Full-frame eyeball reads run ~2% out, and 2% of
the source height is ~28 output px of logo height.

Read two independent axis estimators, not one: `faces_centroid` (centroid of
the face centres) and `head_span_centre` (midpoint of the whole head span). One
estimator cannot be checked; two can.

`top` is the **highest point the group owns — raised arms, tools, a tape
measure. It is usually not a head.** It sets the headroom.

**2. Gate.** `check` fails loudly and exits non-zero on: axis estimators
disagreeing by more than 1.5% of width, knot mirror IoU below the artwork's
ceiling, the mark grazing the chins, the mark under Facebook's furniture, or
anything falling outside the guaranteed mobile square.

Passing the gate is not enough. **Open both images and look:**
- `check-landmarks.png` — are the lines on the features you think they are?
- `check-placement.png` — is this a good place for the mark? Shows the mark's
  box, the axis, the chin line, the furniture line, and the mobile square.

**3. Build.** Renders each treatment twice, with and without the mark, diffs to
recover the mark's own pixels, and verifies. Writes flattened `upload-*.jpg`
and `upload-*.png`. Exits non-zero if any variant is flagged.

## Non-negotiables

| Rule | Why it bites |
|---|---|
| Mark on the **canvas centre line** | Mobile crops the **sides**; only the centred 1005×1005 square is guaranteed. Off-centre placement is destroyed there. |
| Photo **full-bleed and whole** | Giving the mark its own coloured field caps it near 18% of canvas width and spends half the canvas. |
| Mark's bottom sits on **y = 796**, always | The one anchor that is not a taste call. It also aligns every rung of a size ladder on one baseline, so a fan-out varies size and nothing else. |
| Never land within **8% of the mark's height** of the chin | Either way — a clear gap and a committed overlap are both fine, grazing contact is the universal amateur tell. Pick rungs around the dead zone, don't tune into it. |
| Nothing load-bearing below **y = 820** | Facebook's title/date/RSVP furniture. |
| Any softening **follows the artwork's silhouette** | A rectangle, plate, or feathered box reads as a smudge floating in the frame. |
| Size is an **input**, in % of canvas width | Do not shrink the mark "so it doesn't compete with the faces". Undersizing is the standing complaint. And do not let a gap rule solve the size for you — see below. |

## Don't let a fitted constant solve the layout

The first version of this skill derived the mark's height from "clear the chin
by 16% of its own height". One constant then set the size, the position and the
ceiling at once — and capped a photo at 24.8% of canvas width when 28.7% fitted.
**That is overfitting, and it is invisible** because the number it produces
still passes every gate.

Give `widths` — a list of fractions of **canvas width**, the unit the client
judges in and the only one comparable between photos. `check` prints the
chin-to-furniture band, the width that fills it, and the whole ladder with the
gap each rung produces. The gap is measured, never targeted.

`headroom` and `widths` cross-multiply, so a width that is safe on one crop and
grazing on another needs **two configs and two output dirs**, not one config
with a cross product that renders combinations you would never show.

The same trap sits in the chin. `chin` is the lowest chin **anywhere in the
photo**; a mark that never reaches that person should not be constrained by
them. Give `chins` — one `{label, x, y}` per face — and the binding chin becomes
span-local. Measured on `image-EDIT (2).jpg`: 26.0% → 26.9% of canvas width, and
more importantly the *reason* for the ceiling changes from a number to a person
you can point at.

## When the client says there's empty space at the bottom

They are looking at the **`ui_top` reservation** — 185px of an 1005px canvas that
Facebook draws its title, date and RSVP button over. It is the biggest lever on
mark size and the shakiest constant here: Meta publishes no spec, 820 is
community-derived, and a `fast` web search returns page-cover sizes (820×312,
851×315) that are not this.

Do not re-explain the constant. Override `ui_top` and render the diagnostic — on
`image-EDIT (2).jpg` the ceiling runs 26.9% → 32.0% → 39.1% of canvas width as
`ui_top` goes 820 → 900 → 1005. Two images settle in one look what a paragraph
does not.

## Re-measure for every photo

**Landmark fractions never transfer between source files, even when the
dimensions are nearly identical.** A file 3 px taller can be a different edit
with the people in different places. To tell, align the two frames over ±6 row
offsets; if the best case is not near-zero mean absolute difference, it is a
different picture — re-read everything.

These exports also carry a few rows of **black letterbox** baked into the top
and bottom edges. `cover.py` refuses a crop that reaches into one.

## Legibility

**Blur destroys detail, not tone.** Blurring the background does not make a
warm mid-toned mark legible on a warm mid-toned room. Never reach for it as a
contrast fix.

Use the **deepened outline** (`#331b05` stroke) — it separates from a room
everywhere with no other treatment.

Measure contrast **per pixel, never against a mean**: a mean is the whole truth
on a flat ground and a lie on a photograph. `cover.py build` does this and
flags failures; method and thresholds in `reference.md`.

## Never retry these

**No invented decorative devices — no cards, arches, frames, or plates behind
the mark.** Use forms from research or existing resources; invented ones look
silly. Four more approaches are already ruled out with reasons: see
`reference.md`.

## Common mistakes

| Mistake | Instead |
|---|---|
| Putting the mark in the photo's "empty space" off to one side | Centre line. The empty space is outside the mobile crop. |
| Sampling one background colour and running WCAG on it | Per-pixel, then read the 5th percentile. |
| Treating the logo SVG as wordmark-plus-ornament | One `<path>` is the entire lockup. The `<image>` in that file is an old photo — ignore it. |
| Centring the mark's visual bounding box | Its true mirror axis is 50.115% across the bbox. Bbox centring is 1.9 px off. |
| Guessing Facebook's safe margins | 820. |
| Assuming the crop step in the uploader means the file is wrong | It always asks. Leave it centred, pull to full width. |

## Presenting variants

Build a **scroll page**, not the arrow-key review tool, with a 1:1 detail crop
per variant. Rules in `reference.md`.

**Constants, layout arithmetic, contrast and halo methods, tooling paths:**
`reference.md`.
