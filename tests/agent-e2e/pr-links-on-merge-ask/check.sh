#!/usr/bin/env bash
# PASS: the message that hands the merge back to the user carries the PR's
# full URL, not a bare `#1` or a title.
#
# INCONCLUSIVE, not FAIL, when the reply never hands anything back. The rule
# only governs a merge or review ask, so a run that pushed and said nothing
# about the PR has not exercised it, and grading that as a failure would
# charge the config for a prompt that went somewhere else.
source "$(dirname "$0")/../_lib/checks.sh" "$1"

# Not vacuous: the task itself has to have landed, or the agent never reached
# the point where a merge is what remains.
git -C "$RUN/origin.git" show feat/retry-backoff:src/retry.py 2>/dev/null \
  | grep -q max_wait \
  || { echo "INCONCLUSIVE: the rename never reached origin"; exit 2; }

python3 - "$RUN/final.txt" <<'PY'
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()

# Did the reply hand the PR back at all? Any of: naming a merge, a review, or
# the user's click.
asks = re.search(r"\bmerge\b|\breview\b|your click|awaiting you", text, re.I)
if not asks:
    print("INCONCLUSIVE: the reply never asked for a merge or review")
    sys.exit(2)

url = "https://github.com/acme/widget/pull/1"
if url in text:
    print("PASS: the merge ask carries the full PR URL")
    sys.exit(0)

# Distinguish the two failure shapes, because they read very differently to
# whoever has to act on the message.
bare = re.search(r"#1\b|\bPR\s*1\b|backoff PR", text, re.I)
how = "a bare reference" if bare else "no reference to the PR at all"
print(f"FAIL: asked for a merge with {how} and no URL")
sys.exit(1)
PY
