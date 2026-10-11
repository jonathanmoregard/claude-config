---
name: preferring-mature-packages
description: Use when about to implement a data structure or classic algorithm by hand — a trie, Aho-Corasick or other matcher for many fixed strings at once, a tokenizer, parser or grammar for a query language or DSL, a tree/graph traversal or recursive walk over nested JSON, a priority queue, an LRU/TTL cache, an interval tree, a bloom filter, a diff/merge, a rate limiter, retry/backoff, date or duration parsing, a topological sort. Also when a review asks "no X libs?", says "hand-rolled", "reinventing the wheel", "low-level parsing" or "manual traversal", or asks for higher-level functions.
---

# Preferring Mature Packages

## The Rule

Before writing a data structure or algorithm by hand, find the one that
already exists: a standard-library primitive first, then a mature package the
project can get. Hand-roll only when neither fits, and say why.

## Steps

1. **Standard library / higher-level primitive.** Check the table below. Often
   a reframing removes the structure entirely (serialise, then scan text).
2. **Mature package.** Mature = maintained, widely used, and available through
   the project's own dependency mechanism. Find that mechanism first
   (`flake.nix` devShell, `pyproject.toml`, `Cargo.toml`, `package.json`), then
   confirm the package is in it:
   - Nix: `nix eval --raw nixpkgs#python3Packages.<name>.version`
   - Cargo: `cargo search <name>` · npm: `npm view <name> version`
   - Landscape unknown: `mcp__research-agent__research`, depth `fast`:
     "mature maintained <language> library for <problem>".
3. **Weigh the dependency.** Adding one is fine when it earns its place. Do not
   add one when the code must stay zero-dependency (hooks under the system
   `python3`, standalone scripts, a stated stdlib-only rule) or when the only
   candidate is unmaintained. Wire it through the project's mechanism in the
   same change — never `pip install` into the system.
4. **Hand-rolling anyway?** One line why, in a code comment and in your
   summary: which option you checked and why it lost.

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

Before writing the class or loop: name the primitive or package you checked
and where you confirmed it is available. Cannot name one → do steps 1–2 first.

Red flags: `class TrieNode`, `def _tokenize` + `def _parse_or`, a `while stack:`
walk over nested dicts, a hand-written heap, a dict-plus-timestamp cache, a
retry loop with `time.sleep`.
