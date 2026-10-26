#!/usr/bin/env python3
"""Authorship-divergence detector: is DRAFT distinguishable from CORPUS (one author)?

An ensemble of authorship-attribution features. Each is measured on ~1000-word
corpus chunks (the author's OWN chunk-to-corpus variation = the human band) and on
the draft, so "indistinguishable" means the draft lands inside that band, not z=0.

Usage:
    style-detector.py CORPUS DRAFT [--json] [--top-words 500] [--chunk-words 1000]
    style-detector.py CORPUS --self-test [--chunk-words 1000]

Pure stdlib. Markdown/URL cruft is stripped before analysis.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics as st
import sys
from collections import Counter
from pathlib import Path

# A draft feature counts as OUT of band only when it is both outside the chunk
# [min, max] range AND more than this many SDs from the chunk mean. The dual test
# keeps a single fluke chunk from inflating the band and flagging normal writing.
Z_OK = 2.0

# ~150 common English function words (articles, pronouns, prepositions,
# conjunctions, auxiliaries, particles). Hardcoded so the profile is stable.
FUNCTION_WORDS = frozenset("""
a an the this that these those such
i me my mine myself we us our ours ourselves
you your yours yourself yourselves
he him his himself she her hers herself it its itself
they them their theirs themselves one ones
who whom whose which what
am is are was were be been being
have has had having do does did doing
will would shall should can could may might must ought
of in on at by for with about against between into through during
before after above below to from up down out off over under again further
then once here there when where why how all any both each few more most other
some no nor not only own same so than too very
and or but if because as until while although though unless whether since
about above across after among around
i'm you're he's she's it's we're they're i've you've we've they've
don't doesn't didn't isn't aren't wasn't weren't won't wouldn't can't couldn't
he'd she'd i'd they'd i'll you'll we'll
just also however therefore thus hence moreover furthermore nevertheless
""".split())

# Known LLM "tell" phrases. Compared against the author's OWN corpus baseline,
# since he may legitimately use some ("navigate the ...").
AI_TELL_PATTERNS = [
    ("rather than", r"\brather than\b"),
    ("ensuring/ensures", r"\bensur(?:e|es|ed|ing)\b"),
    ("highlights", r"\bhighlight(?:s|ed|ing)?\b"),
    ("underscores", r"\bunderscore(?:s|d|ing)?\b"),
    ("it's important to note", r"\bit'?s important to note\b"),
    ("it is worth noting", r"\b(?:it is|it'?s) worth noting\b"),
    ("in today's", r"\bin today'?s\b"),
    ("at the end of the day", r"\bat the end of the day\b"),
    ("plays a * role", r"\bplay(?:s|ed|ing)? an?(?:\s+\w+){0,2}\s+role\b"),
    ("not just * but", r"\bnot just\b.{0,40}?\bbut\b"),
    ("not only * but", r"\bnot only\b.{0,40}?\bbut\b"),
    ("delve", r"\bdelv(?:e|es|ed|ing)\b"),
    ("tapestry", r"\btapestry\b"),
    ("testament to", r"\btestament to\b"),
    ("navigate the", r"\bnavigat(?:e|es|ed|ing) the\b"),
    ("in conclusion", r"\bin conclusion\b"),
    ("moreover", r"\bmoreover\b"),
    ("furthermore", r"\bfurthermore\b"),
]
AI_TELLS = [(name, re.compile(pat)) for name, pat in AI_TELL_PATTERNS]

OPENER_WORDS = frozenset({"the", "this", "it", "in", "a"})


# --------------------------------------------------------------------------- #
# Markdown cleaning + tokenization
# --------------------------------------------------------------------------- #
def clean(text: str) -> str:
    """Strip markdown so only the author's prose remains."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)           # images
    text = re.sub(r"\[\^[^\]]+\]:?", " ", text)                 # footnote markers/defs
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)        # links -> anchor text
    text = re.sub(r"https?://\S+", " ", text)                   # bare urls
    text = re.sub(r"^\s{0,3}#+\s.*$", " ", text, flags=re.M)    # atx headers
    text = re.sub(r"^\s*>+", " ", text, flags=re.M)             # blockquote markers
    text = re.sub(r"[*_`~>]", "", text)                         # emphasis / cruft
    text = text.replace("’", "'").replace("‘", "'")            # normalize apostrophes
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


