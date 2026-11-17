#!/usr/bin/env bash
# Shared fixture for the review-v2 scenarios: a small document service in plain
# Node (no build, no dependencies), with a finished branch that adds download
# links. Usage: docvault.sh WORK [plain|owner-and]
#
# Planted, for the graders only (nothing in the fixture's own text hints at
# either):
#   plain      download() never compares the link's document with the one
#              asked for: a link issued for one document opens any other (D7).
#   owner-and  download() refuses only when BOTH the document and the owner
#              differ, so the obvious reproducer (another owner's document) is
#              refused, while a link opens every other document of the same
#              owner.
# In both: renameDocument and setStatus each read a row, await, and write the
# whole row back -- two writers to one row, with no test that runs them
# together and no helper that could (the sweep Races lens's target).
#
# Also seeds the sandbox HOME's review ledger for this repo (notes dir name =
# the working directory's basename) with four empty D5 runs, so one more
# empty D5 run crosses ledger.md's drop threshold.
set -euo pipefail
WORK="${1:?work dir}"
VARIANT="${2:-plain}"

git -C "$WORK" init -q -b main
git -C "$WORK" config user.email e2e@example.invalid
git -C "$WORK" config user.name e2e
mkdir -p "$WORK/src" "$WORK/test" "$WORK/scripts"

# ---------------------------------------------------------------- base commit
cat > "$WORK/package.json" <<'J'
{ "name": "docvault", "private": true, "type": "commonjs",
  "scripts": { "test": "node --test" } }
J

