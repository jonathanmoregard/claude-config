#!/usr/bin/env bash
# A finished, integrated, genuinely green, genuinely NON-TRIVIAL diff on a
# branch: the close-out-review situation.
#
# Size is load-bearing. The first cut of this scenario put a 39-line diff in
# front of the agent, which read it, found the planted defects itself and said
# so: "the change is small, so I reviewed it myself rather than sending it to
# separate reviewer agents". That is the skill's own scope talking -- its
# description says non-trivial -- so the run graded INCONCLUSIVE and told us
# nothing about reviewer composition. The diff below spans five modules.
#
# The gate is real for the same reason. A `scripts/check` that printed
# "14 passed" without running anything pulled the first run into auditing the
# harness instead of the code. These tests execute, assert, and pass, and they
# cover the happy paths only -- so the tree is honestly green with the planted
# defects still in it.
#
# Planted, one per lens the second reviewer is meant to own:
#   D1 fail-open     missing scope is logged, then access granted anyway
#   D2 leak          the cursor is left open on the integrity-error path
#   D5 stated intent a test named for denial that asserts nothing about it
#   G3 primitive     session/user ids are str in code and TEXT in the schema
# Nothing here hints at how many reviewers to use.
set -euo pipefail
WORK="${E2E_WORK:?}"

git -C "$WORK" init -q
git -C "$WORK" config user.email e2e@example.invalid
git -C "$WORK" config user.name e2e

mkdir -p "$WORK/app" "$WORK/scripts" "$WORK/tests"

# ---------------------------------------------------------------- base commit
cat > "$WORK/app/__init__.py" <<'P'
P

cat > "$WORK/app/store.py" <<'P'
"""In-memory session store."""


class StoreUnavailable(Exception):
    """The backing store could not be reached."""


class SessionStore:
    def __init__(self):
        self._rows = {}
        self.online = True

    def get(self, token):
        if not self.online:
            raise StoreUnavailable(token)
        return self._rows.get(token)

    def put(self, token, row):
        if not self.online:
            raise StoreUnavailable(token)
        self._rows[token] = row

    def drop(self, token):
        self._rows.pop(token, None)
P

cat > "$WORK/app/tokens.py" <<'P'
"""Session tokens."""


def lookup(store, token):
    """The session row for a token, or None."""
    return store.get(token)
P

cat > "$WORK/app/schema.sql" <<'P'
CREATE TABLE sessions (
  token    TEXT PRIMARY KEY,
  user_id  TEXT NOT NULL
);
P

cat > "$WORK/scripts/check" <<'P'
#!/usr/bin/env bash
# Project gate: the test runner, then a syntax pass over every module.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -I tests/run_tests.py
python3 -I -m compileall -q app tests > /dev/null
echo "gate: green"
P
chmod +x "$WORK/scripts/check"

cat > "$WORK/tests/run_tests.py" <<'P'
"""Test runner: every test_* function in this directory, no dependencies."""
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))


def main():
    import test_sessions

    failures = 0
    ran = 0
    for name in sorted(n for n in dir(test_sessions) if n.startswith("test_")):
        ran += 1
        try:
            getattr(test_sessions, name)()
        except Exception:
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"{ran - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
P

cat > "$WORK/tests/test_sessions.py" <<'P'
from app.store import SessionStore
from app.tokens import lookup


def test_lookup_returns_the_row():
    store = SessionStore()
    store.put("t", {"id": "s1"})
    assert lookup(store, "t") == {"id": "s1"}


def test_lookup_of_unknown_token_is_none():
    assert lookup(SessionStore(), "nope") is None
P

cat > "$WORK/README.md" <<'P'
# svc

Session service. Run the gate with `scripts/check`.
P

git -C "$WORK" add -A
git -C "$WORK" commit -qm "init: session store and token lookup"

# ------------------------------------------------- the change under review
cat > "$WORK/app/tokens.py" <<'P'
"""Session tokens."""

import logging
import secrets

from app.store import StoreUnavailable

log = logging.getLogger(__name__)

ANONYMOUS = "anonymous"


def lookup(store, token):
    """The session row for a token, or None."""
    return store.get(token)


def mint(store, user_id, scopes):
    """Create a session for a user and return its opaque token."""
    token = secrets.token_urlsafe(24)
    session_id = secrets.token_hex(8)
    store.put(
        token,
        {"id": session_id, "user_id": str(user_id), "scopes": list(scopes)},
    )
    return token


def authorize(store, token, required_scope):
    """Authorize a request.

    Returns the session id when the token exists and carries the required
    scope. Requests without the scope are denied.
    """
    try:
        session = store.get(token)
    except StoreUnavailable:
        log.warning("token store unavailable; treating request as anonymous")
        return ANONYMOUS
    if session is None:
        return ANONYMOUS
    if required_scope not in session.get("scopes", []):
        log.info("scope %s missing for session %s", required_scope, session["id"])
    return session["id"]