WORD_RE = re.compile(r"[a-zA-ZåäöÅÄÖ']+")


def words_of(text: str) -> list[str]:
    return [w for w in WORD_RE.findall(text.lower()) if w != "'"]


def split_sentences(text: str) -> list[str]:
    """Split into sentences, keeping terminal punctuation."""
    parts = re.findall(r"[^.!?]*[.!?]+|[^.!?]+$", text)
    return [p.strip() for p in parts if p.strip()]


# --------------------------------------------------------------------------- #
# Numeric helpers (pure stdlib)
# --------------------------------------------------------------------------- #
def cosine_sparse(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def cosine_dense(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def ks_2samp(a: list[float], b: list[float]) -> float:
    """Two-sample Kolmogorov-Smirnov statistic = max gap between empirical CDFs."""
    a, b = sorted(a), sorted(b)
    na, nb = len(a), len(b)
    if na == 0 or nb == 0:
        return 0.0
    vals = sorted(set(a) | set(b))
    i = j = 0
    d = 0.0
    for v in vals:
        while i < na and a[i] <= v:
            i += 1
        while j < nb and b[j] <= v:
            j += 1
        d = max(d, abs(i / na - j / nb))
    return d


def _mtld_pass(tokens: list[str], threshold: float = 0.72) -> float:
    factors = 0.0
    types: set[str] = set()
    count = 0
    for t in tokens:
        count += 1
        types.add(t)
        if len(types) / count <= threshold:
            factors += 1.0
            types = set()
            count = 0
    if count > 0:
        ttr = len(types) / count
        factors += (1.0 - ttr) / (1.0 - threshold)
    if factors == 0.0:
        return float(len(tokens))
    return len(tokens) / factors


def mtld(tokens: list[str]) -> float:
    """Bidirectional MTLD (McCarthy), TTR factor threshold 0.72."""
    if len(tokens) < 10:
        return 0.0
    return (_mtld_pass(tokens) + _mtld_pass(list(reversed(tokens)))) / 2.0


def yules_k(tokens: list[str]) -> float:
    """Yule's K: high K = more repetition / lower diversity."""
    n = len(tokens)
    if n == 0:
        return 0.0
    freqs = Counter(tokens)
    freq_of_freq = Counter(freqs.values())
    s = sum(m * m * vm for m, vm in freq_of_freq.items())
    return 10000.0 * (s - n) / (n * n)


def char_ngrams(text: str, n: int) -> Counter:
    s = re.sub(r"\s+", " ", text.lower())
    return Counter(s[i:i + n] for i in range(len(s) - n + 1))


def rel_from_counts(counts: Counter) -> dict:
    total = sum(counts.values())
    if total == 0:
        return {}
    return {k: v / total for k, v in counts.items()}


def word_trigrams(words: list[str]) -> list[tuple[str, str, str]]:
    return list(zip(words, words[1:], words[2:]))


# --------------------------------------------------------------------------- #
# Sample = one text (chunk or draft), with derived surface forms
# --------------------------------------------------------------------------- #
class Sample:
    def __init__(self, text: str) -> None:
        self.text = text
        self.words = words_of(text)
        self.sents = split_sentences(text)
        self.sent_lens = [len(words_of(s)) for s in self.sents] or [0]


def chunk_corpus(sents: list[str], chunk_words: int) -> list[Sample]:
    """Group consecutive sentences into ~chunk_words-sized Samples."""
    chunks: list[Sample] = []
    buf: list[str] = []
    count = 0
    for s in sents:
        buf.append(s)
        count += len(words_of(s))
        if count >= chunk_words:
            chunks.append(Sample(" ".join(buf)))
            buf, count = [], 0
    if buf and (not chunks or count >= chunk_words // 2):
        chunks.append(Sample(" ".join(buf)))
    elif buf and chunks:                 # fold a tiny tail into the last chunk
        chunks[-1] = Sample(chunks[-1].text + " " + " ".join(buf))
    return chunks


# --------------------------------------------------------------------------- #
# Reference model built from the corpus (or a training subset)
# --------------------------------------------------------------------------- #
class Reference:
    def __init__(self, chunks: list[Sample], top_words: int) -> None:
        self.top_words = top_words
        all_words = [w for c in chunks for w in c.words]
        all_text = " ".join(c.text for c in chunks)
        self.total_words = max(len(all_words), 1)

        # unigram MFW model
        self.uni_vocab = [w for w, _ in Counter(all_words).most_common(top_words)]
        self.uni_weight, self.uni_corpus_vec = self._delta_model(
            self.uni_vocab, [c.words for c in chunks], all_words)

        # word-trigram MFW model
        tri_chunks = [[ "\x1f".join(t) for t in word_trigrams(c.words)] for c in chunks]
        all_tri = [t for tc in tri_chunks for t in tc]
        self.tri_vocab = [w for w, _ in Counter(all_tri).most_common(top_words)]
        self.tri_weight, self.tri_corpus_vec = self._delta_model(
            self.tri_vocab, tri_chunks, all_tri)

        # function-word rates (per word)
        fw_counts = Counter(w for w in all_words if w in FUNCTION_WORDS)
        self.fw_rate = {w: fw_counts[w] / self.total_words for w in FUNCTION_WORDS}

        # sentence-length reference sample (for KS)
        self.sent_lens = [n for c in chunks for n in c.sent_lens]

        # character n-gram centroids
        self.char3 = rel_from_counts(char_ngrams(all_text, 3))
        self.char4 = rel_from_counts(char_ngrams(all_text, 4))

        # AI-tell rates per phrase: corpus-wide mean (baseline, for display) and
        # the top of the author's own per-chunk band. A draft phrase is flagged
        # only when it beats the per-chunk max -- i.e. it lands OUTSIDE the
        # author's own variation, per the calibration principle -- so the
        # author's normal single-use spikes don't trip it.
        self.tell_baseline = {}
        self.tell_chunk_max = {}
        for name, rx in AI_TELLS:
            self.tell_baseline[name] = len(rx.findall(all_text)) / self.total_words * 1000.0
            per_chunk = [len(rx.findall(c.text)) / max(len(c.words), 1) * 1000.0
                         for c in chunks]
            self.tell_chunk_max[name] = max(per_chunk) if per_chunk else 0.0

    @staticmethod
    def _delta_model(vocab, chunk_tokens, all_tokens):
        """Return (weight[token]=1/std, corpus_weighted_vec) for cosine delta.

        Mean-centering (the classic z-score) is intentionally omitted: with a
        single-author corpus the author centroid sits at the origin, so a cosine
        against it is undefined. We keep the discriminative half of Cosine Delta
        -- inverse-SD weighting -- which up-weights reliably measured words and
        stays non-degenerate. Deviation documented per project convention.
        """
        vset = set(vocab)
        chunk_rel = []
        for toks in chunk_tokens:
            n = max(len(toks), 1)
            c = Counter(t for t in toks if t in vset)
            chunk_rel.append({w: c[w] / n for w in vocab})
        weight = {}
        for w in vocab:
            sd = st.pstdev([r[w] for r in chunk_rel]) if len(chunk_rel) > 1 else 0.0
            weight[w] = 1.0 / sd if sd > 1e-12 else 0.0
        n_all = max(len(all_tokens), 1)
        cc = Counter(t for t in all_tokens if t in vset)
        corpus_vec = [cc[w] / n_all * weight[w] for w in vocab]
        return weight, corpus_vec

    def _cos_delta(self, tokens, vocab, weight, corpus_vec) -> float:
        vset = set(vocab)
        n = max(len(tokens), 1)
        c = Counter(t for t in tokens if t in vset)
        vec = [c[w] / n * weight[w] for w in vocab]
        return 1.0 - cosine_dense(vec, corpus_vec)


# --------------------------------------------------------------------------- #
# Feature extraction: a sample -> {name: scalar value}
# --------------------------------------------------------------------------- #
PUNCT = [
    ("comma", ","), ("colon", ":"), ("semicolon", ";"),
    ("question", "?"), ("exclaim", "!"), ("emdash", "—"), ("endash", "–"),
]


def compute_features(s: Sample, ref: Reference) -> dict[str, float]:
    words = s.words
    n = max(len(words), 1)
    per1k = 1000.0 / n
    f: dict[str, float] = {}

    f["cos_delta_unigram"] = ref._cos_delta(
        words, ref.uni_vocab, ref.uni_weight, ref.uni_corpus_vec)
    tri = ["\x1f".join(t) for t in word_trigrams(words)]
    f["cos_delta_trigram"] = ref._cos_delta(
        tri, ref.tri_vocab, ref.tri_weight, ref.tri_corpus_vec)

    # function-word chi-square
    chi2 = 0.0
    for w in FUNCTION_WORDS:
        exp = ref.fw_rate.get(w, 0.0) * n
        if exp > 0.0:
            obs = sum(1 for x in words if x == w)
            chi2 += (obs - exp) ** 2 / exp
    f["funcword_chi2"] = chi2

    # sentence rhythm
    f["sent_mean"] = st.mean(s.sent_lens)
    f["sent_stdev"] = st.pstdev(s.sent_lens) if len(s.sent_lens) > 1 else 0.0
    f["sent_ks"] = ks_2samp(s.sent_lens, ref.sent_lens)

    # vocabulary richness
    f["mtld"] = mtld(words)
    f["yules_k"] = yules_k(words)

    # character n-gram distance vs corpus centroid
    f["char3_dist"] = 1.0 - cosine_sparse(rel_from_counts(char_ngrams(s.text, 3)), ref.char3)
    f["char4_dist"] = 1.0 - cosine_sparse(rel_from_counts(char_ngrams(s.text, 4)), ref.char4)

    # punctuation per 1000 words
    tc = Counter(s.text)
    for name, ch in PUNCT:
        f[f"punct_{name}/1k"] = tc[ch] * per1k
    f["punct_paren/1k"] = (tc["("] + tc[")"]) / 2.0 * per1k
    f["punct_quote/1k"] = (tc['"'] + tc["“"] + tc["”"]) * per1k

    # sentence-opener share (The/This/It/In/A)
    openers = 0
    for sent in s.sents:
        w0 = words_of(sent)
        if w0 and w0[0] in OPENER_WORDS:
            openers += 1
    f["opener_share"] = openers / max(len(s.sents), 1)

    # AI-tell total rate per 1000 words
    f["ai_tell/1k"] = sum(len(rx.findall(s.text)) for _, rx in AI_TELLS) * per1k

    return f


# feature -> which tail is "bad": 'high' (only high is OUT) or 'two' (either side)
SIDED = {
    "cos_delta_unigram": "high", "cos_delta_trigram": "high",
    "funcword_chi2": "high", "sent_ks": "high",
    "char3_dist": "high", "char4_dist": "high", "ai_tell/1k": "high",
}
LABELS = {
    "cos_delta_unigram": "Cosine Delta (unigram MFW)",
    "cos_delta_trigram": "Cosine Delta (word-trigram MFW)",
    "funcword_chi2": "Function-word chi-square",
    "sent_mean": "Sentence length: mean",
    "sent_stdev": "Sentence length: stdev (burstiness)",
    "sent_ks": "Sentence-length KS statistic",
    "mtld": "Vocabulary richness: MTLD",
    "yules_k": "Vocabulary richness: Yule's K",
    "char3_dist": "Char 3-gram profile distance",
    "char4_dist": "Char 4-gram profile distance",
    "punct_comma/1k": "Punctuation: comma /1k",
    "punct_colon/1k": "Punctuation: colon /1k",
    "punct_semicolon/1k": "Punctuation: semicolon /1k",
    "punct_question/1k": "Punctuation: question mark /1k",
    "punct_exclaim/1k": "Punctuation: exclamation /1k",
    "punct_emdash/1k": "Punctuation: em-dash /1k",
    "punct_endash/1k": "Punctuation: en-dash /1k",
    "punct_paren/1k": "Punctuation: paren-pair /1k",
    "punct_quote/1k": "Punctuation: quote /1k",
    "opener_share": "Sentence openers The/This/It/In/A",
    "ai_tell/1k": "AI-phrase tells /1k",
}


# --------------------------------------------------------------------------- #
# Band evaluation
# --------------------------------------------------------------------------- #
def build_bands(chunk_feats: list[dict[str, float]]) -> dict[str, dict]:
    bands = {}
    for name in chunk_feats[0]:
        vals = [cf[name] for cf in chunk_feats]
        m = st.mean(vals)
        sd = st.pstdev(vals) if len(vals) > 1 else 0.0
        bands[name] = {"min": min(vals), "max": max(vals),
                       "mean": m, "sd": sd, "vals": vals}
    return bands


def evaluate(name: str, value: float, band: dict) -> dict:
    m, sd = band["mean"], band["sd"]
    mn, mx = band["min"], band["max"]
    z = (value - m) / sd if sd > 1e-12 else 0.0
    pct = 100.0 * sum(1 for v in band["vals"] if v < value) / len(band["vals"])
    sided = SIDED.get(name, "two")
    if sided == "high":
        in_range = value <= mx
    else:
        in_range = mn <= value <= mx
    in_band = in_range or abs(z) <= Z_OK
    return {"value": value, "z": z, "percentile": pct, "in_band": in_band,
            "min": mn, "max": mx, "mean": m, "sd": sd}


# --------------------------------------------------------------------------- #
# Feedback strings for OUT-of-band features
# --------------------------------------------------------------------------- #
def make_feedback(name: str, ev: dict, ref: Reference, draft: Sample) -> str:
    v, mn, mx, m, sd = ev["value"], ev["min"], ev["max"], ev["mean"], ev["sd"]
    hi = v > mx
    if name == "cos_delta_unigram":
        return (f"word-frequency mix diverges (delta {v:.3f} > corpus max {mx:.3f}); "
                f"biggest offenders: {_funcword_offenders(ref, draft)}")
    if name == "cos_delta_trigram":
        return (f"word-trigram phrasing diverges (delta {v:.3f} > corpus max {mx:.3f}) "
                f"-- vary set phrases / collocations to read less templated")
    if name == "funcword_chi2":
        return (f"function-word usage diverges (chi2 {v:.0f} > corpus max {mx:.0f}); "
                f"biggest offenders: {_funcword_offenders(ref, draft)}")
    if name == "sent_mean":
        act = "shorten sentences" if hi else "lengthen sentences / add clauses"
        return f"mean sentence length {v:.1f} vs corpus {m:.1f}±{sd:.1f} -- {act}"
    if name == "sent_stdev":
        if not hi:
            return (f"sentence-length variation {v:.1f} < corpus {m:.1f}±{sd:.1f} -- "
                    f"too uniform; merge some short sentences and split others for rhythm")
        return (f"sentence-length variation {v:.1f} > corpus {m:.1f}±{sd:.1f} -- "
                f"unusually choppy/erratic; even out extreme sentence lengths")
    if name == "sent_ks":
        return (f"sentence-length distribution shape differs (KS {v:.2f} > {mx:.2f}) -- "
                f"reshape toward the author's mix of short and long sentences")
    if name == "mtld":
        if not hi:
            return (f"lexical diversity MTLD {v:.0f} < corpus min {mn:.0f} -- "
                    f"vocabulary too repetitive; vary word choice")
        return f"MTLD {v:.0f} > corpus max {mx:.0f} -- unusually varied; watch for thesaurus-y words"
    if name == "yules_k":
        if hi:
            return (f"Yule's K {v:.0f} > corpus max {mx:.0f} -- more word repetition than the "
                    f"author; diversify recurring nouns/verbs")
        return f"Yule's K {v:.0f} < corpus min {mn:.0f} -- unusually low repetition"
    if name in ("char3_dist", "char4_dist"):
        return (f"character n-gram profile diverges (dist {v:.3f} > {mx:.3f}) -- "
                f"sub-word/spelling/morphology patterns read unlike the author")
    if name.startswith("punct_"):
        label = LABELS[name].split(": ", 1)[1]
        act = "use fewer" if hi else "use more"
        return f"{label} = {v:.1f} vs corpus {m:.1f}±{sd:.1f} -- {act}"
    if name == "opener_share":
        act = "vary sentence openings" if hi else "openings are unusually varied"
        return (f"{v:.0%} of sentences open with The/This/It/In/A vs corpus {m:.0%} "
                f"(max {mx:.0%}) -- {act}")
    if name == "ai_tell/1k":
        over = _tells_over_baseline(ref, draft)
        return (f"AI-tell phrases at {v:.1f}/1k exceed corpus {m:.1f}/1k; "
                f"over baseline: {over}")
    return f"{LABELS.get(name, name)} out of band ({v:.3f}; corpus {mn:.3f}..{mx:.3f})"


def _funcword_offenders(ref: Reference, draft: Sample) -> str:
    n = max(len(draft.words), 1)
    scored = []
    for w in FUNCTION_WORDS:
        exp = ref.fw_rate.get(w, 0.0) * n
        if exp > 0.5:
            obs = sum(1 for x in draft.words if x == w)
            contrib = (obs - exp) ** 2 / exp
            direction = "over" if obs > exp else "under"
            scored.append((contrib, w, direction))
    scored.sort(reverse=True)
    return ", ".join(f"{w}({d})" for _, w, d in scored[:5]) or "none"


def _tells_over_baseline(ref: Reference, draft: Sample) -> str:
    n = max(len(draft.words), 1)
    out = []
    for name, rx in AI_TELLS:
        rate = len(rx.findall(draft.text)) / n * 1000.0
        ceiling = ref.tell_chunk_max.get(name, 0.0)
        base = ref.tell_baseline.get(name, 0.0)
        if rate > ceiling and rate > 0.0:
            out.append(f"{name} ({rate:.1f}/1k vs author max {ceiling:.1f}, mean {base:.1f})")
    return ", ".join(out) or "none"


# --------------------------------------------------------------------------- #
# Scoring a draft against a reference + bands
# --------------------------------------------------------------------------- #
def score(draft: Sample, ref: Reference, bands: dict) -> dict:
    results = {}
    dfeat = compute_features(draft, ref)
    # AI-tell verdict also fails if any phrase exceeds baseline
    tells_over = _tells_over_baseline(ref, draft) != "none"
    for name, value in dfeat.items():
        ev = evaluate(name, value, bands[name])
        if name == "ai_tell/1k" and tells_over:
            ev["in_band"] = False
        ev["feedback"] = None if ev["in_band"] else make_feedback(name, ev, ref, draft)
        results[name] = ev
    return results


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #
def render_report(results: dict, ref: Reference, n_chunks: int, draft: Sample) -> str:
    lines = []
    order = [
        "cos_delta_unigram", "cos_delta_trigram", "funcword_chi2",
        "sent_mean", "sent_stdev", "sent_ks", "mtld", "yules_k",
        "char3_dist", "char4_dist",
        "punct_comma/1k", "punct_colon/1k", "punct_semicolon/1k",
        "punct_question/1k", "punct_exclaim/1k", "punct_emdash/1k",
        "punct_endash/1k", "punct_paren/1k", "punct_quote/1k",
        "opener_share", "ai_tell/1k",
    ]
    lines.append("=" * 78)
    lines.append(f"AUTHORSHIP-DIVERGENCE REPORT  ({n_chunks} corpus chunks, "
                 f"{len(draft.words)} draft words, top-{ref.top_words} MFW)")
    lines.append("=" * 78)
    lines.append(f"{'feature':38}{'draft':>9}{'corpus band':>20}{'z':>7}  verdict")
    lines.append("-" * 78)
    for name in order:
        ev = results[name]
        label = LABELS.get(name, name)
        band = f"{ev['min']:.2f}..{ev['max']:.2f}"
        verdict = "IN-BAND" if ev["in_band"] else "** OUT **"
        lines.append(f"{label:38}{ev['value']:9.2f}{band:>20}{ev['z']:7.1f}  {verdict}")
        if ev["feedback"]:
            lines.append(f"    FEEDBACK: {ev['feedback']}")
    lines.append("-" * 78)

    in_band = sum(1 for ev in results.values() if ev["in_band"])
    total = len(results)
    frac = in_band / total
    lines.append("")
    lines.append(f"DISTINGUISHABILITY VERDICT: {in_band}/{total} features inside author band "
                 f"({frac:.0%})")
    if frac >= 0.9:
        lines.append("  -> Draft is largely INDISTINGUISHABLE from the author's own variation.")
    elif frac >= 0.75:
        lines.append("  -> Draft is MOSTLY in-band; a few tells stand out (see feedback).")
    else:
        lines.append("  -> Draft is DISTINGUISHABLE from the author; multiple features diverge.")

    fixes = [ev["feedback"] for name in order
             for ev in [results[name]] if ev["feedback"]]
    lines.append("")
    lines.append(f"CONSOLIDATED FEEDBACK ({len(fixes)} fixes for a reviser):")
    if fixes:
        for i, fb in enumerate(fixes, 1):
            lines.append(f"  {i}. {fb}")
    else:
        lines.append("  (none -- every feature is inside the author's band)")
    return "\n".join(lines)


def results_to_json(results: dict, order: list[str]) -> dict:
    feats = {}
    for name in order:
        ev = results[name]
        feats[name] = {
            "value": round(ev["value"], 5),
            "band": {"min": round(ev["min"], 5), "max": round(ev["max"], 5),
                     "mean": round(ev["mean"], 5), "sd": round(ev["sd"], 5)},
            "z": round(ev["z"], 3),
            "percentile": round(ev["percentile"], 1),
            "in_band": ev["in_band"],
            "feedback": ev["feedback"],
        }
    in_band = sum(1 for ev in results.values() if ev["in_band"])
    fixes = [results[n]["feedback"] for n in order if results[n]["feedback"]]
    return {"features": feats,
            "in_band_fraction": round(in_band / len(results), 4),
            "in_band_count": in_band, "feature_count": len(results),
            "feedback": fixes}


JSON_ORDER = [
    "cos_delta_unigram", "cos_delta_trigram", "funcword_chi2",
    "sent_mean", "sent_stdev", "sent_ks", "mtld", "yules_k",
    "char3_dist", "char4_dist",
    "punct_comma/1k", "punct_colon/1k", "punct_semicolon/1k",
    "punct_question/1k", "punct_exclaim/1k", "punct_emdash/1k",
    "punct_endash/1k", "punct_paren/1k", "punct_quote/1k",
    "opener_share", "ai_tell/1k",
]


# --------------------------------------------------------------------------- #
# Self-test: hold out a slice of the corpus, score it against the rest
# --------------------------------------------------------------------------- #
def run_self_test(chunks: list[Sample], top_words: int) -> None:
    n = len(chunks)
    if n < 6:
        print("Self-test needs at least 6 corpus chunks; corpus too small.")
        return
    # hold out every 5th chunk (~20%), spread across the corpus
    held_idx = list(range(0, n, 5))
    train = [c for i, c in enumerate(chunks) if i not in set(held_idx)]
    held = [chunks[i] for i in held_idx]

    ref = Reference(train, top_words)
    bands = build_bands([compute_features(c, ref) for c in train])

    print("=" * 78)
    print(f"SELF-TEST: {len(held)} held-out author chunks vs {len(train)}-chunk band")
    print("(the corpus scored against itself -- should be MOSTLY in-band)")
    print("=" * 78)
    total_in = total = 0
    per_feature_out: Counter = Counter()
    for k, hc in enumerate(held, 1):
        res = score(hc, ref, bands)
        ib = sum(1 for ev in res.values() if ev["in_band"])
        tot = len(res)
        total_in += ib
        total += tot
        for name, ev in res.items():
            if not ev["in_band"]:
                per_feature_out[name] += 1
        print(f"  held-out chunk {k:2}: {ib}/{tot} in-band ({ib/tot:.0%})")
    frac = total_in / total
    print("-" * 78)
    print(f"OVERALL: {total_in}/{total} feature-checks in-band ({frac:.1%})")
    if per_feature_out:
        worst = ", ".join(f"{name}×{c}" for name, c in per_feature_out.most_common(6))
        print(f"Most-flagged features (expected some tail noise): {worst}")
    verdict = "PASS" if frac >= 0.85 else "WEAK" if frac >= 0.75 else "FAIL"
    print(f"CALIBRATION: {verdict} "
          f"({'author not flagged as alien' if frac >= 0.85 else 'bands may be too tight'})")


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("corpus")
    ap.add_argument("draft", nargs="?", help="draft file (omit with --self-test)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    ap.add_argument("--self-test", action="store_true",
                    help="score a held-out corpus slice against the rest")
    ap.add_argument("--top-words", type=int, default=500, help="MFW vocabulary size")
    ap.add_argument("--chunk-words", type=int, default=1000, help="corpus chunk size")
    args = ap.parse_args()

    corpus_text = clean(Path(args.corpus).read_text(encoding="utf-8"))
    chunks = chunk_corpus(split_sentences(corpus_text), args.chunk_words)
    if len(chunks) < 4:
        sys.exit("Corpus too small: fewer than 4 chunks after cleaning.")

    if args.self_test:
        run_self_test(chunks, args.top_words)
        return

    if not args.draft:
        ap.error("DRAFT is required unless --self-test is given")

    draft = Sample(clean(Path(args.draft).read_text(encoding="utf-8")))
    ref = Reference(chunks, args.top_words)
    bands = build_bands([compute_features(c, ref) for c in chunks])
    results = score(draft, ref, bands)

    if args.json:
        print(json.dumps(results_to_json(results, JSON_ORDER), indent=2))
    else:
        print(render_report(results, ref, len(chunks), draft))


if __name__ == "__main__":
    main()
