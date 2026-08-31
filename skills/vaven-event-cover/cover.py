#!/usr/bin/env python3
"""VÄVEN event-cover engine: measure a photo, place the mark, verify the result.

Three subcommands, run in this order:

    cover.py grid  --photo P --out D
        Percentage-grid overlay plus per-person zooms at 1% gridlines. Read the
        landmarks off these, not off the full frame.

    cover.py check --photo P --config C --out D
        Draws the config's landmarks back onto the photo so you can see whether
        you read them right. ALWAYS run this before build.

    cover.py build --photo P --config C --out D
        Renders every treatment, verifies each, writes report.txt.

Config is JSON, all landmarks as fractions of the source:

    {
      "axis": 0.599, "face_l": 0.445, "face_r": 0.765,
      "heads": 0.265, "top": 0.190, "chin": 0.450,
      "treatments": ["glow", "pad"], "sigma": 8.0,
      "headroom": "auto", "widths": [0.20, 0.24, 0.28, 0.33],
      "logo_svg": "vaven-mark.svg"
    }

`logo_svg` resolves relative to the config file, and the mark ships with the
skill, so a config stays valid wherever the skill is checked out. `headroom` may
also be a LIST of rungs; each gets its own named output.

`widths` is the mark's size ladder, as fractions of CANVAS WIDTH -- the unit the
client judges in. Size is an INPUT here; the chin gap that falls out is measured
and reported, not the other way round. Every rung sits on one baseline just above
Facebook's furniture, so a fan-out varies size and nothing else; a rung taller
than the chin-to-furniture band overlaps the group deliberately. The two ladders
multiply: headroom changes the CROP, widths change the MARK inside a fixed crop.

`top` is the topmost point the GROUP owns -- raised arms and tools, not heads.
`headroom: "auto"` clears that point by 38 px; a number overrides it, and a
number small enough to crop it is how you buy a bigger mark.

Requires numpy + PIL. On this machine the system python3 has neither; use
Inkscape's bundled environment (see SKILL.md for both paths).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# --- output spec ------------------------------------------------------------
CW, CH = 1920, 1005          # Facebook event cover, 1.91:1
AXIS = CW / 2
UI_TOP = 900.0               # Where the mark's baseline sits. NOT an occlusion
                             # line — observed 2026-08-31, desktop web does not
                             # overlap the cover at all and mobile fades the
                             # bottom out. 900 is the rung that shipped; the old
                             # 820 was community folklore reserving 185px for
                             # furniture that is not drawn there. Override per
                             # photo, and see reference.md before "restoring" 820.
UI_CLEAR = 24.0              # deliberate air between the mark and that line
CHIN_MARGIN = 16.0           # a chin this close to the mark's edge counts as
                             # under it; covers the halo's ~11px reach
GRAZE_FRAC = 0.08            # dead zone either side of the chin, as a share of
                             # mark height: closer than this to touching, in
                             # either direction, reads as an accident

# --- logo, measured once off the artwork; do not re-derive ------------------
L_AX = 150.7778              # TRUE mirror axis, solved on the knot alone
L_X0, L_Y0 = 77.6287, 60.2358
L_W, L_H = 145.9614, 103.9569
RATIO = L_W / L_H            # 1.4041
WORD_FRAC = 0.381            # wordmark band as a share of logo height
KNOT_CEILING = 0.9697        # the artwork's own mirror IoU; not a defect budget

OUTLINE = ("fill:#ffab2c;fill-opacity:1;stroke:#331b05;stroke-width:5;"
           "stroke-linecap:round;stroke-linejoin:round;stroke-opacity:1;"
           "paint-order:stroke markers fill")
DEEP = (0x4A, 0x27, 0x08)
GAIN = 2.6                   # replaces the dilate pass of the SVG filter chain
GLOW_OPACITY = 0.88
PHOTO_BLUR = 22.0
DEFS = f'<clipPath id="canvasClip"><rect x="0" y="0" width="{CW}" height="{CH}"/></clipPath>'


def find_inkscape():
    for c in (os.environ.get("INKSCAPE"), "inkscape"):
        if not c:
            continue
        try:
            subprocess.run([c, "--version"], check=True, capture_output=True)
            return c
        except (OSError, subprocess.CalledProcessError):
            pass
    raise SystemExit("inkscape not found; set INKSCAPE=/path/to/inkscape")


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load_logo(svg_path):
    ns = {"svg": "http://www.w3.org/2000/svg"}
    p = ET.parse(svg_path).getroot().find(".//svg:path", ns)
    if p is None:
        raise SystemExit(f"no <path> found in {svg_path}")
    return p.get("d")


def read_config(path):
    """Load the config, resolving `logo_svg` relative to the config file.

    The mark ships inside the skill as `vaven-mark.svg` so a config can point at
    it relatively and keep working wherever the skill is checked out. An
    absolute path still works, for a one-off.
    """
    cfg = json.load(open(path))
    logo = cfg.get("logo_svg", "vaven-mark.svg")
    if not os.path.isabs(logo):
        logo = os.path.join(os.path.dirname(os.path.abspath(path)), logo)
    if not os.path.exists(logo):
        raise SystemExit(f"logo_svg not found: {logo}")
    cfg["logo_svg"] = logo
    return cfg


def letterbox_rows(im, thresh=24.0):
    """Rows of near-black baked into the top and bottom edges. These exports
    carry a handful and they are invisible until they land in the output."""
    rows = np.asarray(im.convert("RGB"), np.float32).mean(axis=(1, 2))
    dark = rows < thresh
    top = int(np.argmax(~dark)) if dark[0] else 0
    bot = int(np.argmax(~dark[::-1])) if dark[-1] else 0
    return top, bot


def resolve_axis(cfg):
    """The group's symmetry axis, from two INDEPENDENT estimators.

    Returns (axis, disagreement). One estimator cannot be checked; two can.
    They are the centroid of the face centres and the midpoint of the whole
    head span, and they answer slightly different questions -- where the mass
    of faces sits, and where the outline is centred. When they agree the axis
    is trustworthy. When they do not, the reading is wrong somewhere and the
    mark will sit visibly off-centre against the people.
    """
    if "faces_centroid" in cfg and "head_span_centre" in cfg:
        a, b = cfg["faces_centroid"], cfg["head_span_centre"]
        return (a + b) / 2.0, abs(a - b)
    if "axis" in cfg:
        return cfg["axis"], None
    raise SystemExit("config needs faces_centroid + head_span_centre "
                     "(preferred) or axis")


def logo_layer(path_d, height, top, style=OUTLINE, axis=AXIS):
    """Place the mark with its TRUE symmetry axis on `axis`, bbox top on `top`.

    Centring the visual bbox instead is 1.9 px off, because the wordmark is not
    mirror-symmetric and only the knot is.
    """
    k = height / L_H
    tx, ty = axis - L_AX * k, top - L_Y0 * k
    w = L_W * k
    return (f'<g transform="translate({tx:.4f},{ty:.4f}) scale({k:.6f})">'
            f'<path style="{esc(style)}" d="{esc(path_d)}"/></g>',
            (axis - w / 2, top, w, height))


def canvas(layers, title=""):
    body = "\n".join("  " + l for l in layers)
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{CW}" height="{CH}" viewBox="0 0 {CW} {CH}" version="1.1">\n'
            f'<title>{esc(title)}</title>\n<defs>{DEFS}</defs>\n'
            f'<g clip-path="url(#canvasClip)">\n{body}\n</g>\n</svg>')


def bg_layer(href):
    return (f'<image xlink:href="{href}" x="0" y="0" width="{CW}" '
            f'height="{CH}" preserveAspectRatio="none"/>')


class Frame:
    """The photo full-bleed on the canvas, centred on the group's axis.

    The scale is pinned from below: a crop wider than MAXW stops being centred
    on the group, so MAXW is the widest usable crop and SBASE the smallest
    usable scale. Zooming in further only costs logo room.
    """

    def __init__(self, photo, cfg, headroom):
        self.im = Image.open(photo).convert("RGB")
        self.SW, self.SH = self.im.size
        self.axis_frac, self.axis_disagree = resolve_axis(cfg)
        self.gx = self.axis_frac * self.SW
        self.heads = cfg["heads"] * self.SH
        self.topmost = cfg["top"] * self.SH
        self.chin = cfg["chin"] * self.SH
        self.face_l = cfg["face_l"] * self.SW
        self.face_r = cfg["face_r"] * self.SW

        # The biggest single lever on mark size, and a taste call rather than a
        # hard limit: nothing occludes the cover on desktop, and mobile fades
        # the bottom instead of covering it. Override per photo and render the
        # ladder — what it costs is visible in one look, never worth assuming.
        self.ui_top = float(cfg.get("ui_top", UI_TOP))
        self.bottom = self.ui_top - UI_CLEAR

        # Per-person chins. `chin` alone is the lowest chin ANYWHERE in the
        # photo, which over-constrains every mark that does not actually reach
        # that person: on image-EDIT (2) the lowest chin sits 274px off the
        # axis and only enters a mark's span past 27% of canvas width, yet it
        # was capping every width below that too. Give `chins` and the binding
        # chin becomes the lowest one the mark really passes under.
        ch = cfg.get("chins")
        self.chins = ([(c.get("label", f"chin{i}"), float(c["x"]), float(c["y"]))
                       for i, c in enumerate(ch)] if ch else
                      [("lowest chin", None, cfg["chin"])])

        self.maxw = 2 * min(self.gx, self.SW - self.gx)
        self.s = CW / self.maxw
        self.wpx, self.src_h = CW / self.s, CH / self.s
        self.hr = headroom
        self.x0 = self.gx - self.wpx / 2
        self.ytop = self.heads - headroom / self.s
        # Which way to move headroom is not obvious and is easy to state
        # backwards: MORE headroom lowers ytop (the crop starts higher up the
        # photo); LESS headroom raises it (the crop starts further down).
        if self.ytop < 0:
            raise SystemExit(
                f"headroom {headroom:.0f} runs off the top of the photo; "
                f"REDUCE it below {self.heads * self.s:.0f}")
        if self.ytop + self.src_h > self.SH + 1:
            raise SystemExit(
                f"photo runs past its bottom edge "
                f"({(self.ytop + self.src_h) / self.SH * 100:.1f}% of source); "
                f"INCREASE headroom by at least "
                f"{(self.ytop + self.src_h - self.SH) * self.s:.0f}, "
                f"or use a taller photo")

        # These exports arrive with a few rows of black letterbox baked into the
        # top and bottom edges. They are invisible at a glance and fatal in the
        # output, so refuse a crop that would reach into one.
        t, b = letterbox_rows(self.im)
        if self.ytop < t:
            raise SystemExit(
                f"crop starts at row {self.ytop:.0f} but the photo has {t} rows "
                f"of black letterbox at the top; REDUCE headroom below "
                f"{(self.heads - t) * self.s:.0f}")
        if self.ytop + self.src_h > self.SH - b:
            raise SystemExit(
                f"crop ends at row {self.ytop + self.src_h:.0f} but the photo has "
                f"{b} rows of black letterbox at the bottom; INCREASE headroom by "
                f"at least {(self.ytop + self.src_h - (self.SH - b)) * self.s:.0f}")

    def ox(self, sx):
        return (sx - self.x0) * self.s

    def oy(self, sy):
        return (sy - self.ytop) * self.s

    def keeps_top(self):
        return self.ytop <= self.topmost

    def base(self):
        box = (self.x0, self.ytop, self.x0 + self.wpx, self.ytop + self.src_h)
        return self.im.resize((CW, CH), Image.LANCZOS, box=box)

    def chins_under(self, width_px):
        """Every chin the mark of this width actually passes under.

        A chin with x=None always binds -- that is the single-`chin` fallback,
        which has no x and so cannot be excluded. If a width reaches nobody,
        fall back to all of them rather than to nothing: an unconstrained mark
        is never the right answer, it is a missing measurement.
        """
        half = width_px / 2 + CHIN_MARGIN
        got = [(l, self.oy(y * self.SH)) for l, x, y in self.chins
               if x is None or abs(self.ox(x * self.SW) - AXIS) <= half]
        return got or [(l, self.oy(y * self.SH)) for l, x, y in self.chins]

    def band(self, width_px=None):
        """Clear room between the binding chin and Facebook's furniture.

        With no width, uses every chin -- the conservative reading, and the one
        to quote as "the band" before a width has been chosen.
        """
        pts = self.chins_under(width_px) if width_px is not None else [
            (l, self.oy(y * self.SH)) for l, x, y in self.chins]
        label, chin = max(pts, key=lambda t: t[1])
        return chin, self.bottom - chin, label

    def gaps(self, h, top):
        """(label, gap%) for every chin under the mark. The worst one decides.

        Checking only the LOWEST chin is not enough once overlap is allowed: a
        mark that clears the lowest chin by a committed margin can still land a
        few pixels off a higher one, which is the same amateur tell.
        """
        return [(l, (top - c) / h * 100)
                for l, c in self.chins_under(h * RATIO)]

    def layout(self, width_frac):
        """Place a mark `width_frac` of canvas width. Returns (height, top).

        SIZE IS AN INPUT, NOT A DERIVED QUANTITY. An earlier version solved the
        height from "clear the chin by GAP_FRAC of the mark's own height", which
        made one fitted constant set the size, the position, and the ceiling all
        at once -- and quietly capped this photo at 24.8% of canvas width when
        29.1% fits. The gap is now an OUTCOME that gets measured and reported.

        Vertical rule: the mark's BOTTOM sits on BOTTOM, always. That line is a
        hard external fact (Facebook starts drawing over the image at UI_TOP),
        so it is the one anchor that is not a taste call, and pinning to it
        aligns every rung of a size ladder on one baseline -- the fan-out then
        varies size and nothing else. A mark taller than the band grows up
        across the chin line, which is a committed overlap and a real option.

        Centring the mark in the band was tried and dropped: it makes the gap
        (band - h)/2, which crawls through zero, so EVERY width from 24% to 35%
        of canvas landed in the grazing dead zone on this photo.
        """
        if width_frac <= 0:
            raise SystemExit(f"width fraction must be positive, got {width_frac}")
        h = width_frac * CW / RATIO
        if self.band(h * RATIO)[1] <= 0:
            raise SystemExit("no room for the mark: chin is already below "
                             f"{self.bottom:.0f}px. Reduce headroom.")
        return h, self.bottom - h


def ceiling_width(f, lo=0.05, hi=0.60, step=0.001, clear=1.5 * GRAZE_FRAC):
    """Widest mark that stays clear of EVERY chin it passes under.

    `clear` is the gap it must actually leave, and it is deliberately LARGER
    than GRAZE_FRAC. Scanning down to the gate's own 8% threshold returns the
    width whose gap is 8.0% -- passing by a rounding error, and sitting exactly
    on the boundary the "commit to a gap or an overlap, never between" rule
    exists to keep marks away from. A ceiling wants margin, not a photo finish.

    Scanned, not solved. The binding chin depends on the width (a chin off to
    the side only counts once the mark grows out to it) and the width depends
    on the binding chin, so the relationship is not monotone: it has a cliff
    where an off-axis chin enters the span and the usable band collapses.
    Iterating to a fixed point oscillates across that cliff and lands below the
    real ceiling; a descending scan finds it directly.

    Deliberately restricted to the GAP regime -- every gap positive. Overlap is
    a real option but never a silent default; ask for it with an explicit width.
    """
    n = int(round((hi - lo) / step))
    for i in range(n, -1, -1):
        wf = lo + i * step
        h = wf * CW / RATIO
        if all(g >= clear * 100 for _, g in f.gaps(h, f.bottom - h)):
            return wf
    raise SystemExit("no width clears the chins; the crop leaves no usable band")


def read_widths(cfg, photo):
    """The size ladder: mark widths as fractions of CANVAS width.

    Stated in the one unit the client actually judges in -- how much of the
    cover the mark occupies -- rather than as a scale of some internally
    derived maximum, which moves whenever the crop moves and makes two rungs
    from different photos incomparable.

    Default: the widest mark that still clears the grazing dead zone. NOT the
    width that fills the band exactly -- that lands the mark's top on the chin
    line, gap 0.0%, which fails the grazing gate every time and makes the
    zero-config path useless. The 1.5x margin on GRAZE_FRAC is a default's
    safety factor, not a design rule; pass `widths` and it never applies.
    """
    w = cfg.get("widths")
    if w is None:
        return [ceiling_width(Frame(photo, cfg, read_headrooms(cfg, photo)[0]))]
    w = [float(x) for x in (w if isinstance(w, list) else [w])]
    if not w:
        raise SystemExit("widths is empty")
    if any(x <= 0 or x > 1 for x in w):
        raise SystemExit(f"widths are fractions of canvas width in (0,1]: {w}")
    return w


def read_headrooms(cfg, photo):
    hrs = cfg.get("headroom", "auto")
    if hrs in ("auto", None):
        return [auto_headroom(photo, cfg)]
    return [float(x) for x in (hrs if isinstance(hrs, list) else [hrs])]


def auto_headroom(photo, cfg, clearance=38.0):
    """Headroom that clears the group's topmost point by `clearance` px."""
    im = Image.open(photo)
    SW, SH = im.size
    gx = resolve_axis(cfg)[0] * SW
    s = CW / (2 * min(gx, SW - gx))
    return (cfg["heads"] - cfg["top"]) * SH * s + clearance


