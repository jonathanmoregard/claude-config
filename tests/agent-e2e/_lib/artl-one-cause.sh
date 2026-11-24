#!/usr/bin/env bash
# Fixture for artl-one-cause-*: round 2 of a close-out review, adjudicated in
# review/round-2.md, reproducers red in tests/test_round2.py, round 1 in the
# review ledger. Usage: artl-one-cause.sh WORK RUN ask|fix|unrelated
#   ask        the findings share one cause: SessionStore.load raises one
#              StoreError for an ended session and an invalid row, so each
#              handler guesses a status (round 1's patches disagree: 401, 409).
#              The store is on main, outside the diff.
#   fix        the same, but the branch itself added the store.
#   unrelated  the store already has two errors; two separate defects in the
#              handlers plus a design taste call on the store.
set -euo pipefail
WORK="${1:?work dir}" RUN="${2:?run dir}" MODE="${3:?ask|fix|unrelated}"
cd "$WORK"
git init -q -b main
git config user.email e2e@example.invalid
git config user.name e2e
commit() { git add -A && git commit -qm "$1"; }
line() { grep -n -m1 -- "$2" "$1" | cut -d: -f1; }
test_case() { printf '\n    def test_%s(self):\n        %s\n' "$1" "$2" >> tests/test_documents.py; }
mkdir -p app tests scripts docs review
printf 'review/\n' >> .git/info/exclude
printf '__pycache__/\n' > .gitignore
: > app/__init__.py
printf '# docsvc\n\nDocument service. Gate: `scripts/check`.\n' > README.md
printf '#!/usr/bin/env bash\nset -euo pipefail\ncd "$(dirname "$0")/.."\npython3 -m unittest discover -s tests -q\n' > scripts/check
chmod +x scripts/check

store() { # ENDED INVALID: the error load raises for each
  printf '"""Session store."""\n\n\nclass %s(Exception):\n    """A session cannot be used."""\n' "$1" > app/store.py
  [ "$1" = "$2" ] || printf '\n\nclass %s(Exception):\n    """The stored session row is not usable."""\n' "$2" >> app/store.py
  cat >> app/store.py <<P


class SessionStore:
    def __init__(self):
        self.rows = {}

    def put(self, token, row):
        self.rows[token] = row

    def end(self, token):
        self.rows.pop(token, None)

    def load(self, token):
        row = self.rows.get(token)
        if row is None:
            raise $1("could not load session")
        if not isinstance(row.get("user_id"), str):
            raise $2("could not load session")
        return row
P
}
case $MODE in
  ask) store StoreError StoreError ;;
  unrelated) store SessionEnded InvalidSession ;;
esac
commit "init: docsvc skeleton"
git switch -qc feat/documents
[ "$MODE" != fix ] || store StoreError StoreError
cat > docs/documents-api.md <<'M'
# Documents API acceptance

1. A request whose session has ended is answered 401, so the client signs in again.
2. A request that meets an invalid stored session row is answered 500.
3. Only a document's owner may overwrite or share it: 403 otherwise.
4. A document that does not exist is answered 404.
M
cat > tests/test_documents.py <<'P'
import unittest

from app.documents import read_document, share_document, write_document
from app.store import SessionStore


class Documents(unittest.TestCase):
    def setUp(self):
        self.store, self.docs = SessionStore(), {}
        self.store.put("a", {"user_id": "u1"})
        self.store.put("b", {"user_id": "u2"})

    def test_write_read_share(self):
        self.assertEqual(write_document(self.store, self.docs, "a", "d1", "hi"), (200, "d1"))
        self.assertEqual(read_document(self.store, self.docs, "a", "d1"), (200, "hi"))
        self.assertEqual(share_document(self.store, self.docs, "a", "d1", "u2"), (200, ["u2"]))
