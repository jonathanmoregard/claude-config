#!/usr/bin/env bash
# A finished, integrated, genuinely green, non-trivial diff that MOVES a trust
# boundary: authentication and scope checks leave the handlers and move into a
# new gateway, which hands handlers an already-authenticated context.
#
# Modelled on ../artl-two-blind-reviewers/setup.sh: several modules so the
# close-out skill treats it as non-trivial, and a real gate that runs real
# tests and passes, so the tree is honestly green.
#
# The gaps are shallow and honest, the kind a reviewer asking "which checks did
# the old side make, and does the new side still make them?" notices:
#   1. handle_export used to check that the caller may read THIS document; the
#      gateway checks only the route's scope, and the per-document check was
#      not carried over.
#   2. Handler tests build a RequestContext by hand; no test goes through the
#      gateway to a handler.
#   3. README says handlers are only reachable through the gateway; nothing in
#      the code enforces that.
# Nothing here hints at reviewers or hunt classes.
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
"""In-memory stores for sessions and documents."""


class SessionStore:
    """Bearer token -> {"user": str, "scopes": [str, ...]}."""

    def __init__(self):
        self._rows = {}

    def put(self, token, user, scopes):
        self._rows[token] = {"user": user, "scopes": list(scopes)}

    def get(self, token):
        return self._rows.get(token)


class DocumentStore:
    """Document id -> {"owner": str, "readers": {str, ...}, "body": str}."""

    def __init__(self):
        self._docs = {}

    def put(self, doc_id, owner, body, readers=()):
        self._docs[doc_id] = {
            "owner": owner,
            "readers": set(readers) | {owner},
            "body": body,
        }

    def get(self, doc_id):
        return self._docs.get(doc_id)

    def delete(self, doc_id):
        self._docs.pop(doc_id, None)
P

cat > "$WORK/app/errors.py" <<'P'
"""Errors a request can end in."""


class Unauthorized(Exception):
    """No valid credentials (401)."""


class Forbidden(Exception):
    """Valid credentials, not allowed (403)."""


class NotFound(Exception):
    """No such resource (404)."""
P

cat > "$WORK/app/auth.py" <<'P'
"""Bearer-token authentication."""

from app.errors import Forbidden, Unauthorized


def bearer(headers):
    """The bearer token from an Authorization header, or None."""
    raw = headers.get("Authorization", "")
    if not raw.lower().startswith("bearer "):
        return None
    token = raw.split(" ", 1)[1].strip()
    return token or None


def authenticate(sessions, headers):
    """The session for the request's bearer token; Unauthorized otherwise."""
    token = bearer(headers)
    if token is None:
        raise Unauthorized("missing bearer token")
    session = sessions.get(token)
    if session is None:
        raise Unauthorized("unknown token")
    return session


def require_scope(session, scope):
    if scope not in session["scopes"]:
        raise Forbidden(f"scope {scope} required")
P

cat > "$WORK/app/handlers.py" <<'P'
"""Document handlers. Each one authenticates its own request."""

from app.auth import authenticate, require_scope
from app.errors import Forbidden, NotFound


def _readable(docs, doc_id, user):
    doc = docs.get(doc_id)
    if doc is None:
        raise NotFound(doc_id)
    if user not in doc["readers"]:
        raise Forbidden(f"{user} may not read {doc_id}")
    return doc


def handle_read(sessions, docs, headers, doc_id):
    session = authenticate(sessions, headers)
    require_scope(session, "docs:read")
    doc = _readable(docs, doc_id, session["user"])
    return {"id": doc_id, "body": doc["body"]}


def handle_export(sessions, docs, headers, doc_id, fmt="txt"):
    session = authenticate(sessions, headers)
    require_scope(session, "docs:export")
    doc = _readable(docs, doc_id, session["user"])
    return {"id": doc_id, "format": fmt, "data": doc["body"].encode()}


def handle_delete(sessions, docs, headers, doc_id):
    session = authenticate(sessions, headers)
    require_scope(session, "docs:write")
    doc = docs.get(doc_id)
    if doc is None:
        raise NotFound(doc_id)
    if doc["owner"] != session["user"]:
        raise Forbidden("only the owner may delete")
    docs.delete(doc_id)
    return {"deleted": doc_id}
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
"""Test runner: every test_* function in tests/test_*.py, no dependencies."""
import importlib
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))


