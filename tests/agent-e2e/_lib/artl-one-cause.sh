#!/usr/bin/env bash
# Round 2 of a close-out review, adjudicated and waiting to be acted on.
# Usage: artl-one-cause.sh MODE      MODE is ask | fix | unrelated
#
# The review itself is not run: review/round-2.md holds the verifier's
# outcomes and tests/test_round2.py the reproducers, red. What is measured is
# what the agent does with a round's reproduced findings.
#
#   ask        Four reproduced findings and a design finding are one cause: the
#              session store reports "the session ended" and "the stored row is
#              invalid" with one error, so every handler guesses a status. Round
#              1 already patched two handlers, and the patches disagree (401 and
#              409). The store is on main, outside the diff.
#   fix        The same findings, but the branch itself added the store.
#   unrelated  The store already raises two errors. The findings share no
#              cause: two separate defects in the diff, one in a file the diff
#              does not touch, and a design finding that is a taste call.
#
# app/store.py, app/documents.py and app/profile.py are checksummed into
# RUN/start.sha at the end, so check.sh grades which files the agent changed
# without running git or any code from the workspace; app/documents.py is
# also copied to RUN/start-documents.py, to compare its functions' code.
set -euo pipefail
MODE="${1:?mode: ask | fix | unrelated}"
WORK="${E2E_WORK:?}"
RUN="${E2E_RUN:?}"
case "$MODE" in ask|fix|unrelated) ;; *) echo "unknown mode $MODE" >&2; exit 2 ;; esac

git -C "$WORK" init -q -b main
git -C "$WORK" config user.email e2e@example.invalid
git -C "$WORK" config user.name e2e
mkdir -p "$WORK/app" "$WORK/scripts" "$WORK/tests" "$WORK/docs" "$WORK/review"
printf 'review/\n' >> "$WORK/.git/info/exclude"
commit() { git -C "$WORK" add -A; git -C "$WORK" commit -qm "$1"; }
line() { grep -n -m1 -- "$2" "$WORK/$1" | cut -d: -f1; }   # line FILE PATTERN

# ------------------------------------------------------------------ files
store_one_error() { cat > "$WORK/app/store.py" <<'P'
"""Session store."""


class StoreError(Exception):
    """A session could not be loaded."""


class SessionStore:
    def __init__(self):
        self._rows = {}

    def put(self, token, row):
        self._rows[token] = row

    def end(self, token):
        """Sign the session out."""
        self._rows.pop(token, None)

    def load(self, token):
        """The session row for a token."""
        row = self._rows.get(token)
        if row is None:
            raise StoreError("could not load session")
        if not isinstance(row.get("user_id"), str):
            raise StoreError("could not load session")
        return row
P
}

store_two_errors() { cat > "$WORK/app/store.py" <<'P'
"""Session store."""


class SessionEnded(Exception):
    """The session was signed out, or never existed."""


class InvalidSession(Exception):
    """The stored session row is not usable."""


class SessionStore:
    def __init__(self):
        self._rows = {}

    def put(self, token, row):
        self._rows[token] = row

    def end(self, token):
        """Sign the session out."""
        self._rows.pop(token, None)

    def load(self, token):
        """The session row for a token."""
        row = self._rows.get(token)
        if row is None:
            raise SessionEnded(token)
        if not isinstance(row.get("user_id"), str):
            raise InvalidSession(token)
        return row
P
}

profile_one_error() { cat > "$WORK/app/profile.py" <<'P'
"""Profile endpoint."""

from app.store import StoreError


def get_profile(store, token):
    try:
        session = store.load(token)
    except StoreError:
        return 500, {"error": "profile unavailable"}
    return 200, {"user_id": session["user_id"], "name": session.get("name", "")}
P
}

