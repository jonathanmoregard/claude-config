---
name: preferring-mature-packages
description: Use when about to implement a data structure or classic algorithm by hand — a trie, Aho-Corasick or other matcher for many fixed strings at once, a tokenizer, parser or grammar for a query language or DSL, a tree/graph traversal or recursive walk over nested JSON, a priority queue, an LRU/TTL cache, an interval tree, a bloom filter, a diff/merge, a rate limiter, retry/backoff, date or duration parsing, a topological sort — or when about to add a new dependency or choose between packages. Also when a review asks "no X libs?", says "hand-rolled", "reinventing the wheel", "low-level parsing" or "manual traversal", or asks for higher-level functions.
---

# Preferring Mature Packages

## The Rule

Before writing a data structure or algorithm by hand, find the one that
already exists: in the repo, in the standard library, then in a mature package
the project can get. Vet a package before adopting it. Hand-roll only when
nothing fits, and say why.

## Steps

0. **Already here?** `rg` the repo for an existing implementation, and check
   the dependencies the project already has — one of them may do it.
1. **Standard library / higher-level primitive.** Check the table below. Often
   a reframing removes the structure entirely (serialise, then scan text).
2. **Find a package** available through the project's own dependency
   mechanism. Find that mechanism first (`flake.nix` devShell,
   `pyproject.toml`, `Cargo.toml`, `package.json`), then confirm the package is
   in it: `nix eval --raw nixpkgs#python3Packages.<name>.version`,
   `cargo search <name>`, `npm view <name> version`. Landscape unknown:
   `mcp__research-agent__research`, depth `fast`.
3. **Vet it before adopting** (commands: `references/vetting.md`).
   - **Hard stop** — do not adopt; take the successor or next candidate and
     tell the user why: archived repo; explicit deprecation or end-of-life;
     RustSec `unmaintained` advisory; nixpkgs `meta.knownVulnerabilities` or
     `meta.broken`. Never override the gate (`NIXPKGS_ALLOW_INSECURE`,
     `permittedInsecurePackages`, `allowBroken`) to get a package in. Applies
     when the user named the package too.
   - **Warn, don't reject:** ≥12 months with no release, commit or maintainer
     reply; no nixpkgs maintainers. Small finished utilities are fine stale.
     Weigh by how deep the integration goes, whether alternatives exist, and
     how fast the surrounding ecosystem moves.
   - **Popularity lags.** Downloads and stars trail decline; never use them
     alone. Prefer many dependents and organisational backing.
   - **Takeover red flags:** a recent change of owner, maintainer or trusted
     publisher; install scripts; a very new package (soft heuristic: under 90
     days old or under 10 commits).
4. **Decide.**

   | Finding | Action |
   |---|---|
   | Exact fit, passes vetting | **Adopt** — use it directly |
   | Good foundation, partial fit | **Extend** — adopt plus a thin wrapper |
   | Several small pieces each fit a part | **Compose** — combine them |
   | Nothing fits or passes vetting; zero-dep context | **Build** — hand-roll, informed by what you found |

   Zero-dep contexts: hooks under the system `python3`, standalone scripts, a
   stated stdlib-only rule. Wire an adopted package through the project's
   mechanism in the same change — never `pip install` into the system.
5. **Report.** Name what you checked and which channels you skipped or could
   not reach (registry, research agent, web) — never "nothing found" for a
   channel you did not search. Built anyway → one line why, in a code comment
   and in your summary.

## Quick Reference (Python; same question in any language)

| About to hand-roll | Reach for |
|---|---|
| search/replace of many fixed strings | one compiled `re` alternation, longest first; thousands of patterns or a hot path: `pyahocorasick` / `ahocorasick-rs` (Rust: `aho-corasick`) |
| tokenizer + recursive-descent parser | `lark`, `pyparsing`, `parsy`; known formats: `json`, `tomllib`, `csv`, `shlex` (Rust: `nom`, `pest`, `chumsky`) |
| recursive walk over nested JSON/dicts | `json.dumps` then scan the text; `glom`, `jmespath`, `jsonpath-ng` |
| priority queue | `heapq` |
| memo / LRU / TTL cache | `functools.cache`, `functools.lru_cache`; TTL: `cachetools` |
| graph walk, topological sort | `graphlib.TopologicalSorter`; `networkx` (Rust: `petgraph`) |
| sorted set, intervals | `bisect`, `sortedcontainers`, `intervaltree`, `portion` |
| diff / sequence matching | `difflib` |
| retry / backoff | `tenacity`, `stamina` |
| rate limiter | `limits`, `pyrate-limiter`, `aiolimiter` |
| date / duration parsing | `datetime.fromisoformat`, `python-dateutil`, `isodate` |
| counting, grouping, windows | `collections.Counter`, `itertools.groupby` / `pairwise` / `batched` |

## Self-check

Before writing the class or loop: name the primitive or package you checked,
where you confirmed it is available, and what vetting it passed. Cannot name
one → do steps 0–3 first.

Red flags: `class TrieNode`, `def _tokenize` + `def _parse_or`, a `while stack:`
walk over nested dicts, a hand-written heap, a dict-plus-timestamp cache, a
retry loop with `time.sleep`, an `allowInsecure`/`allowBroken` override.

---
Step 0, the Adopt/Extend/Compose/Build table and skipped-channel reporting are
adapted from affaan-m/ECC `search-first` (MIT).