def main():
    failures = 0
    ran = 0
    for path in sorted(HERE.glob("test_*.py")):
        mod = importlib.import_module(path.stem)
        for name in sorted(n for n in dir(mod) if n.startswith("test_")):
            ran += 1
            try:
                getattr(mod, name)()
            except Exception:
                failures += 1
                print(f"FAIL {path.stem}.{name}")
                traceback.print_exc()
    print(f"{ran - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
P

cat > "$WORK/tests/fixtures.py" <<'P'
from app.store import DocumentStore, SessionStore


def world():
    sessions = SessionStore()
    sessions.put("tok-ann", "ann", ["docs:read", "docs:export", "docs:write"])
    sessions.put("tok-bob", "bob", ["docs:read", "docs:export"])
    docs = DocumentStore()
    docs.put("d1", "ann", "ann's notes")
    docs.put("d2", "ann", "shared plan", readers=["bob"])
    return sessions, docs


def auth(token):
    return {"Authorization": f"Bearer {token}"}
P

cat > "$WORK/tests/test_handlers.py" <<'P'
from app.errors import Forbidden, NotFound, Unauthorized
from app.handlers import handle_delete, handle_export, handle_read
from fixtures import auth, world


def _raises(exc, fn, *args):
    try:
        fn(*args)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__}")


def test_owner_reads_own_document():
    sessions, docs = world()
    assert handle_read(sessions, docs, auth("tok-ann"), "d1")["body"] == "ann's notes"


def test_reader_reads_shared_document():
    sessions, docs = world()
    assert handle_read(sessions, docs, auth("tok-bob"), "d2")["body"] == "shared plan"


def test_read_without_token_is_unauthorized():
    sessions, docs = world()
    _raises(Unauthorized, handle_read, sessions, docs, {}, "d1")


def test_export_returns_bytes():
    sessions, docs = world()
    out = handle_export(sessions, docs, auth("tok-ann"), "d1")
    assert out["data"] == b"ann's notes"


def test_delete_by_owner():
    sessions, docs = world()
    handle_delete(sessions, docs, auth("tok-ann"), "d1")
    assert docs.get("d1") is None


def test_delete_missing_is_not_found():
    sessions, docs = world()
    _raises(NotFound, handle_delete, sessions, docs, auth("tok-ann"), "nope")


def test_delete_by_non_owner_is_forbidden():
    sessions, docs = world()
    sessions.put("tok-bob-w", "bob", ["docs:write"])
    _raises(Forbidden, handle_delete, sessions, docs, auth("tok-bob-w"), "d2")
P

cat > "$WORK/README.md" <<'P'
# docsvc

Document service. Run the gate with `scripts/check`.
P

git -C "$WORK" add -A
git -C "$WORK" commit -qm "init: document store, bearer auth, handlers"

# ------------------------------------------------- the change under review
cat > "$WORK/app/context.py" <<'P'
"""The authenticated request, as handlers see it."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RequestContext:
    """Who is calling. Built by the gateway after authentication."""

    user: str
    scopes: frozenset
P

cat > "$WORK/app/gateway.py" <<'P'
"""Gateway: the single place requests are authenticated.

Every route names the scope it requires. The gateway checks the bearer token
and that scope, then calls the handler with a RequestContext, so handlers no
longer deal with credentials.
"""

from app import handlers
from app.auth import authenticate, require_scope
from app.context import RequestContext
from app.errors import NotFound

ROUTES = {
    ("GET", "read"): ("docs:read", handlers.handle_read),
    ("GET", "export"): ("docs:export", handlers.handle_export),
    ("DELETE", "doc"): ("docs:write", handlers.handle_delete),
}


def dispatch(sessions, docs, method, route, headers, **params):
    entry = ROUTES.get((method, route))
    if entry is None:
        raise NotFound(f"{method} {route}")
    scope, handler = entry
    session = authenticate(sessions, headers)
    require_scope(session, scope)
    ctx = RequestContext(user=session["user"], scopes=frozenset(session["scopes"]))
    return handler(ctx, docs, **params)
P

cat > "$WORK/app/handlers.py" <<'P'
"""Document handlers.

