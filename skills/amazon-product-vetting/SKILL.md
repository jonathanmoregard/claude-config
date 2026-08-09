---
name: amazon-product-vetting
description: Use whenever the user asks for a product recommendation — Amazon, Amazon.se, eBay EU, or any online shopping intent. Triggers on phrases like "find me X on amazon", "high rec/reviewed", "test winners", "bäst i test", "quality", "premium", "reasonable price", "sturdy", "well-reviewed", "recommend me a…", or when the user pastes a product listing / ASIN and asks whether it's any good. Also use when the user asks for a "few options" or "3 candidates" for a physical product. Do NOT skip this even if the user only gave a single constraint — Jonathan has a full litany of shopping criteria he assumes is baked in. Applies especially in the EU / Swedish market.
---

# Amazon Product Vetting

Sweden-based shopper. Give a rigorous vetting pass — not the top Amazon search result. Consumer tooling has collapsed (Fakespot dead 2025-07, ReviewMeta dormant, Firefox Review Checker dead 2025-06), so manual signal-reading is the primary method.

## Effort budget — READ THIS FIRST

**Target: ≤5 minutes wall time, ≤10 web fetches total, not per candidate.** If you're about to launch dozens of fetches, you're doing it wrong.

- One-shot search for candidates. One-shot local price check. Spot-check the top pick deeply. Skim the rest.
- Prefer **parallel fetches** in one turn over serial rounds.
- If a check would need >2 fetches for a single signal, skip it and note "not verified".
- If a category is commoditised (gel pens, sponges, USB cables), the full battery is overkill — cite category-appropriate certs (USB-IF, USP, etc.) and move on.
- Never wait for all 10 steps below to complete — they're a menu, not a checklist.

## The flow

### 0. Parse constraints — filter vs verify (30 seconds, do this FIRST)

Before opening a search box, list every constraint (stated + inferred). Sort each into two buckets:

- **Filter** — a marketed, searchable spec that meaningfully narrows the field: dimensions, colour, capacity, material class, cert-required categories, brand, form factor.
- **Verify** — a property near-universal in the material/category, so the search field can't filter on it. Confirm on the top pick instead — physically, in the returns window if needed.

If a search for a Verify property returns empty pages or forces you to trawl PDFs, you mis-classified it. Flip it back to Verify and drop the filter. **Do not spend research budget hunting a "freezer-safe" or "BPA-free" label — the absence of the marketing claim is not evidence of failure.**