# --- halo -------------------------------------------------------------------
def mark_alpha(path_d, height, top, out, ink):
    """Render the mark alone on transparency; return its alpha channel.

    Used for the halo AND for the symmetry check. Recovering the mark by
    diffing against the background fails on a dark glow -- the near-black
    stroke nearly matches the halo, and the check reports a bogus ~84%.
    """
    layer, _ = logo_layer(path_d, height, top)
    svg, png = f"{out}/_alpha.svg", f"{out}/_alpha.png"
    with open(svg, "w") as fh:
        fh.write(canvas([layer], "alpha"))
    subprocess.run([ink, "--export-type=png", "--export-width", str(CW),
                    "--export-filename", png, svg], check=True, capture_output=True)
    return np.asarray(Image.open(png).convert("RGBA"))[..., 3].astype(np.float32) / 255.0


def halo(alpha, sigma, gain=GAIN):
    b = Image.fromarray((alpha * 255).astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(sigma))
    return np.clip(np.asarray(b, np.float32) / 255.0 * gain, 0.0, 1.0)


def halo_thickness(alpha, h):
    """Measured reach past the artwork: band area over mark perimeter."""
    solid = alpha > 0.5
    er = (solid & np.roll(solid, 1, 0) & np.roll(solid, -1, 0)
          & np.roll(solid, 1, 1) & np.roll(solid, -1, 1))
    return int(((h > 0.15) & ~solid).sum()) / max(int((solid & ~er).sum()), 1)