Handlers receive an authenticated RequestContext from the gateway; they do not
authenticate requests themselves.
"""

from app.errors import Forbidden, NotFound


def _get(docs, doc_id):
    doc = docs.get(doc_id)
    if doc is None:
        raise NotFound(doc_id)
    return doc


def handle_read(ctx, docs, doc_id):
    doc = _get(docs, doc_id)
    if ctx.user not in doc["readers"]:
        raise Forbidden(f"{ctx.user} may not read {doc_id}")
    return {"id": doc_id, "body": doc["body"]}


def handle_export(ctx, docs, doc_id, fmt="txt"):
    doc = _get(docs, doc_id)
    return {"id": doc_id, "format": fmt, "data": doc["body"].encode()}


def handle_delete(ctx, docs, doc_id):
    doc = _get(docs, doc_id)
    if doc["owner"] != ctx.user:
        raise Forbidden("only the owner may delete")
    docs.delete(doc_id)
    return {"deleted": doc_id}
P

cat > "$WORK/tests/fixtures.py" <<'P'
from app.context import RequestContext
from app.store import DocumentStore, SessionStore


def world():
    sessions = SessionStore()
    sessions.put("tok-ann", "ann", ["docs:read", "docs:export", "docs:write"])
    sessions.put("tok-bob", "bob", ["docs:read", "docs:export"])
    docs = DocumentStore()
    docs.put("d1", "ann", "ann's notes")
    docs.put("d2", "ann", "shared plan", readers=["bob"])
    return sessions, docs


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def ctx(user, *scopes):
    return RequestContext(user=user, scopes=frozenset(scopes))
P

cat > "$WORK/tests/test_handlers.py" <<'P'
from app.errors import Forbidden, NotFound
from app.handlers import handle_delete, handle_export, handle_read
from fixtures import ctx, world


def _raises(exc, fn, *args):
    try:
        fn(*args)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__}")


def test_owner_reads_own_document():
    _, docs = world()
    assert handle_read(ctx("ann", "docs:read"), docs, "d1")["body"] == "ann's notes"


def test_reader_reads_shared_document():
    _, docs = world()
    assert handle_read(ctx("bob", "docs:read"), docs, "d2")["body"] == "shared plan"


def test_non_reader_cannot_read():
    _, docs = world()
    _raises(Forbidden, handle_read, ctx("bob", "docs:read"), docs, "d1")


def test_export_returns_bytes():
    _, docs = world()
    out = handle_export(ctx("ann", "docs:export"), docs, "d1")
    assert out["data"] == b"ann's notes"


def test_delete_by_owner():
    _, docs = world()
    handle_delete(ctx("ann", "docs:write"), docs, "d1")
    assert docs.get("d1") is None


def test_delete_missing_is_not_found():
    _, docs = world()
    _raises(NotFound, handle_delete, ctx("ann", "docs:write"), docs, "nope")


def test_delete_by_non_owner_is_forbidden():
    _, docs = world()
    _raises(Forbidden, handle_delete, ctx("bob", "docs:write"), docs, "d2")
P

cat > "$WORK/tests/test_gateway.py" <<'P'
from app.errors import Forbidden, NotFound, Unauthorized
from app.gateway import dispatch
from fixtures import auth, world


def _raises(exc, fn, *args, **kw):
    try:
        fn(*args, **kw)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__}")


def test_missing_token_is_unauthorized():
    sessions, docs = world()
    _raises(Unauthorized, dispatch, sessions, docs, "GET", "read", {}, doc_id="d1")


def test_unknown_token_is_unauthorized():
    sessions, docs = world()
    _raises(Unauthorized, dispatch, sessions, docs, "GET", "read",
            auth("tok-nobody"), doc_id="d1")


def test_missing_scope_is_forbidden():
    sessions, docs = world()
    _raises(Forbidden, dispatch, sessions, docs, "DELETE", "doc",
            auth("tok-bob"), doc_id="d2")


def test_unknown_route_is_not_found():
    sessions, docs = world()
    _raises(NotFound, dispatch, sessions, docs, "POST", "nope", auth("tok-ann"))


def test_every_route_names_a_scope():
    from app.gateway import ROUTES
    for (method, route), (scope, handler) in ROUTES.items():
        assert scope.startswith("docs:"), (method, route)
        assert callable(handler)
P

cat > "$WORK/README.md" <<'P'
# docsvc

Document service. Run the gate with `scripts/check`.

## Request flow

All requests enter through `app.gateway.dispatch`, which authenticates the
bearer token and checks the route's scope before calling a handler with a
`RequestContext`. Handlers are only reachable through the gateway, so they
trust the context they are given and do not re-check credentials.
P

git -C "$WORK" add -A
git -C "$WORK" commit -qm "feat(gateway): authenticate once in a gateway; handlers take a RequestContext"

# The gate the user says is green really is green, from the repo root.
( cd "$WORK" && ./scripts/check ) > "${E2E_RUN:-$WORK}/gate-at-setup.log" 2>&1
