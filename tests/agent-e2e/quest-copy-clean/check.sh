#!/usr/bin/env bash
# PASS: no copyable value in the walkthrough has punctuation glued after it.
# Every inline code span outside fenced blocks is checked: a span followed
# directly (optionally after closing bold) by . , ; or : fails, because a
# double-click copies that character into the secret name or command.
# INCONCLUSIVE when the reply names neither secret (scenario not engaged).
source "$(dirname "$0")/../_lib/checks.sh" "$1"
[ -s "$RUN/final.txt" ] || { echo "INCONCLUSIVE: no final message"; exit 2; }
grep -q 'DEMO_APP_PRIVATE_KEY' "$RUN/final.txt" || { echo "INCONCLUSIVE: secrets not named"; exit 2; }
read -r spans glued sample < <(python3 - "$RUN/final.txt" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
prose = re.sub(r"```.*?```", "", text, flags=re.S)
spans = re.findall(r"`[^`\n]+`", prose)
glued = re.findall(r"`[^`\n]+`(?:\*\*|\*|_)?[.,;:](?=\s|$)", prose)
print(len(spans), len(glued), (glued[0] if glued else "-").replace(" ", "_"))
PY
)
[ "$glued" -eq 0 ] || fail "$glued of $spans code spans carry trailing punctuation, e.g. $sample"
pass "$spans code spans, none with trailing punctuation"
