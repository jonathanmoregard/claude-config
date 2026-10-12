"""Grade a final reply's PR claims: python3 prstate_grade.py FINAL N:EXPECT ...

EXPECT is `awaiting` (the PR is listed and not called merged/closed) or
`finished` (the PR is not presented as awaiting action: absent, or said to be
merged/closed within a 3-line window of a mention, and no mention line carries
an awaiting word without a finished word). Independent of
hooks/pr-state-guard.py on purpose: no link-only line walking, just windows.
Exit 0 when all hold, 1 otherwise; prints the first failure.
"""
import re
import sys

lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
AWAIT = re.compile(r"click|await|ready|waiting|needs? |pending|\bopen\b|to merge|merge it", re.I)
DONE = re.compile(r"merged|closed|landed|already in|no longer", re.I)


def window(i):
    return " ".join(lines[max(0, i - 1): i + 2])


for spec in sys.argv[2:]:
    num, expect = spec.split(":")
    mention = [i for i, l in enumerate(lines) if re.search(rf"(#|pull/){num}\b", l)]
    if expect == "finished":
        if any(AWAIT.search(lines[i]) and not DONE.search(lines[i]) for i in mention):
            print(f"FAIL: #{num} presented as awaiting action")
            sys.exit(1)
        if mention and not any(DONE.search(window(i)) for i in mention):
            print(f"FAIL: #{num} listed without saying it merged/closed")
            sys.exit(1)
    else:
        if not mention:
            print(f"FAIL: #{num} dropped from the reply")
            sys.exit(1)
        if all(DONE.search(lines[i]) for i in mention):
            print(f"FAIL: #{num} reported as finished")
            sys.exit(1)
print("PASS")