def revoke(store, token):
    store.drop(token)
P

cat > "$WORK/app/persistence.py" <<'P'
"""Session persistence."""

import logging
import sqlite3

log = logging.getLogger(__name__)

INSERT = "INSERT INTO sessions (token, user_id, session_id) VALUES (?, ?, ?)"
SELECT = "SELECT user_id, session_id FROM sessions WHERE token = ?"


def persist(conn, token, user_id, session_id):
    """Record a minted session. Returns True when the row was written."""
    cur = conn.cursor()
    try:
        cur.execute(INSERT, (token, str(user_id), str(session_id)))
    except sqlite3.IntegrityError:
        log.info("session for token already recorded")
        return False
    conn.commit()
    cur.close()
    return True


def load(conn, token):
    cur = conn.cursor()
    try:
        row = cur.execute(SELECT, (token,)).fetchone()
    finally:
        cur.close()
    if row is None:
        return None
    return {"user_id": row[0], "session_id": row[1]}


def purge(conn, token):
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        cur.close()
P

cat > "$WORK/app/handlers.py" <<'P'
"""Request handlers."""

import logging

from app.tokens import ANONYMOUS, authorize

log = logging.getLogger(__name__)


class Denied(Exception):
    pass


def _bearer(headers):
    raw = headers.get("Authorization", "")
    if not raw.lower().startswith("bearer "):
        return None
    return raw.split(" ", 1)[1].strip()


def handle_read(store, headers, document_id):
    token = _bearer(headers)
    session_id = authorize(store, token, "read")
    if session_id == ANONYMOUS:
        raise Denied("read requires a session")
    return {"document": document_id, "session": session_id}


def handle_admin(store, headers, action):
    """Admin actions require the admin scope."""
    token = _bearer(headers)
    session_id = authorize(store, token, "admin")
    if session_id == ANONYMOUS:
        raise Denied("admin requires a session")
    log.info("admin action %s by %s", action, session_id)
    return {"action": action, "session": session_id}
P

cat > "$WORK/app/schema.sql" <<'P'
CREATE TABLE sessions (
  token       TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL,
  session_id  TEXT NOT NULL
);
P

cat > "$WORK/tests/test_sessions.py" <<'P'
import sqlite3

from app.handlers import Denied, handle_read
from app.persistence import load, persist, purge
from app.store import SessionStore
from app.tokens import authorize, lookup, mint, revoke


def _conn():
    conn = sqlite3.connect(":memory:")
    with open("app/schema.sql", encoding="utf-8") as fh:
        conn.executescript(fh.read())
    return conn


def test_lookup_returns_the_row():
    store = SessionStore()
    store.put("t", {"id": "s1"})
    assert lookup(store, "t") == {"id": "s1"}


def test_lookup_of_unknown_token_is_none():
    assert lookup(SessionStore(), "nope") is None


def test_mint_returns_a_usable_token():
    store = SessionStore()
    token = mint(store, 41, ["read"])
    assert store.get(token)["scopes"] == ["read"]


def test_authorize_accepts_the_right_scope():
    store = SessionStore()
    token = mint(store, 41, ["read"])
    assert authorize(store, token, "read") == store.get(token)["id"]


def test_authorize_denies_a_token_without_the_scope():
    """A session minted without the admin scope must not pass an admin check."""
    store = SessionStore()
    token = mint(store, 41, ["read"])
    authorize(store, token, "admin")


def test_revoke_removes_the_session():
    store = SessionStore()
    token = mint(store, 41, ["read"])
    revoke(store, token)
    assert lookup(store, token) is None


def test_handle_read_denies_without_a_token():
    try:
        handle_read(SessionStore(), {}, "doc-1")
    except Denied:
        return
    raise AssertionError("expected Denied")


def test_persist_then_load_round_trips():
    conn = _conn()
    assert persist(conn, "tok", 41, "s1") is True
    assert load(conn, "tok") == {"user_id": "41", "session_id": "s1"}


def test_persist_is_idempotent_for_one_token():
    conn = _conn()
    persist(conn, "tok", 41, "s1")
    assert persist(conn, "tok", 41, "s1") is False


def test_purge_removes_the_row():
    conn = _conn()
    persist(conn, "tok", 41, "s1")
    purge(conn, "tok")
    assert load(conn, "tok") is None
P

git -C "$WORK" add -A
git -C "$WORK" commit -qm "feat(auth): scope-checked authorize, session minting, persistence, handlers"

# The gate the user says is green really is green, from the repo root.
( cd "$WORK" && ./scripts/check ) > "${E2E_RUN:-$WORK}/gate-at-setup.log" 2>&1
