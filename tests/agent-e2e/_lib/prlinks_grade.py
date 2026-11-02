#!/usr/bin/env python3
"""Grade a "what can I merge?" reply: every open PR it hands back carries its URL.

Usage: prlinks_grade.py FINAL_TXT
Exit 0 PASS, 1 FAIL, 2 INCONCLUSIVE. The fixture (pr-links-after-history and
pr-links-no-history) has four OPEN PRs, widget#37 and #41 plus infra#393 and #394,
and widget#38 MERGED. Merged #38 is not part of any merge ask, so a bare `#38` is
never graded.

What counts as handing a PR back is deliberately broad: its number in any form
(`#37`, `PR 37`, a bare `393` in "393/394"), or its topic name. The lapse being
measured is "the user has to hunt for the link", and that happens whatever
words the PR was named in. It also includes NOT naming the PRs at all, as in
"all four are green, links above": a reply that says merge and links nothing
is a FAIL, not a pass on a technicality.

A URL counts only under its own repo: `widget/pull/393` is not infra#393's link.
"""
import re
import sys

OPEN = {
    37: ("acme/widget", r"backoff"),
    41: ("acme/widget", r"log[\s_-]*sampling"),
    393: ("acme/infra", r"alert[\s_-]*threshold"),
    394: ("acme/infra", r"dashboard[\s_-]*id"),
}

text = open(sys.argv[1], encoding="utf-8").read()

linked = {n for n, (nwo, _) in OPEN.items()
          if re.search(rf"https://github\.com/{re.escape(nwo)}/pull/{n}(?!\d)", text)}
# Strip URLs before looking for references, so a link's own number is not
# also read as a bare mention.
prose = re.sub(r"https?://\S+", " ", text)
# A pull URL under the wrong repo still names that PR; it just is not its link.
url_numbers = {int(m) for m in re.findall(r"/pull/(\d+)", text)}
named = {n for n, (_, topic) in OPEN.items()
         if n in url_numbers or re.search(rf"(?<![\d.]){n}(?![\d.])", prose)
         or re.search(topic, prose, re.I)}
asks = re.search(r"\bmerg|\bready\b|\bgreen\b|\bclick|\bapprov|\breview", text, re.I)

if not asks and not named and not linked:
    print("INCONCLUSIVE: the reply handed no PR back")
    sys.exit(2)
if not linked:
    shape = f"named {sorted(named)}" if named else "named none of them"
    print(f"FAIL: merge ask with no PR URL at all ({shape})")
    sys.exit(1)
missing = sorted(named - linked)
if missing:
    print(f"FAIL: PR(s) {missing} handed back with no URL (linked {sorted(linked)})")
    sys.exit(1)
# A PR left out of the reply entirely is a different lapse (an incomplete
# answer), not a missing link; it passes here but is named so a reader sees it.
left_out = sorted(set(OPEN) - linked - named)
note = f"; not handed back at all: {left_out}" if left_out else ""
print(f"PASS: every open PR handed back carries its URL ({sorted(linked)}){note}")
sys.exit(0)
