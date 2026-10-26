#!/usr/bin/env python3
"""Prepare LoRA style-fine-tuning data from Jonathan's Honest Living archive.

The archive is one concatenated markdown blob with no clean per-post delimiters,
so for STYLE tuning we clean the markdown and chunk into ~TARGET-word windows on
paragraph boundaries (coherent prose samples are what a style LoRA needs, not
perfect essay boundaries). Output = chat JSONL with the user turn masked
(weight 0) so loss fires only on Jonathan's prose.

Usage: prep_finetune_data.py [--target-words 1000] [--min-words 150]
Pure stdlib.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SRC = Path.home() / "Dropbox/Blogg/extract/honest_living_archive.md"
OUT_DIR = Path.home() / "Dropbox/Blogg/docs/brand/finetune"


def clean(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)                 # images
    text = re.sub(r"\[Subscribe now\]\([^)]*\)[^\n]*", "", text)      # substack CTA
    text = re.sub(r"_Category:[^_\n]*_", "", text)                    # category tags
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)              # links -> anchor
    text = re.sub(r"https?://\S+", " ", text)                        # bare urls
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)            # header markup (keep text)
    text = re.sub(r"\[\^?\d+\]:?", "", text)                         # footnote markers
    text = re.sub(r"[*_`~>]", "", text)                             # emphasis/quote chars
    # collapse 3+ blank lines to a paragraph break
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def chunk(paras: list[str], target_words: int, min_words: int) -> list[str]:
    chunks, cur, n = [], [], 0
    for p in paras:
        w = len(p.split())
        cur.append(p)
        n += w
        if n >= target_words:
            chunks.append("\n\n".join(cur))
            cur, n = [], 0
    if cur and n >= min_words:
        chunks.append("\n\n".join(cur))
    elif cur and chunks:  # fold a short tail into the previous chunk
        chunks[-1] += "\n\n" + "\n\n".join(cur)
    return [c for c in chunks if len(c.split()) >= min_words]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-words", type=int, default=1000)
    ap.add_argument("--min-words", type=int, default=150)
    ap.add_argument("--src", type=Path, default=SRC,
                    help="archive to chunk (default: canonical scoring archive)")
    args = ap.parse_args()

    raw = args.src.read_text(encoding="utf-8")
    cleaned = clean(raw)
    chunks = chunk(paragraphs(cleaned), args.target_words, args.min_words)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    train, val = [], []
    for i, c in enumerate(chunks):
        row = {"messages": [
            {"role": "user", "content": "", "weight": 0},
            {"role": "assistant", "content": c},
        ]}
        (val if i % 10 == 9 else train).append(row)

    def dump(rows: list[dict], path: Path) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    dump(train, OUT_DIR / "train.jsonl")
    dump(val, OUT_DIR / "val.jsonl")

    words = sum(len(c.split()) for c in chunks)
    print(f"chunks:        {len(chunks)}  (target {args.target_words} words each)")
    print(f"train / val:   {len(train)} / {len(val)}")
    print(f"total words:   {words:,}  (~{int(words * 1.3):,} tokens)")
    print(f"train tokens:  ~{int(sum(len(r['messages'][1]['content'].split()) for r in train) * 1.3):,}")
    print(f"wrote:         {OUT_DIR/'train.jsonl'}")
    print(f"               {OUT_DIR/'val.jsonl'}")
    print("\nfirst chunk preview (first 220 chars of assistant content):")
    print("  " + chunks[0][:220].replace("\n", " ") + " ...")


if __name__ == "__main__":
    main()