# --- contrast ---------------------------------------------------------------
def srgb_lum(rgb):
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * c[..., 0] + 0.7152 * c[..., 1] + 0.0722 * c[..., 2]


def ratio(a, b):
    return (np.maximum(a, b) + 0.05) / (np.minimum(a, b) + 0.05)


def hexrgb(style, key):
    m = re.search(rf"(?:^|;)\s*{key}\s*:\s*#([0-9a-fA-F]{{6}})", style)
    return tuple(int(m.group(1)[j:j + 2], 16) for j in (0, 2, 4))


def contrast_stats(rendered, plain, style):
    """max(fill, stroke) PER PIXEL against the pixel underneath.

    A mean is the whole truth on a flat ground and a lie on a photograph.
    Antialiased edges are excluded -- they blend with the ground by definition
    and would drag every number toward 1:1. Never score stroke pixels alone
    against the ground: the stroke's job is to fence the fill off, not to
    out-contrast the ground, and that mistake floors every candidate near 49%.
    """
    diff = np.abs(rendered.astype(np.int16) - plain.astype(np.int16)).sum(axis=2)
    solid = diff > 90
    er = (solid & np.roll(solid, 1, 0) & np.roll(solid, -1, 0)
          & np.roll(solid, 1, 1) & np.roll(solid, -1, 1))
    if not er.any():
        raise SystemExit("could not recover the mark's pixels")
    lbg = srgb_lum(plain[er])
    cr = np.maximum(ratio(srgb_lum(hexrgb(style, "fill")), lbg),
                    ratio(srgb_lum(hexrgb(style, "stroke")), lbg))
    return float(cr.mean()), float(np.percentile(cr, 5)), float((cr < 3.0).mean() * 100)


