#!/usr/bin/env python3
"""Neural authorship channels for the brand-copy stylometric detector.

Adds two API-based neural detector channels on top of the existing local
stylometric authorship detector. The author scores drafts against his own
~60-essay corpus. Both channels are CALIBRATED against the author's own
writing: the question is not "is this AI in the abstract" but "does this
draft sit where the author's real writing sits".

Pure standard library only (urllib.request, json, math, argparse, os, sys,
re, statistics, pathlib). No requests / numpy / torch.

CHANNELS
--------
  style       StyleDistance embedding (Hugging Face). Cosine similarity of a
              draft to the author-corpus centroid, judged against the author's
              own in-band cosine distribution.
  perplexity  Fast-DetectGPT-lite (DeepInfra). Mean token log-prob (perplexity
              proxy) + a conditional-curvature discrepancy, judged against the
              author's own band.

ENVIRONMENT VARIABLES
---------------------
  HF_TOKEN            (required for style channel)   Hugging Face access token.
  HF_ENDPOINT_URL     (optional)                     Dedicated HF Inference
                                                     Endpoint URL for
                                                     StyleDistance/styledistance.
                                                     If unset, serverless
                                                     feature-extraction is used.
  DEEPINFRA_API_KEY   (required for perplexity chan) DeepInfra API key.
  FASTDETECT_MODEL    (optional)                     Completions model for the
                                                     perplexity channel. Default
                                                     meta-llama/Meta-Llama-3.1-8B-Instruct.

If a required key is missing the corresponding channel prints a SKIP line and
the tool continues; it never crashes on a missing key.

USAGE
-----
  neural_channel.py build-centroid          # one-time: embed corpus, cache
                                            # centroid + author bands for BOTH
                                            # channels' calibration
  neural_channel.py score DRAFT [--json]    # score a draft on both channels
  neural_channel.py score DRAFT --channel style|perplexity   # one channel

Files:
  Corpus : /home/jonathan/Dropbox/Blogg/extract/honest_living_archive.md
  Cache  : /home/jonathan/Dropbox/Blogg/docs/brand/.neural-cache/
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #

CORPUS_PATH = Path("/home/jonathan/Dropbox/Blogg/extract/honest_living_archive.md")
CACHE_DIR = Path("/home/jonathan/Dropbox/Blogg/docs/brand/.neural-cache")

STYLE_CENTROID_FILE = "styledistance-centroid.json"
PERPLEXITY_BAND_FILE = "perplexity-band.json"

CHUNK_WORDS = 500              # ~500-word chunks for embedding
PERPLEXITY_CALIB_CHUNKS = 15   # ~15 corpus chunks for perplexity calibration

HTTP_TIMEOUT = 30              # seconds
HF_LOAD_RETRIES = 3           # retries on 503 "model loading"

HF_SERVERLESS_URL = (
    "https://router.huggingface.co/hf-inference/models/"
    "StyleDistance/styledistance/pipeline/feature-extraction"
)
# Provider is configurable so DeepInfra can be swapped for any OpenAI-compatible
# completions endpoint that supports echo+logprobs (e.g. Fireworks:
#   FASTDETECT_COMPLETIONS_URL=https://api.fireworks.ai/inference/v1/completions
#   FASTDETECT_MODEL=accounts/fireworks/models/llama-v3p1-8b-instruct
# Together: https://api.together.xyz/v1/completions ). Auth key still read from
# DEEPINFRA_API_KEY (kept as the env name for wrapper compatibility).
DEEPINFRA_COMPLETIONS_URL = os.environ.get(
    "FASTDETECT_COMPLETIONS_URL", "https://api.deepinfra.com/v1/openai/completions"
)
DEFAULT_FASTDETECT_MODEL = "meta-llama/Meta-Llama-3.1-8B-Instruct"


# --------------------------------------------------------------------------- #
# Small errors
# --------------------------------------------------------------------------- #

class ChannelSkip(Exception):
    """Raised when a channel cannot run (missing key/endpoint). Not fatal."""


class ChannelError(Exception):
    """Raised on a network/API failure with a human-readable message."""


# --------------------------------------------------------------------------- #
# Corpus loading + markdown stripping
# --------------------------------------------------------------------------- #

_RE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")           # ![alt](url)
_RE_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")           # [text](url) -> text
_RE_REF_LINK = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")       # [text][ref] -> text
_RE_FOOTNOTE_DEF = re.compile(r"^\s*\[\^[^\]]+\]:.*$", re.MULTILINE)  # def lines
_RE_FOOTNOTE_MARK = re.compile(r"\[\^[^\]]+\]")           # [^n] markers
_RE_BARE_URL = re.compile(r"https?://\S+")
_RE_HEADER = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_RE_BLOCKQUOTE = re.compile(r"^\s{0,3}>+\s?", re.MULTILINE)
_RE_HR = re.compile(r"^\s{0,3}([-*_])\1{2,}\s*$", re.MULTILINE)
_RE_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_RE_EMPHASIS = re.compile(r"[*_`~]")                      # *_`~ emphasis chars
_RE_MULTISPACE = re.compile(r"[ \t]+")
_RE_MULTINEWLINE = re.compile(r"\n{3,}")


def strip_markdown(text: str) -> str:
    """Strip markdown noise so only prose remains.

    Removes images, converts links to their anchor text, drops bare URLs,
    footnote markers/definitions, headers, blockquote markers, horizontal
    rules, fenced code, and *_`>~ emphasis characters.
    """
    text = _RE_CODE_FENCE.sub(" ", text)
    text = _RE_IMAGE.sub(" ", text)
    text = _RE_LINK.sub(r"\1", text)
    text = _RE_REF_LINK.sub(r"\1", text)
    text = _RE_FOOTNOTE_DEF.sub("", text)
    text = _RE_FOOTNOTE_MARK.sub("", text)
    text = _RE_BARE_URL.sub(" ", text)
    text = _RE_HR.sub("", text)
    text = _RE_HEADER.sub("", text)
    text = _RE_BLOCKQUOTE.sub("", text)
    text = _RE_EMPHASIS.sub("", text)
    text = _RE_MULTISPACE.sub(" ", text)
    text = _RE_MULTINEWLINE.sub("\n\n", text)
    return text.strip()


def load_corpus_text() -> str:
    """Read and clean the author corpus. Raises FileNotFoundError if missing."""
    if not CORPUS_PATH.exists():
        raise FileNotFoundError(f"Corpus not found: {CORPUS_PATH}")
    raw = CORPUS_PATH.read_text(encoding="utf-8", errors="replace")
    return strip_markdown(raw)


def chunk_words(text: str, size: int = CHUNK_WORDS) -> list[str]:
    """Split cleaned text into ~`size`-word chunks."""
    words = text.split()
    chunks = [
        " ".join(words[i : i + size]) for i in range(0, len(words), size)
    ]
    # Drop a trailing sliver chunk if it is very short and there is more.
    if len(chunks) > 1 and len(chunks[-1].split()) < max(1, size // 10):
        chunks = chunks[:-1]
    return chunks


# --------------------------------------------------------------------------- #
# Pure-Python vector math
# --------------------------------------------------------------------------- #

def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def norm(a: list[float]) -> float:
    return math.sqrt(sum(x * x for x in a))


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Returns 0.0 if either vector is zero-length."""
    na, nb = norm(a), norm(b)
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot(a, b) / (na * nb)


def mean_vector(vectors: list[list[float]]) -> list[float]:
    """Element-wise mean of equal-length vectors."""
    if not vectors:
        raise ValueError("mean_vector: empty input")
    dim = len(vectors[0])
    acc = [0.0] * dim
    for vec in vectors:
        if len(vec) != dim:
            raise ValueError("mean_vector: inconsistent vector dimensions")
        for i, v in enumerate(vec):
            acc[i] += v
    n = len(vectors)
    return [v / n for v in acc]


def safe_std(values: list[float]) -> float:
    """Population std with 0.0 fallback for <2 values."""
    if len(values) < 2:
        return 0.0
    return statistics.pstdev(values)


# --------------------------------------------------------------------------- #
# HTTP helper
# --------------------------------------------------------------------------- #

def http_post_json(
    url: str,
    payload: dict,
    headers: dict,
    timeout: int = HTTP_TIMEOUT,
) -> tuple[int, str]:
    """POST JSON, return (status_code, body_text).

    On an HTTP error status the body is still returned (not raised) so callers
    can inspect it (e.g. HF 503 model-loading). Network-level failures raise
    ChannelError with a clear message.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.getcode(), body
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - best-effort body read
            body = "<unreadable error body>"
        return exc.code, body
    except urllib.error.URLError as exc:
        raise ChannelError(f"network error contacting {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ChannelError(f"timeout after {timeout}s contacting {url}") from exc


def _body_snippet(body: str, limit: int = 200) -> str:
    snippet = body.strip().replace("\n", " ")
    return snippet[:limit]


# --------------------------------------------------------------------------- #
# Channel 1 — StyleDistance embedding (Hugging Face)
# --------------------------------------------------------------------------- #

def hf_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _flatten_embedding(obj) -> list[float]:
    """Normalise HF feature-extraction output to a single vector.

    HF may return:
      - a flat list of floats               -> use as-is
      - a list of token vectors (2-D)       -> mean-pool over tokens
      - [ [ ...token vectors... ] ] (3-D)   -> unwrap outer batch, mean-pool
    """
    if not isinstance(obj, list) or not obj:
        raise ChannelError(f"unexpected embedding shape: {type(obj).__name__}")

    # Unwrap a batch dimension: [[ ... ]] where inner is the real payload.
    if isinstance(obj[0], list) and obj and isinstance(obj[0][0], list):
        obj = obj[0]  # now a 2-D list of token vectors

    if isinstance(obj[0], (int, float)):
        return [float(x) for x in obj]  # flat sentence embedding

    if isinstance(obj[0], list) and obj and isinstance(obj[0][0], (int, float)):
        # token-level: mean-pool
        token_vectors = [[float(x) for x in tok] for tok in obj]
        return mean_vector(token_vectors)

    raise ChannelError("unable to interpret embedding shape from HF response")


def style_embed(text: str, token: str, endpoint_url: str | None) -> list[float]:
    """Embed `text` via StyleDistance. Returns a single vector."""
    url = endpoint_url or HF_SERVERLESS_URL
    payload = {"inputs": text}
    # Dedicated endpoints benefit from wait_for_model; serverless honours it too.
    payload_with_opts = dict(payload)
    payload_with_opts["options"] = {"wait_for_model": True}

    last_err = ""
    for attempt in range(HF_LOAD_RETRIES + 1):
        status, body = http_post_json(url, payload_with_opts, hf_headers(token))
        if status == 200:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as exc:
                raise ChannelError(
                    f"HF returned non-JSON 200 body: {_body_snippet(body)}"
                ) from exc
            return _flatten_embedding(parsed)

        if status == 503:
            # Model loading — parse estimated_time and retry a couple times.
            wait = 5.0
            try:
                info = json.loads(body)
                wait = float(info.get("estimated_time", wait))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            wait = min(wait, 30.0)
            last_err = f"503 model loading (est {wait:.0f}s)"
            if attempt < HF_LOAD_RETRIES:
                time.sleep(wait)
                continue

        # Any other non-200: fail with a clear message.
        raise ChannelError(
            f"HF embed failed: HTTP {status} — {_body_snippet(body)}"
        )

    raise ChannelError(f"HF embed failed after retries: {last_err}")


def build_style_centroid(token: str, endpoint_url: str | None) -> dict:
    """Embed every corpus chunk, compute centroid + author cosine band.

    Returns the cache dict and writes it to STYLE_CENTROID_FILE.
    """
    text = load_corpus_text()
    chunks = chunk_words(text)
    if not chunks:
        raise ChannelError("corpus produced no chunks after cleaning")

    print(f"[style] embedding {len(chunks)} corpus chunks ...", file=sys.stderr)
    vectors: list[list[float]] = []
    for idx, chunk in enumerate(chunks, 1):
        vec = style_embed(chunk, token, endpoint_url)
        vectors.append(vec)
        print(f"[style]   chunk {idx}/{len(chunks)} dim={len(vec)}", file=sys.stderr)

    centroid = mean_vector(vectors)

    # Author's own in-band cosine distribution: each chunk vs centroid.
    cosines = [cosine(vec, centroid) for vec in vectors]
    band = {
        "min": min(cosines),
        "mean": statistics.fmean(cosines),
        "max": max(cosines),
        "std": safe_std(cosines),
    }

    cache = {
        "centroid": centroid,
        "dim": len(centroid),
        "count": len(vectors),
        "cosine_band": band,
        "endpoint": endpoint_url or HF_SERVERLESS_URL,
    }
    save_cache(STYLE_CENTROID_FILE, cache)
    print(
        f"[style] centroid cached: dim={len(centroid)} count={len(vectors)} "
        f"band=[{band['min']:.4f}, {band['max']:.4f}]",
        file=sys.stderr,
    )
    return cache


def score_style(draft: str) -> dict:
    """Score a draft on the style channel. Returns a result dict."""
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise ChannelSkip("HF_TOKEN not set")

    cache = load_cache(STYLE_CENTROID_FILE)
    if cache is None:
        raise ChannelError(
            "no style centroid cached — run `neural_channel.py build-centroid` first"
        )

    endpoint_url = os.environ.get("HF_ENDPOINT_URL") or None
    draft_vec = style_embed(draft, token, endpoint_url)
    centroid = cache["centroid"]
    if len(draft_vec) != len(centroid):
        raise ChannelError(
            f"embedding dim mismatch: draft={len(draft_vec)} centroid={len(centroid)} "
            "(did the endpoint/model change since build-centroid?)"
        )

    cos = cosine(draft_vec, centroid)
    band = cache["cosine_band"]
    inside = band["min"] <= cos <= band["max"]

    if inside:
        verdict = "in-band"
        interp = "cosine sits inside the author's own variation — consistent with his voice"
    elif cos > band["max"]:
        verdict = "above-band"
        interp = "cosine higher than any real chunk — suspiciously centroid-hugging (possible over-smoothing)"
    else:
        verdict = "below-band"
        interp = "cosine below the author's range — reads as stylistically off for him"

    return {
        "channel": "style",
        "metric": "cosine_to_centroid",
        "value": cos,
        "author_band": {"min": band["min"], "mean": band["mean"], "max": band["max"]},
        "inside": inside,
        "verdict": verdict,
        "interpretation": interp,
        "corpus_chunks": cache["count"],
    }


# --------------------------------------------------------------------------- #
# Channel 2 — Fast-DetectGPT-lite perplexity/curvature (DeepInfra)
# --------------------------------------------------------------------------- #

def deepinfra_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _parse_completion_logprobs(body: str) -> tuple[list[float], list[dict]]:
    """Extract (token_logprobs, top_logprobs) from a completions response.

    OpenAI-compatible completions logprobs shape:
      choices[0].logprobs.token_logprobs -> list[float|None]
      choices[0].logprobs.top_logprobs   -> list[dict[str,float]|None]
    """
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ChannelError(
            f"DeepInfra returned non-JSON body: {_body_snippet(body)}"
        ) from exc

    choices = data.get("choices")
    if not choices:
        raise ChannelError(f"DeepInfra response has no choices: {_body_snippet(body)}")

    logprobs = choices[0].get("logprobs")
    if not logprobs:
        raise ChannelError(
            "DeepInfra response has no logprobs — the model/endpoint may not "
            "support echo+logprobs on the completions endpoint"
        )

    token_logprobs = logprobs.get("token_logprobs") or []
    top_logprobs = logprobs.get("top_logprobs") or []
    return token_logprobs, top_logprobs


def perplexity_metrics(text: str, key: str, model: str) -> dict:
    """Compute (mean_logprob, curvature) for `text` via DeepInfra completions.

    Uses the completions endpoint with echo=true + max_tokens=0 so the
    per-token logprobs of the INPUT text are returned. Falls back to
    max_tokens=1 if the first request yields no usable input logprobs.
    """
    def request(max_tokens: int) -> tuple[list[float], list[dict]]:
        payload = {
            "model": model,
            "prompt": text,
            "max_tokens": max_tokens,
            "echo": True,
            "logprobs": 5,
            "temperature": 0,
        }
        status, body = http_post_json(
            DEEPINFRA_COMPLETIONS_URL, payload, deepinfra_headers(key)
        )
        if status != 200:
            raise ChannelError(
                f"DeepInfra completions failed: HTTP {status} — {_body_snippet(body)}"
            )
        return _parse_completion_logprobs(body)

    token_logprobs, top_logprobs = request(0)
    usable = [lp for lp in token_logprobs if isinstance(lp, (int, float))]
    if not usable:
        # Some backends ignore max_tokens:0; retry with max_tokens:1.
        token_logprobs, top_logprobs = request(1)
        usable = [lp for lp in token_logprobs if isinstance(lp, (int, float))]

    if not usable:
        raise ChannelError(
            "no usable input token logprobs returned (echo may be unsupported "
            "for this model on DeepInfra)"
        )

    # (a) mean token log-prob — higher (less negative) = more predictable/AI-like.
    mean_logprob = statistics.fmean(usable)

    # (b) Fast-DetectGPT-lite conditional-curvature discrepancy.
    discrepancies: list[float] = []
    for i, actual in enumerate(token_logprobs):
        if not isinstance(actual, (int, float)):
            continue
        if i >= len(top_logprobs):
            continue
        top = top_logprobs[i]
        if not isinstance(top, dict) or len(top) < 2:
            continue
        candidate_logprobs = [
            v for v in top.values() if isinstance(v, (int, float))
        ]
        if len(candidate_logprobs) < 2:
            continue
        mu = statistics.fmean(candidate_logprobs)
        sigma = safe_std(candidate_logprobs)
        if sigma == 0.0:
            continue
        discrepancies.append((actual - mu) / sigma)

    curvature = statistics.fmean(discrepancies) if discrepancies else float("nan")

    return {
        "mean_logprob": mean_logprob,
        "curvature": curvature,
        "n_tokens": len(usable),
        "n_curvature_tokens": len(discrepancies),
    }


def build_perplexity_band(key: str, model: str) -> dict:
    """Calibrate perplexity metrics over ~15 corpus chunks. Caches the band."""
    text = load_corpus_text()
    chunks = chunk_words(text)
    if not chunks:
        raise ChannelError("corpus produced no chunks after cleaning")
    sample = chunks[:PERPLEXITY_CALIB_CHUNKS]

    print(
        f"[perplexity] calibrating over {len(sample)} corpus chunks ...",
        file=sys.stderr,
    )
    mean_logprobs: list[float] = []
    curvatures: list[float] = []
    for idx, chunk in enumerate(sample, 1):
        m = perplexity_metrics(chunk, key, model)
        mean_logprobs.append(m["mean_logprob"])
        if not math.isnan(m["curvature"]):
            curvatures.append(m["curvature"])
        print(
            f"[perplexity]   chunk {idx}/{len(sample)} "
            f"mean_logprob={m['mean_logprob']:.4f} curvature={m['curvature']:.4f}",
            file=sys.stderr,
        )

    def band(values: list[float]) -> dict:
        if not values:
            return {"min": None, "mean": None, "max": None, "std": None}
        return {
            "min": min(values),
            "mean": statistics.fmean(values),
            "max": max(values),
            "std": safe_std(values),
        }

    cache = {
        "model": model,
        "count": len(sample),
        "mean_logprob_band": band(mean_logprobs),
        "curvature_band": band(curvatures),
    }
    save_cache(PERPLEXITY_BAND_FILE, cache)
    print("[perplexity] band cached.", file=sys.stderr)
    return cache


def _in_band(value: float, band: dict) -> bool:
    if band.get("min") is None or band.get("max") is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    return band["min"] <= value <= band["max"]


def score_perplexity(draft: str) -> dict:
    """Score a draft on the perplexity/curvature channel."""
    key = os.environ.get("DEEPINFRA_API_KEY")
    if not key:
        raise ChannelSkip("DEEPINFRA_API_KEY not set")

    model = os.environ.get("FASTDETECT_MODEL", DEFAULT_FASTDETECT_MODEL)

    cache = load_cache(PERPLEXITY_BAND_FILE)
    if cache is None:
        raise ChannelError(
            "no perplexity band cached — run `neural_channel.py build-centroid` first"
        )

    m = perplexity_metrics(draft, key, model)
    mlp_band = cache["mean_logprob_band"]
    curv_band = cache["curvature_band"]

    mlp_inside = _in_band(m["mean_logprob"], mlp_band)
    curv_inside = _in_band(m["curvature"], curv_band)

    # Interpretation: both metrics inside the author's band == consistent.
    if mlp_inside and curv_inside:
        verdict = "in-band"
        interp = "perplexity and curvature both sit where the author's real writing sits"
    elif not mlp_inside and not curv_inside:
        verdict = "out-of-band"
        interp = "both metrics fall outside the author's range — reads unlike his real writing"
    else:
        verdict = "mixed"
        interp = "one metric in-band, one out — inspect manually before trusting"

    # AI-leaning hint: mean_logprob above the author's max + curvature near zero.
    ai_hint = None
    if mlp_band.get("max") is not None and m["mean_logprob"] > mlp_band["max"]:
        ai_hint = (
            "mean log-prob higher (more predictable) than any author chunk — "
            "text hugs the model's high-probability mass (AI-leaning signal)"
        )

    return {
        "channel": "perplexity",
        "mean_logprob": {
            "value": m["mean_logprob"],
            "author_band": mlp_band,
            "inside": mlp_inside,
        },
        "curvature": {
            "value": m["curvature"],
            "author_band": curv_band,
            "inside": curv_inside,
        },
        "verdict": verdict,
        "interpretation": interp,
        "ai_hint": ai_hint,
        "model": model,
        "n_tokens": m["n_tokens"],
        "n_curvature_tokens": m["n_curvature_tokens"],
        "calibration_chunks": cache["count"],
    }


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #

def ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def save_cache(name: str, obj: dict) -> None:
    ensure_cache_dir()
    path = CACHE_DIR / name
    path.write_text(json.dumps(obj), encoding="utf-8")


def load_cache(name: str) -> dict | None:
    path = CACHE_DIR / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ChannelError(f"corrupt cache {path}: {exc}") from exc


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def _fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        return f"{value:.4f}"
    return str(value)


def render_style_human(res: dict) -> str:
    band = res["author_band"]
    lines = [
        "── Channel: STYLE (StyleDistance embedding) ──",
        f"  cosine to author centroid : {_fmt(res['value'])}",
        f"  author's own cosine band  : "
        f"[{_fmt(band['min'])} .. {_fmt(band['mean'])} .. {_fmt(band['max'])}] "
        f"(n={res['corpus_chunks']})",
        f"  in author's variation?    : {'YES' if res['inside'] else 'NO'}",
        f"  verdict                   : {res['verdict']}",
        f"  interpretation            : {res['interpretation']}",
    ]
    return "\n".join(lines)


def render_perplexity_human(res: dict) -> str:
    mlp = res["mean_logprob"]
    curv = res["curvature"]
    mlp_b = mlp["author_band"]
    curv_b = curv["author_band"]
    lines = [
        "── Channel: PERPLEXITY (Fast-DetectGPT-lite) ──",
        f"  model                     : {res['model']}",
        f"  mean token log-prob       : {_fmt(mlp['value'])}  "
        f"(band [{_fmt(mlp_b['min'])} .. {_fmt(mlp_b['max'])}], "
        f"inside={'YES' if mlp['inside'] else 'NO'})",
        f"  curvature discrepancy     : {_fmt(curv['value'])}  "
        f"(band [{_fmt(curv_b['min'])} .. {_fmt(curv_b['max'])}], "
        f"inside={'YES' if curv['inside'] else 'NO'})",
        f"  tokens scored             : {res['n_tokens']} "
        f"({res['n_curvature_tokens']} with curvature)",
        f"  verdict                   : {res['verdict']}",
        f"  interpretation            : {res['interpretation']}",
    ]
    if res.get("ai_hint"):
        lines.append(f"  ai-leaning hint           : {res['ai_hint']}")
    return "\n".join(lines)


def render_skip(channel: str, reason: str) -> str:
    return f"── Channel: {channel.upper()} ──\n  SKIP ({reason})"


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_build_centroid(_args: argparse.Namespace) -> int:
    ensure_cache_dir()
    any_ok = False

    # Style channel calibration.
    token = os.environ.get("HF_TOKEN")
    if not token:
        print(render_skip("style", "HF_TOKEN not set"))
    else:
        try:
            build_style_centroid(token, os.environ.get("HF_ENDPOINT_URL") or None)
            print("[style] build-centroid: OK")
            any_ok = True
        except (ChannelError, FileNotFoundError) as exc:
            print(f"[style] build-centroid FAILED: {exc}", file=sys.stderr)

    # Perplexity channel calibration.
    key = os.environ.get("DEEPINFRA_API_KEY")
    if not key:
        print(render_skip("perplexity", "DEEPINFRA_API_KEY not set"))
    else:
        model = os.environ.get("FASTDETECT_MODEL", DEFAULT_FASTDETECT_MODEL)
        try:
            build_perplexity_band(key, model)
            print("[perplexity] build-centroid: OK")
            any_ok = True
        except (ChannelError, FileNotFoundError) as exc:
            print(f"[perplexity] build-centroid FAILED: {exc}", file=sys.stderr)

    return 0 if any_ok else 1


def _read_draft(draft_arg: str) -> str:
    """Interpret DRAFT as a file path if it exists, else as literal text."""
    path = Path(draft_arg)
    if path.exists() and path.is_file():
        return strip_markdown(path.read_text(encoding="utf-8", errors="replace"))
    return draft_arg


def _run_channel(channel: str, draft: str) -> dict:
    """Run one channel, converting SKIP/errors into a result dict."""
    try:
        if channel == "style":
            res = score_style(draft)
        else:
            res = score_perplexity(draft)
        res["status"] = "ok"
        return res
    except ChannelSkip as exc:
        return {"channel": channel, "status": "skip", "reason": str(exc)}
    except (ChannelError, FileNotFoundError) as exc:
        return {"channel": channel, "status": "error", "error": str(exc)}


def cmd_score(args: argparse.Namespace) -> int:
    draft = _read_draft(args.draft)

    channels = ["style", "perplexity"]
    if args.channel:
        channels = [args.channel]

    results = {ch: _run_channel(ch, draft) for ch in channels}

    if args.json:
        print(json.dumps({"draft_source": args.draft, "channels": results}, indent=2))
        return 0

    # Human-readable report.
    print("=" * 60)
    print("NEURAL AUTHORSHIP CHANNELS — per-channel verdicts")
    print("(multi-family ensemble; each channel judged separately)")
    print("=" * 60)

    verdict_line = []
    for ch in channels:
        res = results[ch]
        print()
        if res["status"] == "skip":
            print(render_skip(ch, res["reason"]))
            verdict_line.append(f"{ch}=SKIP")
        elif res["status"] == "error":
            print(f"── Channel: {ch.upper()} ──\n  ERROR: {res['error']}")
            verdict_line.append(f"{ch}=ERROR")
        elif ch == "style":
            print(render_style_human(res))
            verdict_line.append(f"style={res['verdict']}")
        else:
            print(render_perplexity_human(res))
            verdict_line.append(f"perplexity={res['verdict']}")

    print()
    print("-" * 60)
    print("COMBINED (do NOT collapse to a single pass/fail):")
    print("  " + "  |  ".join(verdict_line))
    print("  Read each channel on its own; overfitting one is the failure mode.")
    print("-" * 60)
    return 0


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    epilog = (
        "environment variables:\n"
        "  HF_TOKEN           (required for style)      Hugging Face token\n"
        "  HF_ENDPOINT_URL    (optional)                dedicated HF endpoint URL\n"
        "  DEEPINFRA_API_KEY  (required for perplexity) DeepInfra API key\n"
        "  FASTDETECT_MODEL   (optional)                completions model\n"
        f"                                               default: {DEFAULT_FASTDETECT_MODEL}\n"
        "\n"
        "typical use:\n"
        "  neural_channel.py build-centroid           # one-time calibration\n"
        "  neural_channel.py score draft.md           # both channels\n"
        "  neural_channel.py score draft.md --json\n"
        "  neural_channel.py score draft.md --channel style\n"
        "\n"
        "A missing required key makes that channel SKIP; it never crashes.\n"
    )
    parser = argparse.ArgumentParser(
        prog="neural_channel.py",
        description="Two API-based neural authorship channels, calibrated "
        "against the author's own corpus.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser(
        "build-centroid",
        help="one-time: embed corpus, cache centroid + author bands (both channels)",
    )
    p_build.set_defaults(func=cmd_build_centroid)

    p_score = sub.add_parser("score", help="score a draft on both channels")
    p_score.add_argument("draft", help="draft text, or a path to a draft file")
    p_score.add_argument("--json", action="store_true", help="emit JSON")
    p_score.add_argument(
        "--channel",
        choices=["style", "perplexity"],
        help="score only one channel",
    )
    p_score.set_defaults(func=cmd_score)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
