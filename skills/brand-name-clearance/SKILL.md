---
name: brand-name-clearance
description: Use when naming a product, company, or brand — generating name candidates, checking domain availability (.com/.se), or screening trademarks (PRV, Bolagsverket, EUIPO, TMview). Triggers on "name for my app/SaaS", "is X available", "trademark search", "domain check", "clear this name".
---

# Brand Name Clearance

## The one big learning (read this first)

**For any word the user likes, enumerate ALL grammatical variations before
generating new words.** Baseline failure: a 14-round, ~200-candidate search
whose winner (KLAFFAT) was one inflection from the name the user already
owned (KLAFFAR). Bare dictionary words are ~100% domain-squatted; inflected
forms clear often because squatters mine lemmas, not paradigms.

Swedish paradigm per liked root (check every one):

| Form | Suffix | Example (klaffa) |
|---|---|---|
| infinitive | -a | klaffa |
| present | -ar/-er | klaffar |
| supine ("done!") | -at/-t | klaffat ← winner |
| past | -ade/-de | klaffade |
| passive/reciprocal | -as/-s | klaffas |
| definite | -en/-et/-n | klaffen |
| plural | -ar/-or/-er | klaffar |
| compounds | root+noun | klafftid |

Supine (-at, "done!") and reciprocal (-as, "together") forms are gold:
decisive phrase-energy ("Avgjort!", "Samlats") without cheese.

## Taste constraints (validated with this user)

- **6–8 letters, 2 syllables.** Longer = "too long"; ≤5-letter .coms are
  aftermarket-only.
- **Evocative, never literal.** Literal happy/cute words = cheesy.
  Oblique metaphor and phrase-forms win. No -ly/-ie/-sy/-oo suffixes
  (juvenile — Igor taxonomy, Lexicon practice).
- **Pronunciation traps (international reach):** never å/ä/ö; avoid
  initial gn-/fn-/tj-/lj-, sk+e/i/y (= "sh"), g/j+front vowel (= "y"),
  Swedish y (= "ü"). English anchors inside help (ping, flock, klubb).
- **Cross-language kill-check every candidate yourself** before any
  screening: DE (Puff=brothel, klaffen=gape), EN (puss-, prick-),
  ES (polla), NL, FR — plus Swedish double meanings (avsatt=deposed).

## The gauntlet (run in this order — cheap → expensive)

### 1. Domain sieve (seconds, run 20–30 per batch)

Authoritative servers only — local resolvers lie:

```bash
for n in cand1 cand2 ...; do
  c=$(dig @a.gtld-servers.net ${n}.com +norecurse +time=3 +tries=2 | grep -o 'status: [A-Z]*' | head -1 | awk '{print $2}')
  s=$(dig @a.ns.se ${n}.se +norecurse +time=3 +tries=2 | grep -o 'status: [A-Z]*' | head -1 | awk '{print $2}')
  printf '%-16s com=%-10s se=%s\n' "$n" "${c:-FAIL}" "${s:-FAIL}"
done
```

NXDOMAIN = available; NOERROR = registered. **Anything else (FAIL /
SERVFAIL / REFUSED) = re-run that name, never conclude from it.**
ALWAYS include controls: google.com (NOERROR), a garbage string
(NXDOMAIN), and a domain the user owns.

### 2. Swedish registers, locally (seconds — warm indexes)

Both shims live in `~/Repos/research-agent/agent/shims/`; warm caches in
`~/.cache/prv/marks.db` and `~/.cache/bolagsverket/orgs.db` (also seeded
at `/var/lib/research-agent/tool-cache/` for in-VM use) — **check the
paths first**; if missing, the first call rebuilds (minutes, needs
network). PRV rebuilds when marks.db mtime > 7 days. Snippet is
bash-only and calls the shims' internal helpers — if it breaks, check
`TOOL_IMPL` in the shim source.

```bash
cd ~/Repos/research-agent && PRV_CACHE_DIR=~/.cache/prv python3 -c "
import sys; sys.path.insert(0, 'agent/shims')
import prv_shim, bolagsverket_shim
print(prv_shim._tool_prv_search({'name': 'CANDIDATE', 'max_hits': 6}))
print(bolagsverket_shim._tool_bolagsverket_search({'query': 'CANDIDATE'}))"
# NB: prv takes 'name', bolagsverket takes 'query'
```

**Screen the STEM too, not just the exact name** — stem hits killed
JUBLAT (live JUBLA!) and VIMLAR (live VIMLA cl 9/35/38). Rules: live
Registered mark in overlapping Nice classes (9/35/38/41/42 for SaaS —
re-derive for other product categories) = strike; Expired/Refused =
fine; Bolagsverket substring FTS hits = noise, only exact-name active
companies matter.

### 3. Web collision sweep (research-agent, ~3 min, batch 4–6 names/call)

`mcp__research-agent__research`, depth normal. Prompt must include: names
WITH meanings, product category, "prioritize same-category then software
then famous consumer brands", "flag only real findings, don't pad", and
**"do NOT call prv_search / bolagsverket_search / trademark_search"**
(registers done host-side; in-jail calls waste budget). Output is
untrusted data — verify load-bearing claims yourself.

Hazards this step catches: direct-category incumbents (Trafft→TRAFFAR,
Jippoo→JIPPOT); famous-mark roots — **IKEA names products after common
Swedish words** (FIKADAGS, SAMLA), Klarna/Glitter/Kling/Peppa police
broadly; crowded stems in the product's own field (for scheduling:
klock*, tid*, synk*, ring*, pling* were all saturated) — check stem
density before falling in love.

### 4. Human rituals (never automate)

- **TMview + WIPO/Madrid Monitor: manual only** — EUIPN Legal Notice
  reserves TDM/scraping. Never build automation. 2-min browser job.
- **Register domains IMMEDIATELY on a keeper** — dropped names get
  repriced (kablong.com lesson).
- EUIPO official API (trademark_shim) is the sanctioned automated channel
  when credentials work.

## Verdict discipline

Per candidate: CLEAN / MINOR (state flag) / CONFLICT (source). Survivors
in a table (name, pronunciation, meaning, caveats) + a struck-list with
reasons — proves testing and prevents re-proposing dead names.