def knot_iou(alpha, box):
    """Mirror symmetry of the KNOT band only, about the mark's placed axis.

    Measuring the whole mark against a 90% bar falsely flags everything: the
    wordmark is not mirror-symmetric and whole-mark IoU tops out near 79%.
    """
    x, y, w, h = (int(round(v)) for v in box)
    ax = int(round(box[0] + box[2] / 2))
    m = (alpha > 0.5)[y + int(h * WORD_FRAC):y + h].astype(np.float32)
    mir = np.roll(m[:, ::-1], -(CW - 1 - 2 * ax), axis=1)
    return float(np.minimum(m, mir).sum() / max(np.maximum(m, mir).sum(), 1))


# --- subcommands ------------------------------------------------------------
def cmd_grid(a):
    os.makedirs(a.out, exist_ok=True)
    im = Image.open(a.photo).convert("RGB")
    W, H = im.size
    print(f"source {W} x {H}   aspect {W / H:.4f}")
    g = im.copy()
    d = ImageDraw.Draw(g)
    for p in range(0, 101, 5):
        x, y = W * p / 100, H * p / 100
        heavy = p % 10 == 0
        col = "#ff2d55" if heavy else "#00d0ff"
        d.line([(x, 0), (x, H)], fill=col, width=4 if heavy else 2)
        d.line([(0, y), (W, y)], fill=col, width=4 if heavy else 2)
        if heavy:
            d.text((x + 6, 8), f"{p}", fill="#ff2d55")
            d.text((8, y + 6), f"{p}", fill="#ff2d55")
    g.save(f"{a.out}/grid.png")
    print(f"{a.out}/grid.png")

    # Overlapping thirds at 1% gridlines. Full-frame eyeball reads run ~2% out,
    # and 2% of source height is ~28 output px of logo height, so the landmarks
    # have to be read off these. The windows span the WHOLE frame rather than
    # the region a particular group happened to occupy -- a group standing
    # somewhere else must not silently end up with no zoom covering it.
    for i, (fx0, fx1) in enumerate(((0.00, 0.40), (0.30, 0.70), (0.60, 1.00)), 1):
        x0, x1 = int(W * fx0), int(W * fx1)
        y0, y1 = int(H * 0.05), int(H * 0.65)
        crop = im.crop((x0, y0, x1, y1))
        k = 1500 / crop.width
        crop = crop.resize((1500, int(crop.height * k)), Image.LANCZOS)
        d = ImageDraw.Draw(crop)
        for p in range(round(fx0 * 100), round(fx1 * 100) + 1):
            x = (W * p / 100 - x0) * k
            hv = p % 5 == 0
            d.line([(x, 0), (x, crop.height)], fill="#ff2d55" if hv else "#00d0ff",
                   width=3 if hv else 1)
            d.text((x + 3, 4), f"{p}", fill="#ff2d55" if hv else "#00d0ff")
        for q in range(5, 66):
            y = (H * q / 100 - y0) * k
            hv = q % 5 == 0
            d.line([(0, y), (crop.width, y)], fill="#ff2d55" if hv else "#00d0ff",
                   width=3 if hv else 1)
            d.text((4, y + 3), f"{q}", fill="#ff2d55" if hv else "#00d0ff")
        crop.save(f"{a.out}/zoom{i}.png")
        print(f"{a.out}/zoom{i}.png")
    print("\nRead: axis (mean of faces-centroid and head-span centre), face_l,")
    print("face_r, heads (highest crown), top (highest point the GROUP owns --")
    print("raised arms and tools, NOT heads), chin (LOWEST chin).")


