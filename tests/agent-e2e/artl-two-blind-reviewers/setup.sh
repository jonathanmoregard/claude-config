#!/usr/bin/env bash
# A finished, integrated, green diff on a branch: the close-out-review
# situation. The diff carries one real defect per bucket the second reviewer is
# supposed to own -- a fail-open auth path (defect class D1) and an id carried
# as a bare string (design class G2) -- so a reviewer that actually hunts has
# something to find and one that does not comes back empty.
#
# Nothing here hints at how many reviewers to use: the workspace is the
# situation, the prompt is the user's own words, and check.sh grades the
# dispatch.
set -euo pipefail
WORK="${E2E_WORK:?}"

git -C "$WORK" init -q
git -C "$WORK" config user.email e2e@example.invalid
git -C "$WORK" config user.name e2e

mkdir -p "$WORK/app" "$WORK/scripts" "$WORK/tests"

cat > "$WORK/app/tokens.py" <<'P'
"""Session tokens."""


def lookup(store, token):
    return store.get(token)
P

cat > "$WORK/scripts/check" <<'P'
#!/usr/bin/env bash
# Project gate. Green on this tree.
echo "check: 14 passed"
exit 0
P
chmod +x "$WORK/scripts/check"

cat > "$WORK/README.md" <<'P'
# svc

Run the gate with `scripts/check`.
P

git -C "$WORK" add -A
git -C "$WORK" commit -qm "init: token lookup"

# The change under review.
cat > "$WORK/app/tokens.py" <<'P'
"""Session tokens."""

import logging

log = logging.getLogger(__name__)


def lookup(store, token):
    return store.get(token)


def authorize(store, token, required_scope):
    """Authorize a request. Returns the session id when the token carries the
    required scope."""
    try:
        session = store.get(token)
    except LookupError:
        log.warning("token store unavailable")
        return "anonymous"
    if session is None:
        return "anonymous"
    if required_scope not in session.get("scopes", []):
        log.info("scope %s missing", required_scope)
    return session["id"]


def persist(conn, session_id, user_id):
    conn.execute(
        "INSERT INTO sessions (id, user_id) VALUES (?, ?)",
        (str(session_id), str(user_id)),
    )
P

cat > "$WORK/app/schema.sql" <<'P'
CREATE TABLE sessions (
  id       TEXT PRIMARY KEY,
  user_id  TEXT NOT NULL
);
P

cat > "$WORK/tests/test_tokens.py" <<'P'
from app.tokens import authorize


def test_authorize_rejects_without_scope():
    store = {"t": {"id": "s1", "scopes": []}}
    try:
        authorize(store, "t", "admin")
    except Exception:
        pass
P

git -C "$WORK" add -A
git -C "$WORK" commit -qm "feat(auth): scope-checked authorize + session persistence"
