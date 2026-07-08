#!/usr/bin/env python3
"""Generic style-divergence analyzer: is DRAFT statistically distinguishable from CORPUS?

Method: split the corpus into chunks; measure each chunk and the draft on (a) Burrows'
Delta over the corpus's own top function words (classic authorship attribution) and
(b) surface style features (sentence rhythm, punctuation profile, word length, TTR).
"Nigh undetectable" = the draft falls inside the corpus's own chunk distribution
(Delta percentile not extreme; per-feature |z| small).

Usage: style-divergence.py CORPUS DRAFT [--chunk-words 1000] [--top-words 60]
Pure stdlib. Markdown/URL cruft is stripped before analysis.
"""
from __future__ import annotations

import argparse
import re
import statistics as st
from collections import Counter
from pathlib import Path


def clean(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)          # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)        # links -> anchor text
    text = re.sub(r"https?://\S+", " ", text)                   # bare urls
    text = re.sub(r"\[\^?\d+\]:?", " ", text)                   # footnote markers
    text = re.sub(r"^#+\s.*$", " ", text, flags=re.M)           # headers
    text = re.sub(r"[*_`>~]", "", text)                         # md emphasis/quote
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def words_of(text: str) -> list[str]:
    return re.findall(r"[a-zA-ZåäöÅÄÖ'’-]+", text.lower())


def sentences_of(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[.!?]+(?:\s|$)", text) if s.strip()]


def surface_features(text: str) -> dict[str, float]:
    ws = words_of(text)
    sents = sentences_of(text)
    lens = [len(s.split()) for s in sents] or [0]
    n_words = max(len(ws), 1)
    per1k = 1000.0 / n_words
    punct = Counter(c for c in text if c in ",;:()?!—–\"'’“”")
    window = ws[:800]
    return {
        "sent_mean": st.mean(lens),
        "sent_stdev": st.pstdev(lens),
        "word_len": st.mean([len(w) for w in ws]) if ws else 0,
        "ttr_800": len(set(window)) / max(len(window), 1),
        "comma/1k": punct[","] * per1k,
        "colon/1k": punct[":"] * per1k,
        "semicolon/1k": punct[";"] * per1k,
        "paren/1k": (punct["("] + punct[")"]) * per1k / 2,
        "question/1k": punct["?"] * per1k,
        "exclam/1k": punct["!"] * per1k,
        "emdash/1k": punct["—"] * per1k,
        "quote/1k": (punct['"'] + punct["“"] + punct["”"]) * per1k,
        "i_freq/1k": sum(1 for w in ws if w == "i") * per1k,
        "you_freq/1k": sum(1 for w in ws if w == "you") * per1k,
    }


def rel_freqs(ws: list[str], vocab: list[str]) -> dict[str, float]:
    c = Counter(ws)
    n = max(len(ws), 1)
    return {w: c[w] / n for w in vocab}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("corpus")
    ap.add_argument("draft")
    ap.add_argument("--chunk-words", type=int, default=1000)
    ap.add_argument("--top-words", type=int, default=60)
    args = ap.parse_args()

    corpus = clean(Path(args.corpus).read_text(encoding="utf-8"))
    draft = clean(Path(args.draft).read_text(encoding="utf-8"))
    cw = words_of(corpus)

    # chunks
    step = args.chunk_words
    chunk_word_lists = [cw[i:i + step] for i in range(0, len(cw) - step + 1, step)]
    # sentence-level chunking for surface features: split corpus text evenly
    n_chunks = len(chunk_word_lists)
    ctext_len = len(corpus)
    text_chunks = [corpus[int(ctext_len * i / n_chunks):int(ctext_len * (i + 1) / n_chunks)]
                   for i in range(n_chunks)]

    # --- Burrows' Delta ---
    vocab = [w for w, _ in Counter(cw).most_common(args.top_words)]
    chunk_freqs = [rel_freqs(c, vocab) for c in chunk_word_lists]
    mean = {w: st.mean(f[w] for f in chunk_freqs) for w in vocab}
    sd = {w: st.pstdev([f[w] for f in chunk_freqs]) or 1e-9 for w in vocab}

    def delta(freqs: dict[str, float]) -> float:
        return st.mean(abs((freqs[w] - mean[w]) / sd[w]) for w in vocab)

    chunk_deltas = sorted(delta(f) for f in chunk_freqs)
    draft_delta = delta(rel_freqs(words_of(draft), vocab))
    pct = sum(1 for d in chunk_deltas if d < draft_delta) / len(chunk_deltas) * 100

    print(f"== Burrows' Delta (top {args.top_words} corpus words, {n_chunks} chunks) ==")
    print(f"corpus chunks: min {chunk_deltas[0]:.3f}  median {chunk_deltas[len(chunk_deltas)//2]:.3f}  "
          f"max {chunk_deltas[-1]:.3f}")
    print(f"DRAFT delta:   {draft_delta:.3f}  -> percentile {pct:.0f}% "
          f"({'INSIDE corpus band' if draft_delta <= chunk_deltas[-1] else 'OUTSIDE corpus band'})")

    # top diverging function words
    dfr = rel_freqs(words_of(draft), vocab)
    zs = sorted(vocab, key=lambda w: -abs((dfr[w] - mean[w]) / sd[w]))
    print("largest function-word divergences (word: draft z):")
    for w in zs[:8]:
        z = (dfr[w] - mean[w]) / sd[w]
        print(f"  {w:>12}: {z:+.1f}")

    # --- surface features ---
    chunk_feats = [surface_features(t) for t in text_chunks]
    draft_feats = surface_features(draft)
    print(f"\n== surface features (draft z vs corpus-chunk distribution) ==")
    print(f"{'feature':14}{'corpus mean±sd':>20}{'draft':>10}{'z':>8}")
    for k in draft_feats:
        vals = [f[k] for f in chunk_feats]
        m, s = st.mean(vals), st.pstdev(vals) or 1e-9
        z = (draft_feats[k] - m) / s
        flag = "  <-- fix" if abs(z) > 1.5 else ""
        print(f"{k:14}{m:12.2f}±{s:6.2f}{draft_feats[k]:10.2f}{z:8.1f}{flag}")


if __name__ == "__main__":
    main()