def cmd_check(a):
    """The gate. Nothing gets built until this passes and you have LOOKED at
    check-landmarks.png and check-placement.png. It answers three questions in
    order: is the axis trustworthy,
    is the mark's own symmetry intact where it is about to be placed, and is
    that placement any good."""
    os.makedirs(a.out, exist_ok=True)
    ink = find_inkscape()
    cfg = read_config(a.config)
    axis_frac, disagree = resolve_axis(cfg)
    hr = read_headrooms(cfg, a.photo)[0]
    f = Frame(a.photo, cfg, hr)
    # Gate the LARGEST rung: it is the one that can reach Facebook's furniture,
    # the one that can break the mobile square, and the one whose chin gap is
    # smallest, so it is the first to graze.
    widths = read_widths(cfg, a.photo)
    h, top = f.layout(max(widths))
    path_d = load_logo(cfg["logo_svg"])
    alpha = mark_alpha(path_d, h, top, a.out, ink)
    _, box = logo_layer(path_d, h, top)
    iou = knot_iou(alpha, box)

    # --- the picture you actually judge -------------------------------------
    im = Image.open(a.photo).convert("RGB")
    W, H = im.size
    d = ImageDraw.Draw(im)
    for k in ("top", "heads", "chin"):
        y = H * cfg[k]
        d.line([(0, y), (W, y)], fill="#00ff6a", width=3)
        d.text((10, y + 5), f"{k} {cfg[k] * 100:.1f}%", fill="#00ff6a")
    for k, col in (("face_l", "#00ff6a"), ("face_r", "#00ff6a")):
        x = W * cfg[k]
        d.line([(x, 0), (x, H)], fill=col, width=3)
        d.text((x + 5, H - 26), k, fill=col)
    ax = W * axis_frac
    d.line([(ax, 0), (ax, H)], fill="#ff2d55", width=5)
    d.text((ax + 6, H - 50), f"axis {axis_frac * 100:.2f}%", fill="#ff2d55")
    if disagree is not None:
        for k, col in (("faces_centroid", "#ffd400"), ("head_span_centre", "#ffd400")):
            x = W * cfg[k]
            d.line([(x, 0), (x, H)], fill=col, width=2)
            d.text((x + 5, 30), k[:5], fill=col)
    im.save(f"{a.out}/check-landmarks.png")

    # Where the mark lands, drawn on the actual crop it will be composited on.
    prev = f.base()
    pd = ImageDraw.Draw(prev)
    pd.line([(AXIS, 0), (AXIS, CH)], fill="#ff2d55", width=3)
    # Every rung of the size ladder, so the fan-out is judged before it is
    # rendered. The largest is drawn heavy -- it is the one under the gates.
    for wf in sorted(widths, reverse=True):
        sh, stop = f.layout(wf)
        _, sbox = logo_layer(path_d, sh, stop)
        heavy = wf == max(widths)
        pd.rectangle([sbox[0], sbox[1], sbox[0] + sbox[2], sbox[1] + sbox[3]],
                     outline="#00ff6a" if heavy else "#7cffb0", width=4 if heavy else 2)
        pd.text((sbox[0] + 6, sbox[1] + 6), f"{wf * 100:.0f}% wide",
                fill="#00ff6a" if heavy else "#7cffb0")
    for label, x, y in f.chins:
        cy = f.oy(y * f.SH)
        pd.line([(0, cy), (CW, cy)], fill="#00d0ff", width=2)
        pd.text((10, cy - 18), f"{label} chin {cy:.0f}", fill="#00d0ff")
        if x is not None:
            cx = f.ox(x * f.SW)
            pd.ellipse([cx - 8, cy - 8, cx + 8, cy + 8], outline="#00d0ff", width=4)
    pd.line([(0, f.ui_top), (CW, f.ui_top)], fill="#ff2d55", width=3)
    pd.text((10, f.ui_top + 6), f"mark baseline / mobile fade ({f.ui_top:.0f})",
            fill="#ff2d55")
    sq = (CW - CH) // 2
    pd.rectangle([sq, 0, sq + CH - 1, CH - 1], outline="#ffd400", width=4)
    pd.text((sq + 10, 10), "guaranteed mobile square", fill="#ffd400")
    prev.save(f"{a.out}/check-placement.png")

    # --- the gates ----------------------------------------------------------
    worst = min(f.gaps(h, top), key=lambda t: abs(t[1]))
    gap_pct = worst[1]
    crop_faces = 2 * max(AXIS - f.ox(f.face_l), f.ox(f.face_r) - AXIS) / CH
    crop_logo = box[2] / CH
    fails = []
    if disagree is None:
        print("! axis given directly, so it was never cross-checked. Prefer "
              "faces_centroid + head_span_centre.")
    elif disagree > 0.015:
        fails.append(f"AXIS ESTIMATORS DISAGREE by {disagree * 100:.1f}% of width "
                     f"(limit 1.5). One of your reads is wrong -- re-read them.")
    if iou < KNOT_CEILING - 0.03:
        fails.append(f"KNOT SYMMETRY {iou * 100:.2f}% against the artwork's "
                     f"{KNOT_CEILING * 100:.2f}% ceiling -- the mark is off its "
                     f"own mirror axis.")
    if abs(gap_pct) < GRAZE_FRAC * 100:
        # Two-sided now that a mark may deliberately overlap: a clear gap and a
        # committed overlap are both fine, the dead zone around zero is not.
        fails.append(f"GRAZING: mark sits {gap_pct:+.1f}% of its height from the "
                     f"{worst[0]}'s chin. Commit to a gap or an overlap, never "
                     f"between.")
    if top + h > f.ui_top:
        fails.append(f"mark ends at {top + h:.0f}, past its baseline "
                     f"({f.ui_top:.0f}) and into the mobile bottom fade.")
    if max(crop_faces, crop_logo) > 1.0:
        fails.append(f"does not survive the mobile square crop "
                     f"({max(crop_faces, crop_logo):.3f} > 1.0).")

    print(f"axis {axis_frac * 100:.2f}%" + (
        f"  (estimators disagree {disagree * 100:.2f}% of width)"
        if disagree is not None else "  (not cross-checked)"))
    print(f"MAXW {f.maxw:.0f}  scale {f.s:.4f}  letterbox rows {letterbox_rows(f.im)}")
    print(f"group's topmost point sits {(f.heads - f.topmost) * f.s:.0f}px above "
          f"the crowns; headroom {hr:.0f} -> {'kept' if f.keeps_top() else 'CROPPED'}")
    print(f"knot mirror IoU {iou * 100:.2f}% (ceiling {KNOT_CEILING * 100:.2f})")
    print(f"mark {h * RATIO:.0f}px wide = {h * RATIO / CW * 100:.1f}% of canvas, "
          f"gap {gap_pct:+.1f}% of logo height off the {worst[0]}'s chin, "
          f"bottom at {top + h:.0f}")
    print(f"mobile square margin {max(crop_faces, crop_logo):.3f} (under 1.0 survives)")
    chin_y, band, who = f.band(h * RATIO)
    print(f"binding chin: {who} at {chin_y:.0f}; furniture reserves "
          f"{CH - f.ui_top:.0f}px, mark bottom limit {f.bottom:.0f} -> band "
          f"{band:.0f}px, fits a mark up to {band * RATIO / CW * 100:.1f}% "
          f"of canvas width whole")
    print("size ladder (gap is an outcome; negative = deliberate overlap; the "
          "gap shown is the WORST chin under that width):")
    for wf in sorted(widths, reverse=True):
        sh, stop = f.layout(wf)
        wl, wg = min(f.gaps(sh, stop), key=lambda t: abs(t[1]))
        print(f"  {wf * 100:5.1f}% wide  wordmark {sh * WORD_FRAC / CH * 100:4.1f}% "
              f"of canvas height  gap {wg:+6.1f}% ({wl})  bottom {stop + sh:.0f}")
    print(f"\n{a.out}/check-landmarks.png   <-- are the lines on the right features?")
    print(f"{a.out}/check-placement.png   <-- is this a good place for the mark?")
    if fails:
        print("\nGATE FAILED:")
        for m in fails:
            print(f"  - {m}")
        sys.exit(1)
    print("\nGates pass. Now LOOK at both images before running build.")


