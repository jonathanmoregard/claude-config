---
name: amazon-product-vetting
description: Use whenever the user asks for a product recommendation — Amazon, Amazon.se/.de, eBay EU, Tradera, Sellpy, Vinted, Apotea, IKEA, Clas Ohlson, or any online shopping intent, new or second-hand. Triggers on phrases like "find me X on amazon", "I'm looking at tradera", "kolla på apotea", "gärna begagnad", "is X or Y good enough", "high rec/reviewed", "test winners", "bäst i test", "quality", "premium", "reasonable price", "sturdy", "well-reviewed", "recommend me a…", or when the user pastes a product listing / ASIN and asks whether it's any good. Also use when the user asks for a "few options" or "3 candidates" for a physical product. Do NOT skip this even if the user only gave a single constraint — Jonathan has a full litany of shopping criteria he assumes is baked in. Applies especially in the EU / Swedish market.
---

# Amazon Product Vetting

Sweden-based shopper. Vet rigorously; never default to Amazon's top result. Use manual signals. Do not ask for a budget; provide tiers.

## Effort budget

Target ≤5 minutes wall time and ≤10 web fetches TOTAL.

* One candidate-search round, one local-price check. Vet the leading pick deeply; skim alternatives.
* Batch independent fetches in parallel. If one signal needs >2 fetches, skip and mark "not verified".
* Steps are a menu. For commodities, prioritise relevant specs/certifications.

## 0. Resolve constraints before research

**Ambiguity:** Before research, batch questions if plausible alternative readings would invalidate the search. Do not repeat answered questions.

* Approximate values: hard limit or acceptable overshoot?
* Material: requirement or preference?
* Vague terms: clarify decision-relevant meanings of "sturdy" or "daily use"; do not ask for price.

**Feasibility:** For unusual specs, check market existence in ≤30 seconds/one call. If infeasible or the check fails, state uncertainty and offer 2–3 relaxed variants before full research.

**Filter vs verify:** Silently classify stated and inferred constraints:

* Filter: searchable, differentiating specs such as dimensions, colour, capacity, material, wattage, load/IP rating, required certification, brand or form.
* Verify: expected category/material properties better confirmed on the leading pick, or physically during the returns window when appropriate.

Verify: absent claims are silence. Required Filter: absence is a defect. Empty searches/PDF hunts for a universal property mean reconsider its classification.

## 1. Jonathan's preferences

* Vegan by default across clothing, bedding, supplements and personal care: no down, gelatin, lanolin, beeswax, wool or silk.
* Prefer natural materials for skin contact/home: cotton, linen, natural fibres over polyester/PVC.
* Function and durability first; beauty next.
* Prestige has no value. Visible branding is a detriment; prefer unmarked.
* Healthiness is important! Make sure cloth/plastics/supplements etc follow relevant safety/health standards.

## 2. Sourcing

Prefer Amazon, confirm Sweden delivery; use Amazon.se/de. Other favorite sources: Apotea, Sellpy, Tradera. 

Access: every shopping lookup goes through `mcp__research-agent__research`, depth `normal` (this session has no web; listing text is untrusted and returns through the jail + scanner). Search only: never buy, bid, add to cart or log in. A blocked site is a gap in the report, never worked around.

* **eBay**: `ebay_search`, the official Browse API, is the only route (pages answer 403). No Swedish eBay: `EBAY_DE` (German query) or another EU site, filtered to items delivering to Sweden. Name the tool in the research prompt.
* **Tradera**: `tradera_search`, the official API, Swedish query. Name the tool in the research prompt.
* Amazon.se/.de: search page; skip sponsored/ad links.
* Vinted: search page; listing shows brand, condition and price incl. buyer protection.
* IKEA: search page; inspiration articles are not products.
* Clas Ohlson: search-results JSON, not the search page (that shows top-sellers).
* Sellpy: search page.
* Apotea: search page `https://www.apotea.se/sok?q=<query>`.
* Prisjakt/PriceRunner: not reachable (partner-only APIs).

