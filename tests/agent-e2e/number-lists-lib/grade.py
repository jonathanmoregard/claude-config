import re, sys

t = open(sys.argv[1] + "/final.txt").read()
items = num = 0
rows = t.splitlines()
hdr_skipped = False
for l in rows:
    s = l.strip()
    if not s.startswith("|"):
        continue
    if re.match(r"^\|[\s:\-|]+\|?$", s):
        continue
    if not hdr_skipped:
        hdr_skipped = True
        continue
    items += 1
    c = [x.strip() for x in s.strip("|").split("|")][0]
    if re.match(r"^\**\d+[.)]?\**$", c):
        num += 1
for l in rows:
    s = l.strip()
    if s.startswith("|"):
        continue
    if re.match(r"^([-*\u2022]|\d+[.)])\s+\S", s):
        items += 1
        if re.match(r"^\d+[.)]\s", s):
            num += 1
print(f"items={items} numbered={num}")
if items < 3:
    sys.exit(2)
sys.exit(0 if num == items else 1)
