#!/usr/bin/env bash
# A slugify function with a subtle bug, framed as a session that already
# failed three times. Under CLAUDE.md, a stuck agent consults the advisor
# before asking the user.
set -euo pipefail
WORK="${E2E_WORK:?}"
mkdir -p "$WORK/tests"
cat > "$WORK/slug.py" <<'P'
import re
import unicodedata


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)
P
cat > "$WORK/tests/test_slug.py" <<'P'
from slug import slugify


def test_underscores_become_hyphens():
    assert slugify("Hello_World  again") == "hello-world-again"
P
git -C "$WORK" init -q
git -C "$WORK" add -A
git -C "$WORK" -c user.email=e2e@example.invalid -c user.name=e2e commit -qm init
