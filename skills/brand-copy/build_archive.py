#!/usr/bin/env python3
"""Build a 'latter writing' corpus from a Substack export.

Published newsletters with post_date >= CUTOFF, HTML converted to clean
markdown-ish text (images, buttons, subscribe/share widgets stripped; link text
kept), concatenated chronologically — same one-blob shape as the original
honest_living_archive.md that prep_finetune_data.py / neural_channel.py expect.
"""

import csv
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

DUMP = Path(sys.argv[1])
OUT = Path(sys.argv[2])
CUTOFF = sys.argv[3] if len(sys.argv) > 3 else "2024-01-01"

SKIP_TAGS = {"script", "style", "figure", "img", "svg", "button", "audio", "video", "iframe"}
SKIP_CLASS_RE = re.compile(
    r"subscription-widget|button-wrapper|image-link|captioned-image|share|"
    r"embedded-post|poll-embed|digest-post-embed|footer|paywall"
)
BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "div", "ul", "ol", "hr"}

# Promotional CTA blocks (share/subscribe/booking pitches). Deliberately narrow so
# genuine prose that merely mentions sharing/coaching survives; removed blocks are
# printed for review.
CTA_RE = re.compile(
    r"share (this|it) with|share this (post|blog)|please share|"
    r"subscribe to this blog|^unsubscribe$|thank you for reading honest living|"
    r"hope you liked this post|"
    r"book a (session|call)|booking link|"
    r"slots? (open|left)|(have|has|still have) some (time )?slots|slots for philosophical|"
    r"pledge your support|become a paid subscriber",
    re.IGNORECASE,
)
REMOVED: list[str] = []
H_PREFIX = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ", "h5": "##### ", "h6": "###### "}


class PostText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.cur: list[str] = []
        self.skip_depth = 0
        self.block_tag = "p"
        self.in_blockquote = 0

    def flush(self):
        text = "".join(self.cur).strip()
        text = re.sub(r"[ \t]+", " ", text)
        if text:
            prefix = H_PREFIX.get(self.block_tag, "")
            if self.block_tag == "li":
                prefix = "- "
            if self.in_blockquote:
                prefix = "> " + prefix
            self.blocks.append(prefix + text)
        self.cur = []

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class", "")
        if tag in SKIP_TAGS or SKIP_CLASS_RE.search(cls):
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "blockquote":
            self.flush()
            self.in_blockquote += 1
        elif tag in BLOCK_TAGS:
            self.flush()
            if tag in H_PREFIX or tag == "li":
                self.block_tag = tag
            else:
                self.block_tag = "p"
        elif tag == "br":
            self.cur.append("\n")

    def handle_endtag(self, tag):
        cls_owner = tag in SKIP_TAGS
        if self.skip_depth and cls_owner:
            self.skip_depth -= 1
            return
        if self.skip_depth:
            # class-based skips close on their own div/element end
            self.skip_depth -= 1 if tag == "div" and self.skip_depth else 0
            return
        if tag == "blockquote":
            self.flush()
            self.in_blockquote = max(0, self.in_blockquote - 1)
        elif tag in BLOCK_TAGS:
            self.flush()
            self.block_tag = "p"

    def handle_data(self, data):
        if not self.skip_depth:
            self.cur.append(data)


def convert(html: str) -> str:
    p = PostText()
    p.feed(html)
    p.flush()
    blocks = [b for b in p.blocks if not b.lower().startswith("post summary:")]
    kept, removed = [], []
    for b in blocks:
        (removed if CTA_RE.search(b) else kept).append(b)
    REMOVED.extend(removed)
    out = "\n\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


rows = [
    r
    for r in csv.DictReader(open(DUMP / "posts.csv"))
    if r["is_published"] == "true" and r["type"] == "newsletter" and r["post_date"][:10] >= CUTOFF
]
rows.sort(key=lambda r: r["post_date"])

parts, skipped = [], []
for r in rows:
    slug = r["post_id"].split(".", 1)[1] if "." in r["post_id"] else r["post_id"]
    matches = list((DUMP / "posts").glob(f"{r['post_id'].split('.')[0]}.*.html"))
    if not matches:
        skipped.append(r["title"])
        continue
    body = convert(matches[0].read_text(encoding="utf-8"))
    if len(body.split()) < 50:
        skipped.append(f"{r['title']} (too short after cleaning)")
        continue
    parts.append(f"# {r['title'].strip()}\n\n{body}")

OUT.write_text("\n\n\n".join(parts) + "\n", encoding="utf-8")
total_words = sum(len(p.split()) for p in parts)
print(f"posts: {len(parts)} (skipped {len(skipped)}), words: {total_words}, cutoff: {CUTOFF}")
for s in skipped:
    print(f"  skipped: {s}")
print(f"\nCTA blocks removed: {len(REMOVED)} ({sum(len(b.split()) for b in REMOVED)} words)")
for b in REMOVED:
    print(f"  CUT: {b[:110]}")
