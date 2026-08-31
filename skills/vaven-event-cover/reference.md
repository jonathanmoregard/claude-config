# VÄVEN event cover — constants and methods

## Output spec

| | |
|---|---|
| Upload size | **1920 × 1005** (1.91:1) |
| Guaranteed on mobile | the centred **1005 × 1005** square — mobile crops the **sides** |
| Feed card | renders ~**560 px** wide; judge legibility there |
| Facebook furniture | starts eating the image at **y = 820** |
| Mark's bottom limit | **796** (820 − 24) |
| Format | flatten to RGB, hand over a quality-95 JPEG (no chroma subsampling) alongside the PNG |

Meta publishes no formal spec; 1920 × 1005 is the community-validated size and
was re-confirmed current. The uploader always shows a crop/reposition step even
for a correctly-sized file — it derives several crops and needs a focal point.
That is a UI artifact, not a wrong file.

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
h     = (796 − chin_output) / (1 + 0.16)      logo height
top   = chin_output + 0.16 · h
w     = h · 1.4041
```

Headroom must clear the group's topmost point (arms/tools) by ~38 px or it
grazes the canvas edge. Cropping that point out is the lever that buys a bigger
mark — offer it as an explicit rung rather than taking it silently.

## Contrast method

Render each variant **twice, with and without the mark**. Diff to recover the
mark's own pixels.

- Solid pixels only: `diff > 90`, then erode 1 px. Antialiased edges blend with
  the ground by definition and drag every number toward 1:1.
- At each pixel: **`max(ratio(fill, bg_px), ratio(stroke, bg_px))`**.
- Report mean, 5th percentile, and % under 3:1.

**Never score stroke pixels alone against the ground.** The stroke's job is to
fence the fill off, not to out-contrast the ground. That mistake floors every
candidate near 49% and reads as a design failure when it is a metric failure.

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
near-black stroke nearly matches the halo, diff-based recovery loses those
pixels, and the check reports a bogus ~84%.

## Tooling

```
inkscape  /nix/store/30xl2hk1k441jryb4pni78xihkn5kksp-inkscape-1.4.4/bin/inkscape
python    /nix/store/a68sj1q3b4x85zd7sl3ahpbyiarnclxy-python3-3.14.6-env/bin/python3
```

That python is Inkscape's bundled environment and is the one with **numpy and
PIL**; the system python3 has neither. Do not try
`nix shell "nixpkgs#python3.withPackages(...)"` — not valid installable syntax.

Serve a review page with `systemd-run --user --unit=… --collect
--working-directory=…`; backgrounding with `setsid nohup … &` returns exit 124
because the child holds the pipe, and the server is running anyway.

Never delete build outputs. `shutil.copytree(..., dirs_exist_ok=True)`, not
`rmtree`.

## Photo requirements to ask for

- Highest resolution available of the exact frame. About 1540 px of width gets
  used, so a 1919 px source is upscaled ~25%.
- No baked-in letterbox bars (these exports carry several rows of black at top
  and bottom).
- Room above the group's raised arms, not just above their heads.