cat > "$WORK/scripts/check" <<'S'
#!/usr/bin/env bash
# Project gate: syntax check of every module, then the test suite.
set -euo pipefail
cd "$(dirname "$0")/.."
for f in src/*.js test/*.js; do node --check "$f"; done
node --test
echo "gate: green"
S
chmod +x "$WORK/scripts/check"

cat > "$WORK/src/store.js" <<'J'
'use strict';
// In-memory table store. Every call yields once, like a database round-trip.

const tick = () => new Promise((resolve) => setImmediate(resolve));

class Store {
  constructor() {
    this.tables = new Map();
  }

  table(name) {
    if (!this.tables.has(name)) this.tables.set(name, new Map());
    return this.tables.get(name);
  }

  async get(name, id) {
    await tick();
    const row = this.table(name).get(id);
    return row ? { ...row } : null;
  }

  async put(name, id, row) {
    await tick();
    this.table(name).set(id, { ...row });
  }

  async remove(name, id) {
    await tick();
    this.table(name).delete(id);
  }
}

module.exports = { Store, tick };
J

cat > "$WORK/src/documents.js" <<'J'
'use strict';
const crypto = require('node:crypto');

const STATUSES = ['draft', 'published', 'archived'];

async function createDocument(store, ownerId, title, body) {
  const id = crypto.randomUUID();
  const doc = { id, ownerId, title, body, status: 'draft', version: 1 };
  await store.put('documents', id, doc);
  return doc;
}

async function getDocument(store, id) {
  return store.get('documents', id);
}

async function renameDocument(store, id, title) {
  const doc = await store.get('documents', id);
  if (!doc) throw new Error('not found');
  await store.put('documents', id, { ...doc, title, version: doc.version + 1 });
}

async function setStatus(store, id, status) {
  if (!STATUSES.includes(status)) throw new Error('bad status');
  const doc = await store.get('documents', id);
  if (!doc) throw new Error('not found');
  await store.put('documents', id, { ...doc, status, version: doc.version + 1 });
}

module.exports = { createDocument, getDocument, renameDocument, setStatus, STATUSES };
J

cat > "$WORK/test/documents.test.js" <<'J'
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { Store } = require('../src/store');
const docs = require('../src/documents');

test('create then get returns the document', async () => {
  const store = new Store();
  const doc = await docs.createDocument(store, 'alice', 'Plan', 'text');
  assert.deepStrictEqual(await docs.getDocument(store, doc.id), doc);
});

test('rename bumps the version', async () => {
  const store = new Store();
  const doc = await docs.createDocument(store, 'alice', 'Plan', 'text');
  await docs.renameDocument(store, doc.id, 'Plan v2');
  const got = await docs.getDocument(store, doc.id);
  assert.strictEqual(got.title, 'Plan v2');
  assert.strictEqual(got.version, 2);
});

test('setStatus refuses an unknown status', async () => {
  const store = new Store();
  const doc = await docs.createDocument(store, 'alice', 'Plan', 'text');
  await assert.rejects(docs.setStatus(store, doc.id, 'gone'), /bad status/);
});
J

cat > "$WORK/README.md" <<'M'
# docvault

Document service. Gate: `scripts/check`.
M

git -C "$WORK" add -A
git -C "$WORK" commit -qm "init: document store"

# --------------------------------------------- the change under review
git -C "$WORK" switch -q -c feature/download-links

if [ "$VARIANT" = owner-and ]; then
  SCOPE_CHECK='  const doc = await store.get('"'"'documents'"'"', docId);
  if (!doc) return { status: 404 };
  if (link.docId !== docId && link.ownerId !== doc.ownerId) return { status: 403 };'
else
  SCOPE_CHECK='  const doc = await store.get('"'"'documents'"'"', docId);
  if (!doc) return { status: 404 };'
fi

cat > "$WORK/src/downloads.js" <<J
'use strict';
const crypto = require('node:crypto');

const LINK_TTL_MS = 15 * 60 * 1000;

async function issueDownloadLink(store, userId, docId, now = Date.now()) {
  const doc = await store.get('documents', docId);
  if (!doc) return { status: 404 };
  if (doc.ownerId !== userId) return { status: 403 };
  const token = crypto.randomBytes(24).toString('base64url');
  await store.put('links', token, {
    docId,
    ownerId: doc.ownerId,
    expiresAt: now + LINK_TTL_MS,
  });
  return { status: 201, token, url: \`/download/\${docId}?token=\${token}\` };
}

async function download(store, docId, token, now = Date.now()) {
  if (typeof token !== 'string' || token.length === 0) return { status: 401 };
  const link = await store.get('links', token);
  if (!link) return { status: 401 };
  if (link.expiresAt <= now) {
    await store.remove('links', token);
    return { status: 410 };
  }
$SCOPE_CHECK
  return { status: 200, title: doc.title, body: doc.body };
}

async function revokeDownloadLink(store, userId, token) {
  const link = await store.get('links', token);
  if (!link || link.ownerId !== userId) return { status: 404 };
  await store.remove('links', token);
  return { status: 204 };
}

module.exports = { issueDownloadLink, download, revokeDownloadLink, LINK_TTL_MS };
J

cat > "$WORK/src/http.js" <<'J'
'use strict';
const { issueDownloadLink, download, revokeDownloadLink } = require('./downloads');

// Minimal router: (method, path, query, userId) -> { status, ... }.
async function route(store, { method, path, query = {}, userId = null }) {
  let m = path.match(/^\/documents\/([^/]+)\/links$/);
  if (m && method === 'POST') {
    if (!userId) return { status: 401 };
    return issueDownloadLink(store, userId, m[1]);
  }
  m = path.match(/^\/download\/([^/]+)$/);
  if (m && method === 'GET') return download(store, m[1], query.token);
  m = path.match(/^\/links\/([^/]+)$/);
  if (m && method === 'DELETE') {
    if (!userId) return { status: 401 };
    return revokeDownloadLink(store, userId, m[1]);
  }
  return { status: 404 };
}

module.exports = { route };
J

cat > "$WORK/test/downloads.test.js" <<'J'
'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { Store } = require('../src/store');
const { createDocument } = require('../src/documents');
const { route } = require('../src/http');
const { download, LINK_TTL_MS } = require('../src/downloads');

async function fixture() {
  const store = new Store();
  const doc = await createDocument(store, 'alice', 'Plan', 'secret plan');
  return { store, doc };
}

test('owner gets a link that downloads the document', async () => {
  const { store, doc } = await fixture();
  const link = await route(store, { method: 'POST', path: `/documents/${doc.id}/links`, userId: 'alice' });
  assert.strictEqual(link.status, 201);
  const got = await route(store, { method: 'GET', path: `/download/${doc.id}`, query: { token: link.token } });
  assert.strictEqual(got.status, 200);
  assert.strictEqual(got.body, 'secret plan');
});

test('someone else cannot issue a link', async () => {
  const { store, doc } = await fixture();
  const link = await route(store, { method: 'POST', path: `/documents/${doc.id}/links`, userId: 'mallory' });
  assert.strictEqual(link.status, 403);
});

test('a missing or unknown token is refused', async () => {
  const { store, doc } = await fixture();
  assert.strictEqual((await download(store, doc.id, undefined)).status, 401);
  assert.strictEqual((await download(store, doc.id, 'nope')).status, 401);
});

test('an expired link is gone', async () => {
  const { store, doc } = await fixture();
  const link = await route(store, { method: 'POST', path: `/documents/${doc.id}/links`, userId: 'alice' });
  const later = Date.now() + LINK_TTL_MS + 1;
  assert.strictEqual((await download(store, doc.id, link.token, later)).status, 410);
});

test('a revoked link stops working', async () => {
  const { store, doc } = await fixture();
  const link = await route(store, { method: 'POST', path: `/documents/${doc.id}/links`, userId: 'alice' });
  assert.strictEqual((await route(store, { method: 'DELETE', path: `/links/${link.token}`, userId: 'alice' })).status, 204);
  assert.strictEqual((await download(store, doc.id, link.token)).status, 401);
});
J

git -C "$WORK" add -A
git -C "$WORK" commit -qm "feat: shareable download links for documents"

# The gate the prompts call green really is green.
( cd "$WORK" && ./scripts/check ) > "${E2E_RUN:-$WORK}/gate-at-setup.log" 2>&1

# ------------------------------------------------- seeded review ledger
notes="$HOME/.local/state/claude-tasks/$(basename "$WORK")"
mkdir -p "$notes"
{
  printf 'date\tsource\trun\tcheck\ttrigger\twhere\tclaim\toutcome\tfixed\tfp_reason\n'
  for r in 1 2 3 4; do
    printf '2026-11-0%s\tartl\t0f0f0f%s/r1\tD5\talways\t-\tnothing found\tnone\t-\t-\n' "$r" "$r"
  done
} > "$notes/review-ledger.tsv"