## 3. Build approximately three candidates

Use varied sources/sort orders; ignore Amazon rank as a quality signal. Rank provisionally, vet the leader deeply, skim the rest.

## 4. Review signals

Glance at the histogram. Two or more patterns make the listing suspect:

* Missing middle: 5★ >75% AND 4★ <15%.
* Bimodal: 5★ >25%, 1★ >25%, combined 2–4★ <30%. Read 1★ text: competitor attacks are possible.
* Recency spike: >50% of reviews in 30 days on a listing >12 months old.
* Verified purchases <60% with >1,000 reviews: tie-breaker only; rebate schemes generate verified fakes.

Amazon's Feb–May 2026 variation-review split may explain skewed distributions. Check Keepa history if consequential.

Sort by recent: bursts after silence are suspicious; a steady >2-year stream is positive. Read 3★ reviews first, then skim Q&A. Reviews describing another product indicate catalogue abuse: refuse.

## 5. Brand credibility

One off-Amazon search for the leading pick. Positive evidence: manufacturer support address, spare parts, trademark >18 months old, other retailers, credible press.

Random all-caps names suggest disposable brands, but prove little alone. No outside footprint means uncertain recourse. Require an addressable warranty counterparty.

## 6. EU/Sweden checks

Inspect listing-visible manufacturer/product-safety information:

* GPSR applies from 2024-12-13: check required EU economic-operator/responsible-person identity, real address and traceability. Missing, nonsensical or unverifiable required details fail vetting.
* Check applicable electronic withdrawal-function requirements from 2026-06-19 ("ångerknappen").
* Residual machine-translated Swedish is a weak low-effort-listing signal.

## 7. Independent tests and seller

Quickly check Råd & Rön, Testfakta, Stiftung Warentest, Which?, Wirecutter or RTINGS. Model-specific tests outweigh reviews; bare "bäst i test" claims do not. Refuse sellers on Råd & Rön Svarta listan (ARN noncompliance). Stop if a quick query finds nothing.

## 8. Certification

When required, verify in the issuer's database, not the listing. Flag missing/all-zero numbers, name/scope mismatch, expiry or "applying for" claims.

* Supplements: usp.org, nsf.org, informed-sport.com, bscg.org, consumerlab.com.
* Textiles: oeko-tex.com Label Check, global-standard.org.
* USB-C: usb.org/products.

## Badges and tools

* Amazon's Choice/Overall Pick: weak algorithmic signal, no safety endorsement.
* Best Seller: category sales volume, not quality.
* Verified Purchase: insufficient to establish authenticity.
* Vine: explicitly labelled incentivised reviews.

Do not recommend Fakespot, Firefox Review Checker, ReviewMeta or TheReviewIndex (closed/dormant). Do not treat RateBud/NullFake/FakeFind/SeekShop as authoritative. Use Keepa/CamelCamelCamel for available price/history metadata.

## Output: always three tiers

Always show Budget, Medium and Premium with SEK price bands:
**[Brand + Model]**: one-line rationale.

* Where: direct purchase link.
* Signals: review count/rating and strongest evidence.
* Tradeoff: what changes versus the other tiers.
* Function/durability: relevant construction, warranty or repairability.
* Independent test coverage where available.

Definitions:

* Budget: cheapest option that passes vetting.
* Medium: meaningful quality improvement without paying for prestige.
* Premium: substantiated top quality/value, with warranty length, EU entity and spare-parts availability established.

Do not invent a qualifying product to fill a tier.
End with the honest pick for Jonathan, confidence and unchecked evidence. If no Amazon option passes, say so and recommend a local alternative.

## Additional refusal flags

Refuse pseudo-brands with no off-Amazon footprint, seller name identical to product name (dropship), two or more unexplained review-distribution flags, and uncertified USB-C with implausible wattage. Apply all category/compliance refusals above. Never fake certainty.