P
round2() { # TEST...: the round 2 reproducers, each "name<TAB>assertion"
  printf '"""Round 2 reproducers, red until fixed."""\nimport unittest\n\nfrom app.documents import read_document, share_document, write_document\nfrom app.store import SessionStore\n\n\nclass Round2(unittest.TestCase):\n    def setUp(self):\n        self.store, self.docs = SessionStore(), {}\n        self.store.put("a", {"user_id": "u1"})\n        self.store.put("b", {"user_id": "u2"})\n        write_document(self.store, self.docs, "a", "d1", "mine")\n        self.store.end("e")\n' > tests/test_round2.py
  for t in "$@"; do printf '\n    def test_%s(self):\n        %s\n' "${t%%	*}" "${t#*	}" >> tests/test_round2.py; done
}

if [ "$MODE" = unrelated ]; then
  documents() { # OWNER_CHECK: the lines round 1 added to write_document, or none
    cat > app/documents.py <<P
"""Documents API."""
from app.store import InvalidSession, SessionEnded


def _user(store, token):
    """(user_id, None), or (None, the answer for a session that cannot be used)."""
    try:
        return store.load(token)["user_id"], None
    except SessionEnded:
        return None, (401, "sign in again")
    except InvalidSession:
        return None, (500, "could not load session")


def read_document(store, docs, token, doc_id):
    _, refused = _user(store, token)
    return refused or (200, docs[doc_id]["body"])


def write_document(store, docs, token, doc_id, body):
    user, refused = _user(store, token)
    if refused:
        return refused
$1    docs[doc_id] = {"owner": user, "body": body, "shared": []}
    return 200, doc_id


def share_document(store, docs, token, doc_id, with_user):
    user, refused = _user(store, token)
    if refused:
        return refused
    docs[doc_id]["shared"].append(with_user)
    return 200, docs[doc_id]["shared"]
P
  }
  documents ""
  commit "feat(documents): read, write and share"
  tok="$(git rev-parse --short HEAD)-1030"
  r1=("A	app/documents.py:$(line app/documents.py 'def write_document')	write_document lets a user overwrite another owner's document")
  documents '    if doc_id in docs and docs[doc_id]["owner"] != user:
        return 403, "not your document"
'
  test_case write_refuses_another_owner 'write_document(self.store, self.docs, "a", "d1", "x"); self.assertEqual(write_document(self.store, self.docs, "b", "d1", "y")[0], 403)'
  commit "fix(documents): write refuses another owner's document"
  round2 "share_by_someone_else_is_403	self.assertEqual(share_document(self.store, self.docs, \"b\", \"d1\", \"u3\")[0], 403)" \
         "read_of_a_missing_document_is_404	self.assertEqual(read_document(self.store, self.docs, \"a\", \"nope\")[0], 404)"
  findings="| 1 | A | app/documents.py:$(line app/documents.py 'def share_document') | share_document lets a non-owner share; acceptance 3 says 403 | reproduced | caused by the diff |
