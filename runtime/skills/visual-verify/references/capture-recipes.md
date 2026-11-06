# Capture recipes — screenshot matrix discipline

Basis: HartreeWorks/skill--visual-review capture-recipes.md (2026-07-18) +
mickzijdel/vischeck screenshot CLI (2026-06-03). Adapted for kablong-style
axum SSR.

## Priority ladder

1. **Project-local Playwright** (if a `tests/e2e/screenshots/*.spec.ts` exists
   that captures the surface, use it — matches production render).
2. **`bin/screenshot` wrapper** (Python Playwright, matrix-aware) for ad-hoc
   captures against a running dev server.
3. **Logged-in-Chrome iframe** for authenticated surfaces WITHOUT a dev
   auth-bypass. Only if 1+2 don't work; slower and manual.
4. **Static-HTML `file://`** for pure-render checks (no data, no auth).

## Non-negotiables

- **DPR = 2 (device_scale_factor=2)** — retina resolution. Legibility of
  detail depends on it. **The standalone Playwright CLI cannot do this** —
  `playwright screenshot` supports `--viewport-size` but has no
  `--device-scale-factor` flag, so it only produces 1× images. Useless for
  the fidelity this review needs. Use the Python API.
- **Boundary widths**, not representatives: `[390, 767, 768, 1280]`. 390 =
  phone edge; 767 = last mobile row; 768 = first tablet; 1280 = desktop.
  These are where CSS breakpoints flip and bugs hide.
- **Fresh browser context per state.** `browser.new_context(...)` per shot.
  No cookie/localStorage/media-cache leakage between shots.
- **`document.fonts.ready`** before capture. Webfonts loaded, no FOUT.
- **NOT `networkidle`.** Long-polling SSE, analytics beacons, reconnecting
  sockets never settle — the wait hangs forever. Use a specific readiness
  selector like `page.wait_for_selector('main[data-ready="true"]')` or a
  known element that only appears when the page is done.
- **No `transform: scale()`** on the capture target. Scale=1 gives DPR=2
  a fair shot; scaling by CSS confuses the pixel budget.

## The Python matrix loop

Save as `capture.py`, invoke as `python3 capture.py --url … --selector …`.

```python
import asyncio, argparse, pathlib
from playwright.async_api import async_playwright

WIDTHS = [390, 767, 768, 1280]  # boundary widths, not representatives
STATES = {"populated": None, "empty": None}  # extend per surface

async def capture(url, selector, out_dir):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for width in WIDTHS:
            for state_name, state_setup in STATES.items():
                ctx = await browser.new_context(
                    viewport={"width": width, "height": 844},
                    device_scale_factor=2,
                )
                page = await ctx.new_page()
                if state_setup:
                    await state_setup(page)
                await page.goto(url)
                # Readiness — replace 'main' with a real ready-selector
                await page.wait_for_selector("main", state="visible")
                await page.evaluate("document.fonts.ready")
                out = out_dir / f"{selector.replace('.', '_')}-{width}-{state_name}.png"
                if selector:
                    el = page.locator(selector).first
                    await el.screenshot(path=str(out))
                else:
                    await page.screenshot(path=str(out), full_page=True)
                await ctx.close()
        await browser.close()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--selector", default="")
    ap.add_argument("--out", default="/tmp/visual-verify")
    args = ap.parse_args()
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(capture(args.url, args.selector, out_dir))
```

## Contact-sheet output

After capture, `bin/screenshot` builds `<out>/index.html` — a grid of thumbnails,
one row per surface × columns per (width, state). Founder-friendly single-page
review. Also useful when parent inspects subagent output.

## Per-project overrides

Everything project-specific — dev-server URL, ports to free, ready selector,
dev-auth mechanism, batch groups, known-good component paths — lives in
`<repo>/.claude/visual-verify.yml`. See SKILL.md "Project config" for shape.
The CLI + wrapper scripts read it; keep this file generic.

## Failure modes to watch

- **Exit non-zero on auth wall.** If the capture is an HTTP error or unresolved
  auth-wall redirect, `bin/screenshot` exits non-zero (while still saving the
  image). Skill sees this → prompts the parent to add the auth bypass
  (task #15). Never guess-and-screenshot a login page.
- **Batch config**: `screenshots.yml` at repo root with named `groups:`
  (`bin/screenshot --group smoke`). With nothing to capture, prints available
  groups and exits non-zero — never guesses a page list.