**Common Verify (near-universal, don't filter):**
- **Freezer-safe** for food-storage plastic (PP, PET, PS, SAN) and glass — standard down to -20 °C for dry contents; brands rarely stamp it because it's assumed.
- **BPA-free** for post-2015 food-storage plastic — EU banned BPA in food contact 2024; assume unless the listing contradicts.
- **Dishwasher-safe** for most PP, glass, stainless — same pattern.
- **Food-safe** for anything sold as a food container.
- **Airtight** for lids with a silicone gasket.
- **Microwave-safe** for PP and most glass (not crystal, not metal-rimmed ceramic).

**KEEP as filter (don't flip to Verify) when:**
- User explicitly required a printed certification (LFGB, FDA, NSF, USP).
- Material class doesn't guarantee it (acrylic + freezer = brittle; ceramic + microwave = case-by-case; metal + microwave = no).
- Use case is at the material limit (freezing wet content in acrylic, microwaving fatty content in PP).
- Ingested / topical / skin-contact category (§1) — cert is the whole point.

Show the parse to the user ONLY if there's real ambiguity. Otherwise proceed silently.

### 1. Category branch (30 seconds)

Pick the branch. Each branch overrides different parts of the general flow.

- **Supplements / ingested / topical** → require named third-party certifier (USP Verified, NSF, Informed Sport, BSCG, ConsumerLab). Reject vague "third-party tested". Vegan default. **Skip general review-signal battery — label accuracy is the risk, not user satisfaction.**
- **Mains-powered electronics** → GPSR block is the compliance floor; find a real teardown for anything mains. Elsäkerhetsverket found 100% of dropshipping electrical goods had defects — refuse pseudo-brands for mains.
- **USB-C cables** → verify against USB-IF Integrators List at usb.org/products, not the listing description. E-marker chips lie. Braided > rubber. Buy headroom (100W+).
- **Skin-contact textiles** → verify OEKO-TEX / GOTS certificate number in issuer's DB. Vegan (no wool/silk/down unless he says so).
- **Mechanical parts (mounts, holders, hinges)** → warranty length + EU entity. **No plastic rotating joints** (his past failure mode).
- **General** → continue below.

### 2. Local retailer price-anchor (one fetch)

Query Prisjakt (prisjakt.nu) OR PriceRunner (pricerunner.se) OR the obvious specialist:

Kjell & Company (cables, small electronics), Clas Ohlson / Jula / Bauhaus (household, hardware), Elgiganten / NetOnNet (appliances, consumer electronics), IKEA (furniture, textiles), Blocket / Tradera (second-hand).

**If a local retailer stocks it within +10–20% of Amazon, recommend the local option** — known SKU, EU warranty, local reklamation. Only prefer Amazon when the delta is meaningful or the SKU isn't stocked locally.

### 3. Build a slate of ~3 candidates, rank-blind

Not 5. Not 10. Three. Pull from multiple pages / sort orders. Never let Amazon's search rank drive the score — agents have documented position bias (ACES, arXiv 2508.02630).

### 4. Signal battery — deep on the top pick, spot-check the rest

Don't run all signals on all candidates. Rank preliminarily, then vet the top pick with:

**Distribution shape** (glance at the histogram). Apply the **two-flag rule** — 2+ patterns firing = suspect:
- Missing middle: 5★ >75% AND 4★ <15%
- Bimodal love/hate: 5★ >25% AND 1★ >25% AND 2–4★ <30% (can also be competitor 1★ attack on a good product — read the 1★ text)
- Recency spike: >50% of reviews in last 30 days on a >12mo listing
- Verified-purchase <60% on >1000 reviews (tie-breaker only — refund-rebate schemes produce *verified* fakes; extremes both directions warrant a look)

**2026 caveat:** Amazon split variation reviews Feb–May 2026. Thin/lopsided H1 2026 distribution may be a policy artefact — cross-check Keepa review-count history if it matters.

**Timeline** — sort by Most Recent. Burst of dozens in 24–48h after silence = paid batch. Steady drip >2 years = positive.

**Read 3★ reviews first** (least worth faking, highest signal/second). Skim Q&A (AI spam rarely touches it).

**Content mismatch** — if reviews describe a different product, it's catalogue abuse / review hijacking. Refuse.

### 5. Brand credibility (one fetch, top pick only)

Search the brand string off Amazon. Positive: manufacturer site with support address + spare parts, trademark filing >18 months old, retail distribution elsewhere, press coverage.

**Pseudo-brand pattern**: 5–9 letter all-caps random string (SZHLUX, HORUSDY, JHKUJ). Cheapest registrable trademark = coined nonsense with no conflicting prior mark. 44% of Chinese apparel USPTO applications were nonsense marks (Harvard Law Review). Disposable by design — brand string is abandoned when reviews go bad. Warranty claims have no addressable counterparty.

Absence of off-Amazon footprint ≠ bad product; it = no recourse.

### 6. EU / SE compliance (30 seconds, listing-visible)

- **GPSR block** (mandatory since 2024-12-13): scroll to product-safety / manufacturer-info. Must list a named EU Responsible Person with real EU address + traceability. Missing/nonsense/mail-forwarding shell = **legal defect**.
- **Ångerknappen** mandatory from 2026-06-19 — EU-facing shops must offer an electronic withdrawal function.
- Residual auto-translated Swedish in titles/bullets on Amazon.se = low-effort listing tell.

### 7. Test-source and seller checks (only if easily available)

- **Råd & Rön** (radron.se), **Testfakta** (testfakta.se), **Stiftung Warentest** (test.de), **Which?**, **Wirecutter**, **RTINGS** — cite if the category has coverage. "Bäst i test" beats Amazon reviews entirely.
- **Råd & Rön Svarta listan** (radron.se/svarta-listan) — 1,061 Swedish companies that don't comply with ARN. If the seller is on it, refuse.

Skip this step if a quick search returns nothing — no coverage on Amazon-only pseudo-brands is expected.

### 8. Certification lookup (only if the category demands it)

Verify cert numbers in the **issuer's own database**, never in the listing. Fake tells: no number, all-zeros, name/scope mismatch, expired, "applying for [cert]".

- Supplements → usp.org, nsf.org, informed-sport.com, bscg.org, consumerlab.com
- Textiles → oeko-tex.com Label Check, global-standard.org (GOTS)
- Cables → usb.org/products

## Badges — honest status

- **"Amazon's Choice" / "Overall Pick"** — algorithmic, query-specific, return-rate proxy. **Agents over-weight it badly (ACES).** Down-weight to weak positive; WSJ documented it on unsafe products.
- **"Best Seller"** — sales volume within a (often narrow) category node. Not quality.
- **Verified Purchase** — necessary not sufficient. Refund-rebate schemes produce verified fakes.
- **Amazon Vine** — the only legit incentivized-review path, explicitly labelled.

## Dead tools — never recommend

- Fakespot (dead 2025-07-01), Firefox Review Checker (dead 2025-06-10), ReviewMeta (dormant), TheReviewIndex (reportedly dead).
- Do NOT recommend RateBud / NullFake / FakeFind / SeekShop as authoritative — self-scored, no independent benchmark.
- DO recommend **Keepa** / **CamelCamelCamel** for review-count and price history — durable because they track metadata, not review text.

## Jonathan-specific fit (apply on top of everything)

- **Vegan by default** — bedding, clothing, supplements, personal care. No down/gelatin/lanolin/beeswax/wool/silk unless he says so.
- **Delivery to Sweden, fast.** Amazon.se > amazon.de > amazon.com. Flag >1 week shipping.
- **Natural materials** for skin-contact / in-home: bomull, linne, naturfiber over polyester/PVC.
- **Cables**: braided, headroom.
- **Mechanical**: no plastic rotating joints. Metal only.

## Output format — three tiers, always

Even if he asked for one option, present three so he can trade off consciously.

```
## Budget — [SEK price band]
**[Brand + Model]** — [1-line why]
- Where: [amazon.se link / Kjell / etc]
- Signals: [reviews count + star, key positive]
- Tradeoff: [what you give up vs premium]

## Premium-mediocre — [SEK price band]
**[Brand + Model]** — [1-line why]
- Where, Signals, Tradeoff
- Explicitly name what makes it mediocre despite the price — this tier exists so he can AVOID it

## Premium — [SEK price band]
**[Brand + Model]** — [1-line why]
- Where, Signals, Tradeoff
- Cite test-source coverage if available
```

**Tier definitions**:
- **Budget** — cheapest that still passes the vetting bar. Commoditised brand, IKEA/Kjell/Jula own line, or a WRITECH-tier that meets the minimum.
- **Premium-mediocre** — the trap: expensive-looking, big marketing, mediocre engineering. Include so he can *avoid* landing here.
- **Premium** — actual best-in-test or best-quality-per-krona at the top. Warranty length + EU entity + spare-parts availability present.

**Always end with**:
- 1-line honest pick for his profile (often not the most expensive)
- Confidence + what you couldn't check ("no Råd & Rön coverage", "couldn't verify cert #")
- If nothing on Amazon passes, say so and point to the local retailer

## Red flags — refuse

- Two+ distribution-shape patterns firing
- Pseudo-brand with no off-Amazon footprint
- Missing/nonsense GPSR Responsible Person block (EU listings)
- Seller name = product name (dropship)
- Reviews describe different product (catalogue abuse)
- Seller on Råd & Rön Svarta listan
- Unnamed "third-party tested" for supplements
- Uncertified USB-C cable rated implausibly high

## What NOT to do

- Do not hand over the top Amazon search result.
- Do not run the full battery on all candidates — deep on top pick, spot-check rest.
- Do not launch dozens of fetches. Parallel + budgeted.
- **Do not use near-universal properties as search filters** (freezer-safe, BPA-free, dishwasher-safe, food-safe, airtight-with-gasket on standard food-storage plastic/glass). They strand you in empty result pages and cause you to *penalise* good products for lacking a marketing claim they didn't need. Flip to Verify — §0.
- **Do not treat an absent marketing claim as a negative signal** unless the material class actually fails the use case.
- Do not recommend Fakespot / ReviewMeta — dead.
- Do not treat "Overall Pick" as endorsement.
- Do not skip the three-tier format.
- Do not fake certainty — say what you didn't check.