| 2 | D1 | app/documents.py:$(line app/documents.py 'def read_document') | read_document raises KeyError for a missing document; acceptance 4 says 404 | reproduced | caused by the diff |
| 3 | G3 | app/store.py:$(line app/store.py 'def load') | design, unverifiable: true. Tokens and document ids are both bare str, so load(doc_id) passes unnoticed; a Token type would prevent it | design | - |"
else
  documents() { # READ WRITE: what read_document and write_document answer on a StoreError
    cat > app/documents.py <<P
"""Documents API."""
from app.store import StoreError


def read_document(store, docs, token, doc_id):
    try:
        store.load(token)
    except StoreError:
        return $1
    if doc_id not in docs:
        return 404, "no such document"
    return 200, docs[doc_id]["body"]


def write_document(store, docs, token, doc_id, body):
    try:
        user = store.load(token)["user_id"]
    except StoreError:
        return $2
    if doc_id in docs and docs[doc_id]["owner"] != user:
        return 403, "not your document"
    docs[doc_id] = {"owner": user, "body": body, "shared": []}
    return 200, doc_id


def share_document(store, docs, token, doc_id, with_user):
    try:
        user = store.load(token)["user_id"]
    except StoreError:
        return 500, "could not load session"
    if doc_id not in docs:
        return 404, "no such document"
    if docs[doc_id]["owner"] != user:
        return 403, "not your document"
    docs[doc_id]["shared"].append(with_user)
    return 200, docs[doc_id]["shared"]
P
  }
  documents '500, "could not load session"' '500, "could not load session"'
  if [ "$MODE" = fix ]; then
    commit "feat(documents): session store; read, write and share"
    origin="app/store.py is added by the diff"
  else
    commit "feat(documents): read, write and share"
    origin="app/store.py is not in the diff"
  fi
  tok="$(git rev-parse --short HEAD)-1030"
  r1=("A	app/documents.py:$(line app/documents.py 'def read_document')	read_document answers 500 when the session has ended; acceptance 1 says 401"
      "variant	app/documents.py:$(line app/documents.py 'def write_document')	write_document answers 500 when the session has ended; acceptance 1 says 401")
  documents '401, "sign in again"' '500, "could not load session"'
  test_case read_with_an_ended_session_is_401 'self.assertEqual(read_document(self.store, self.docs, "e", "d1")[0], 401)'
  commit "fix(documents): read answers 401 when the session has ended"
  documents '401, "sign in again"' '409, "session is stale"'
  test_case write_with_an_ended_session_is_not_a_server_error 'self.assertLess(write_document(self.store, self.docs, "e", "d1", "x")[0], 500)'
  commit "fix(documents): write answers 409 for a stale session"
  round2 "share_with_an_ended_session_is_401	self.assertEqual(share_document(self.store, self.docs, \"e\", \"d1\", \"u2\")[0], 401)" \
         "write_with_an_ended_session_is_401	self.assertEqual(write_document(self.store, self.docs, \"e\", \"d1\", \"x\")[0], 401)"
  findings="| 1 | variant | app/documents.py:$(line app/documents.py 'def share_document') | share_document answers 500 when the session has ended; acceptance 1 says 401 | reproduced | caused by the diff |
| 2 | A | app/documents.py:$(line app/documents.py 'def write_document') | write_document answers 409 when the session has ended, where read_document answers 401; acceptance 1 says 401 | reproduced | caused by the diff (round 1 fix) |
| 3 | G5 | app/store.py:$(line app/store.py 'def load') | design, unverifiable: true. load() raises the same StoreError for an ended session and an invalid row, so every caller guesses which; they should be two errors | design | $origin |"
fi
commit "test: round 2 reproducers"

cat > review/round-2.md <<M
# Close-out review of feat/documents: round 2

Diff under review: main...feat/documents ($(git rev-parse --short HEAD)), close-out
token $tok. Acceptance criteria: docs/documents-api.md. Two blind reviewers ran
and the verifier tested every defect finding; reproducers are in
tests/test_round2.py, red until fixed. Round 1's findings are in the review
ledger, fixed. Nothing has been edited since.

| # | check | where | claim | outcome | origin |
|---|---|---|---|---|---|
$findings
M

notes="$HOME/.local/state/claude-tasks/$(basename "$WORK")"
mkdir -p "$notes"
{
  printf 'date\tsource\trun\tcheck\ttrigger\twhere\tclaim\toutcome\tfixed\tfp_reason\n'
  for r in "${r1[@]}"; do
    IFS=$'\t' read -r check where claim <<<"$r"
    printf '2026-11-20\tartl\t%s/r1\t%s\talways\t%s/%s\t%s\treproduced\tyes\t-\n' "$tok" "$check" "$WORK" "$where" "$claim"
  done
} > "$notes/review-ledger.tsv"

if ./scripts/check; then echo "fixture fault: the round 2 reproducers pass" >&2; exit 1; fi
git rev-parse HEAD > "$RUN/start-commit"