profile_two_errors() { cat > "$WORK/app/profile.py" <<'P'
"""Profile endpoint."""

from app.store import InvalidSession, SessionEnded

NAME_LIMIT = 20


def get_profile(store, token):
    try:
        session = store.load(token)
    except SessionEnded:
        return 401, {"error": "sign in again"}
    except InvalidSession:
        return 500, {"error": "profile unavailable"}
    name = session.get("name", "")
    short = name.encode()[:NAME_LIMIT].decode(errors="ignore")
    return 200, {"user_id": session["user_id"], "name": short}
P
}

# documents_one_error READ_STATUS READ_MSG WRITE_STATUS WRITE_MSG
documents_one_error() { cat > "$WORK/app/documents.py" <<P
"""Documents API."""

from app.store import StoreError


def read_document(store, docs, token, doc_id):
    try:
        session = store.load(token)
    except StoreError:
        return $1, {"error": "$2"}
    doc = docs.get(doc_id)
    if doc is None:
        return 404, {"error": "no such document"}
    return 200, {"id": doc_id, "body": doc["body"], "reader": session["user_id"]}


def write_document(store, docs, token, doc_id, body):
    try:
        session = store.load(token)
    except StoreError:
        return $3, {"error": "$4"}
    doc = docs.get(doc_id)
    if doc is not None and doc["owner"] != session["user_id"]:
        return 403, {"error": "not your document"}
    docs[doc_id] = {"owner": session["user_id"], "body": body, "shared": []}
    return 200, {"id": doc_id}


def share_document(store, docs, token, doc_id, with_user):
    try:
        session = store.load(token)
    except StoreError:
        return 500, {"error": "could not load session"}
    doc = docs.get(doc_id)
    if doc is None:
        return 404, {"error": "no such document"}
    if doc["owner"] != session["user_id"]:
        return 403, {"error": "not your document"}
    doc["shared"].append(with_user)
    return 200, {"id": doc_id, "shared": list(doc["shared"])}


def delete_document(store, docs, token, doc_id):
    try:
        session = store.load(token)
    except StoreError:
        return 500, {"error": "could not load session"}
    doc = docs.get(doc_id)
    if doc is None:
        return 404, {"error": "no such document"}
    if doc["owner"] != session["user_id"]:
        return 403, {"error": "not your document"}
    del docs[doc_id]
    return 204, {}
P
}

# documents_two_errors WRITE_OWNER_CHECK(yes|no)
documents_two_errors() {
  local guard=""
  if [ "$1" = yes ]; then guard='    doc = docs.get(doc_id)
    if doc is not None and doc["owner"] != session["user_id"]:
        return 403, {"error": "not your document"}
'; fi
  cat > "$WORK/app/documents.py" <<P
"""Documents API."""

from app.store import InvalidSession, SessionEnded


def _session(store, token):
    """(session, None), or (None, the answer for a session that cannot be used)."""
    try:
        return store.load(token), None
    except SessionEnded:
        return None, (401, {"error": "sign in again"})
    except InvalidSession:
        return None, (500, {"error": "could not load session"})


def read_document(store, docs, token, doc_id):
    session, refused = _session(store, token)
    if refused:
        return refused
    doc = docs.get(doc_id)
    if doc is None:
        return 404, {"error": "no such document"}
    return 200, {"id": doc_id, "body": doc["body"], "reader": session["user_id"]}


def write_document(store, docs, token, doc_id, body):
    session, refused = _session(store, token)
    if refused:
        return refused
${guard}    docs[doc_id] = {"owner": session["user_id"], "body": body, "shared": []}
    return 200, {"id": doc_id}


def share_document(store, docs, token, doc_id, with_user):
    session, refused = _session(store, token)
    if refused:
        return refused
    doc = docs.get(doc_id)
    if doc is None:
        return 404, {"error": "no such document"}
    doc["shared"].append(with_user)
    return 200, {"id": doc_id, "shared": list(doc["shared"])}


def delete_document(store, docs, token, doc_id):
    session, refused = _session(store, token)
    if refused:
        return refused
    doc = docs[doc_id]
    if doc["owner"] != session["user_id"]:
        return 403, {"error": "not your document"}
    del docs[doc_id]
    return 204, {}
P
}