def cmd_build(a):
    os.makedirs(a.out, exist_ok=True)
    ink = find_inkscape()
    cfg = read_config(a.config)
    path_d = load_logo(cfg["logo_svg"])
    sigma = float(cfg.get("sigma", 8.0))
    treatments = cfg.get("treatments", ["glow", "pad"])
    hrs = read_headrooms(cfg, a.photo)

    # build must not be a way around the axis cross-check: check only gates the
    # first rung, and build can be run without check at all.
    _, disagree = resolve_axis(cfg)
    if disagree is not None and disagree > 0.015:
        raise SystemExit(
            f"axis estimators disagree by {disagree * 100:.1f}% of width "
            f"(limit 1.5) -- one of your reads is wrong. Re-read them, then "
            f"run check.")

    rows = []
    widths = read_widths(cfg, a.photo)
    for hr in hrs:
        f = Frame(a.photo, cfg, hr)
        base = f.base()
        A = np.asarray(base, np.float32)
        BL = np.asarray(base.filter(ImageFilter.GaussianBlur(PHOTO_BLUR)), np.float32)
        tag = "keep" if f.keeps_top() else "cut"
        for wf in widths:
            h, top = f.layout(wf)
            # Halo and its thickness are per SIZE, not per crop: the blur is a
            # fixed sigma against a mark whose perimeter changes with the rung,
            # so a smaller mark carries a proportionally fatter aura.
            alpha = mark_alpha(path_d, h, top, a.out, ink)
            hm = halo(alpha, sigma)[..., None]
            thick = halo_thickness(alpha, hm[..., 0])
            for t in treatments:
                if t == "glow":
                    out = A * (1 - hm * GLOW_OPACITY) + np.array(DEEP, np.float32) * (hm * GLOW_OPACITY)
                elif t == "pad":
                    out = A * (1 - hm) + BL * hm
                else:
                    raise SystemExit(f"unknown treatment {t!r}; use glow or pad")
                # Headroom and size scale go in the name whenever there is more
                # than one rung of that kind. Without it two rungs that both
                # keep (or both crop) the group's topmost point collide on one
                # filename, the second render overwrites the first, and the
                # first row's chin/gap/box get reported against the second's
                # pixels. Silently.
                name = f"{t}-{tag}"
                if len(hrs) > 1:
                    name += f"-hr{int(round(hr))}"
                if len(widths) > 1:
                    name += f"-w{int(round(wf * 1000)):03d}"
                href = f"bg-{name}.png"
                Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)).save(f"{a.out}/{href}")
                layer, box = logo_layer(path_d, h, top)
                for suf, ls in (("", [bg_layer(href), layer]), ("-nologo", [bg_layer(href)])):
                    with open(f"{a.out}/{name}{suf}.svg", "w") as fh:
                        fh.write(canvas(ls, name))
                rows.append((name, f, h, top, box, alpha, thick, tag))

    svgs = []
    for r in rows:
        svgs += [f"{a.out}/{r[0]}.svg", f"{a.out}/{r[0]}-nologo.svg"]
    subprocess.run([ink, "--export-type=png", "--export-width", str(CW)] + svgs,
                   check=True, capture_output=True)

    lines = ["name\twidth%canvas\twordmark%canvasH\thaloThickness\tcontrastMean\t"
             "contrastP05\tpctUnder3\tknotIoU\tchin\tgapChin%\tlogoBottom\t"
             "cropFaces\tcropLogo\ttopPoint"]
    print(f"{'variant':14s} {'w%cv':>5s} {'thick':>6s} {'cMean':>6s} {'cP05':>6s} "
          f"{'<3:1':>6s} {'knotIoU':>8s} {'crop':>5s} {'top':>5s}  flags")
    ok = True
    for name, f, h, top, box, alpha, thick, tag in rows:
        rend = np.asarray(Image.open(f"{a.out}/{name}.png").convert("RGB"))
        plain = np.asarray(Image.open(f"{a.out}/{name}-nologo.png").convert("RGB"))
        c_mean, c_p05, below3 = contrast_stats(rend, plain, OUTLINE)
        iou = knot_iou(alpha, box)
        who, gap_pct = min(f.gaps(h, top), key=lambda t: abs(t[1]))
        chin = f.band(h * RATIO)[0]
        lg_bot = top + h
        crop_faces = 2 * max(AXIS - f.ox(f.face_l), f.ox(f.face_r) - AXIS) / CH
        crop_logo = max(AXIS - box[0], box[0] + box[2] - AXIS) * 2 / CH

        flags = []
        if c_p05 < 3.0:
            flags.append(f"CONTRAST-P05({c_p05:.2f})")
        if below3 > 5.0:
            flags.append(f"{below3:.0f}%-UNDER-3:1")
        if c_mean < 5.0:
            flags.append(f"MEAN-CONTRAST<5({c_mean:.2f})")
        if iou < KNOT_CEILING - 0.03:
            flags.append(f"OFF-AXIS({iou * 100:.1f}%)")
        if lg_bot > f.ui_top:
            flags.append(f"MARK-UNDER-FB-UI({lg_bot:.0f})")
        if abs(gap_pct) < GRAZE_FRAC * 100:
            flags.append(f"GRAZING-{who.upper().replace(' ', '-')}({gap_pct:+.1f}%)")
        if max(crop_faces, crop_logo) > 1.0:
            flags.append("CROPPED-ON-MOBILE")
        ok &= not flags
        print(f"{name:14s} {box[2] / CW * 100:5.1f} {thick:6.1f} {c_mean:6.2f} "
              f"{c_p05:6.2f} {below3:5.1f}% {iou * 100:7.2f}% "
              f"{max(crop_faces, crop_logo):5.3f} {tag:>5s}  {' '.join(flags)}")
        lines.append("\t".join(str(v) for v in (
            name, f"{box[2] / CW * 100:.3f}", f"{h * WORD_FRAC / CH * 100:.3f}",
            f"{thick:.3f}", f"{c_mean:.3f}", f"{c_p05:.3f}", f"{below3:.3f}",
            f"{iou:.4f}", f"{chin:.1f}", f"{gap_pct:.2f}", f"{lg_bot:.1f}",
            f"{crop_faces:.3f}", f"{crop_logo:.3f}", tag)))

    with open(f"{a.out}/report.txt", "w") as fh:
        fh.write("\n".join(lines) + "\n")

    # Upload files: flatten the alpha Inkscape leaves behind, and pre-encode the
    # JPEG at high quality rather than letting Facebook do it from a PNG.
    for name, *_ in rows:
        im = Image.open(f"{a.out}/{name}.png")
        al = np.asarray(im.convert("RGBA"))[..., 3]
        if al.min() != 255:
            # Never hand over a cover with an alpha channel: re-encoders
            # flatten it to black. A transparent pixel here means the photo
            # did not cover the canvas, which is a real fault, not a warning.
            print(f"  ! {name}: NOT fully opaque (alpha min {al.min()}); "
                  f"no upload file written")
            ok = False
            continue
        rgb = im.convert("RGB")
        rgb.save(f"{a.out}/upload-{name}.png", optimize=True)
        rgb.save(f"{a.out}/upload-{name}.jpg", quality=95, subsampling=0,
                 progressive=True)
    print(f"\nreport: {a.out}/report.txt")
    print("upload-*.jpg / upload-*.png are flattened and ready to hand over.")
    if not ok:
        print("\nSOME VARIANTS ARE FLAGGED. Fix before showing the client.")
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd, fn, needs_cfg in (("grid", cmd_grid, False),
                               ("check", cmd_check, True),
                               ("build", cmd_build, True)):
        s = sub.add_parser(cmd)
        s.add_argument("--photo", required=True)
        s.add_argument("--out", required=True)
        if needs_cfg:
            s.add_argument("--config", required=True)
        s.set_defaults(fn=fn)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
