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

$PY cover.py grid  --photo P --out D                    # 1. read landmarks
$PY cover.py check --photo P --config C --out D         # 2. GATE
$PY cover.py build --photo P --config C --out D         # 3. render + verify
```

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
| Mark clears the chin by **16% of its own height** | Fixed as a fraction, never pixels. Grazing contact is the universal amateur tell — commit to a gap or to an overlap, never land between. |
| Nothing load-bearing below **y = 820** | Facebook's title/date/RSVP furniture. |
| Any softening **follows the artwork's silhouette** | A rectangle, plate, or feathered box reads as a smudge floating in the frame. |
| Size comes from the arithmetic | Do not shrink the mark "so it doesn't compete with the faces". Undersizing is the standing complaint. |

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