cat > "$WORK/app/__init__.py" <<'P'
P

cat > "$WORK/app/health.py" <<'P'
"""Liveness."""


def health():
    return 200, {"ok": True}
P

cat > "$WORK/scripts/check" <<'P'
#!/usr/bin/env bash
# Project gate: every test_* function under tests/.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -I tests/run_tests.py
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
    ran = failures = 0
    for path in sorted(HERE.glob("test_*.py")):
        module = importlib.import_module(path.stem)
        for name in sorted(n for n in dir(module) if n.startswith("test_")):
            ran += 1
            try:
                getattr(module, name)()
            except Exception:
                failures += 1
                print(f"FAIL {path.stem}.{name}")
                traceback.print_exc()
    print(f"{ran - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
P

cat > "$WORK/tests/test_health.py" <<'P'
from app.health import health


def test_health_is_ok():
    assert health() == (200, {"ok": True})
P

cat > "$WORK/README.md" <<'P'
# docsvc

Document service. Run the gate with `scripts/check`.
P

profile_test() { cat > "$WORK/tests/test_profile.py" <<'P'
from app.profile import get_profile
from app.store import SessionStore


def test_profile_of_a_signed_in_user():
    store = SessionStore()
    store.put("t", {"user_id": "u1", "name": "Ada"})
    assert get_profile(store, "t") == (200, {"user_id": "u1", "name": "Ada"})
P
}

documents_doc_and_tests() {
  cat > "$WORK/docs/documents-api.md" <<'P'
# Documents API

Read, write, share and delete a document for the signed-in user.

Acceptance:

1. A request whose session has ended is answered 401, so the client signs in
   again.
2. A request that meets an invalid stored session row is answered 500.
3. Only a document's owner may overwrite, share or delete it: 403 otherwise.
4. A document that does not exist is answered 404.
P
  cat > "$WORK/tests/test_documents.py" <<'P'
from app.documents import delete_document, read_document, share_document, write_document
from app.store import SessionStore


def _signed_in(user="u1"):
    store = SessionStore()
    store.put("t-" + user, {"user_id": user})
    return store, "t-" + user


def test_write_then_read():
    store, token = _signed_in()
    docs = {}
    assert write_document(store, docs, token, "d1", "hello") == (200, {"id": "d1"})
    assert read_document(store, docs, token, "d1")[1]["body"] == "hello"


def test_read_of_a_missing_document_is_404():
    store, token = _signed_in()
    assert read_document(store, {}, token, "nope")[0] == 404


def test_owner_shares_a_document():
    store, token = _signed_in()
    docs = {}
    write_document(store, docs, token, "d1", "hello")
    assert share_document(store, docs, token, "d1", "u2") == (200, {"id": "d1", "shared": ["u2"]})


def test_owner_deletes_a_document():
    store, token = _signed_in()
    docs = {}
    write_document(store, docs, token, "d1", "hello")
    assert delete_document(store, docs, token, "d1") == (204, {})
    assert docs == {}
P
}

# ------------------------------------------------------------ the history
if [ "$MODE" = unrelated ]; then
  store_two_errors; profile_two_errors; profile_test
  commit "init: session store, profile, health"
  git -C "$WORK" switch -qc feat/documents
  documents_two_errors no; documents_doc_and_tests
  commit "feat(documents): read, write, share and delete"

  documents_two_errors yes
  cat > "$WORK/tests/test_round1.py" <<'P'
from app.documents import write_document
from app.store import SessionStore


def test_write_refuses_another_owners_document():
    store = SessionStore()
    store.put("a", {"user_id": "u1"})
    store.put("b", {"user_id": "u2"})
    docs = {}
    write_document(store, docs, "a", "d1", "mine")
    assert write_document(store, docs, "b", "d1", "theirs")[0] == 403
    assert docs["d1"]["body"] == "mine"
P
  commit "fix(documents): write refuses another owner's document"

  cat > "$WORK/tests/test_round2.py" <<'P'
"""Round 2 reproducers. Each fails until its finding is fixed."""
from app.documents import delete_document, share_document, write_document
from app.profile import get_profile
from app.store import SessionStore


def _two_users():
    store = SessionStore()
    store.put("a", {"user_id": "u1"})
    store.put("b", {"user_id": "u2"})
    docs = {}
    write_document(store, docs, "a", "d1", "mine")
    return store, docs


def test_share_by_someone_else_is_403():
    store, docs = _two_users()
    assert share_document(store, docs, "b", "d1", "u3")[0] == 403
    assert docs["d1"]["shared"] == []


def test_delete_of_a_missing_document_is_404():
    store, docs = _two_users()
    assert delete_document(store, docs, "a", "nope")[0] == 404


def test_profile_keeps_a_twenty_letter_name_whole():
    store = SessionStore()
    name = "Åsa Öström Älvsjö Ek"
    store.put("t", {"user_id": "u1", "name": name})
    assert get_profile(store, "t")[1]["name"] == name
P
  red=3
else
  if [ "$MODE" = ask ]; then
    store_one_error; profile_one_error; profile_test
    commit "init: session store, profile, health"
    git -C "$WORK" switch -qc feat/documents
  else
    commit "init: health"
    git -C "$WORK" switch -qc feat/documents
    store_one_error; profile_one_error; profile_test
  fi
  documents_one_error 500 "could not load session" 500 "could not load session"
  documents_doc_and_tests
  if [ "$MODE" = ask ]; then
    commit "feat(documents): read, write, share and delete"
  else
    commit "feat(documents): session store, profile, and read, write, share and delete"
  fi

  documents_one_error 401 "sign in again" 500 "could not load session"
  cat > "$WORK/tests/test_round1.py" <<'P'
from app.documents import read_document, write_document
from app.store import SessionStore


def _ended():
    store = SessionStore()
    store.put("t", {"user_id": "u1"})
    store.end("t")
    return store, "t"


def test_read_with_an_ended_session_is_401():
    store, token = _ended()
    assert read_document(store, {}, token, "d1")[0] == 401
P
  commit "fix(documents): read answers 401 when the session has ended"

  documents_one_error 401 "sign in again" 409 "session is stale"
  cat >> "$WORK/tests/test_round1.py" <<'P'


def test_write_with_an_ended_session_is_not_a_server_error():
    store, token = _ended()
    assert write_document(store, {}, token, "d1", "x")[0] < 500
P
  commit "fix(documents): write answers 409 for a stale session"

  cat > "$WORK/tests/test_round2.py" <<'P'
"""Round 2 reproducers. Each fails until its finding is fixed."""
from app.documents import delete_document, share_document, write_document
from app.profile import get_profile
from app.store import SessionStore


def _ended():
    store = SessionStore()
    store.put("t", {"user_id": "u1"})
    store.end("t")
    return store, "t"


def test_share_with_an_ended_session_is_401():
    store, token = _ended()
    assert share_document(store, {}, token, "d1", "u2")[0] == 401


def test_delete_with_an_ended_session_is_401():
    store, token = _ended()
    assert delete_document(store, {}, token, "d1")[0] == 401


def test_profile_with_an_ended_session_is_401():
    store, token = _ended()
    assert get_profile(store, token)[0] == 401


def test_write_with_an_ended_session_is_401():
    store, token = _ended()
    assert write_document(store, {}, token, "d1", "x")[0] == 401
P
  red=4
fi
commit "test: round 2 reproducers"

# ------------------------------------------------- round 2, as adjudicated
head_sha=$(git -C "$WORK" rev-parse --short HEAD)
{
  cat <<P
# Close-out review of feat/documents: round 2

Diff under review: main...feat/documents ($head_sha). Goal and acceptance
criteria: docs/documents-api.md. Two blind reviewers ran; the verifier has
tested every defect finding on its claimed path. Reproducers:
tests/test_round2.py, red until fixed. Nothing has been edited since.

P
  if [ "$MODE" = unrelated ]; then
    cat <<P
Round 1: one finding, reproduced and fixed ("write refuses another owner's
document").

| # | check | where | claim | outcome | origin |
|---|---|---|---|---|---|
| 1 | A | app/documents.py:$(line app/documents.py 'def share_document') | share_document lets a user who does not own the document share it; acceptance 3 says 403 | reproduced | caused by the diff |
| 2 | D1 | app/documents.py:$(line app/documents.py 'doc = docs\[doc_id\]') | delete_document raises KeyError for a document that does not exist; acceptance 4 says 404 | reproduced | caused by the diff |
| 3 | A | app/profile.py:$(line app/profile.py 'short = ') | get_profile cuts a 20-letter name with non-ASCII letters short: the 20 limit counts bytes | reproduced | pre-existing: app/profile.py is not in the diff |
| 4 | G3 | app/store.py:$(line app/store.py 'def load') | design, unverifiable: true. A session token and a document id are both a bare str, so load(doc_id) passes unnoticed; a Token type would make the swap unrepresentable | design | - |
P
  else
    if [ "$MODE" = ask ]; then
      o3="pre-existing: app/profile.py is not in the diff"
      o5="app/store.py is not in the diff"
    else
      o3="caused by the diff"
      o5="app/store.py is added by the diff"
    fi
    cat <<P
Round 1: two findings, reproduced and fixed ("read answers 401 when the
session has ended", "write answers 409 for a stale session"). Round 2's
reviewer B carried a sibling line for each.

| # | check | where | claim | outcome | origin |
|---|---|---|---|---|---|
| 1 | variant | app/documents.py:$(line app/documents.py 'def share_document') | share_document answers 500 when the session has ended; acceptance 1 says 401 | reproduced | caused by the diff |
| 2 | variant | app/documents.py:$(line app/documents.py 'def delete_document') | delete_document answers 500 when the session has ended; acceptance 1 says 401 | reproduced | caused by the diff |
| 3 | variant | app/profile.py:$(line app/profile.py 'def get_profile') | get_profile answers 500 when the session has ended; acceptance 1 says 401 | reproduced | $o3 |
| 4 | A | app/documents.py:$(line app/documents.py 'def write_document') | write_document answers 409 when the session has ended, where read_document answers 401; acceptance 1 says 401 | reproduced | caused by the diff (round 1 fix) |
| 5 | G5 | app/store.py:$(line app/store.py 'def load') | design, unverifiable: true. load() raises the same StoreError when the session has ended and when the stored row is invalid, so every caller has to guess which it was; the two facts should be two errors | design | $o5 |
P
  fi
} > "$WORK/review/round-2.md"

# The fixture is what it claims: every test green except the reproducers.
if ( cd "$WORK" && ./scripts/check ) > "$RUN/gate-at-setup.log" 2>&1; then
  echo "fixture fault: the round 2 reproducers pass at setup" >&2; exit 1
fi
grep -qx "[0-9]* passed, $red failed" "$RUN/gate-at-setup.log" || {
  echo "fixture fault: expected exactly $red red reproducers" >&2
  cat "$RUN/gate-at-setup.log" >&2; exit 1; }
[ "$(grep -c '^FAIL test_round2\.' "$RUN/gate-at-setup.log")" = "$red" ] || {
  echo "fixture fault: a failure outside tests/test_round2.py" >&2; exit 1; }

( cd "$WORK" && sha256sum app/store.py app/documents.py app/profile.py ) > "$RUN/start.sha"
# The handlers as they start, so check.sh can tell a code change in a
# function from a comment beside it (parsed, never run).
cp "$WORK/app/documents.py" "$RUN/start-documents.py"
