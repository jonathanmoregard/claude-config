---
name: gdpr-crypto-shred
description: Use when working on the database of any system with external users — schema design, migrations, new tables/fields, logging, backups — or when a task mentions GDPR, PII, erasure, right-to-be-forgotten, or data retention. Applies BEFORE the first migration that introduces a PII field, not as a retrofit.
---

# GDPR Crypto-Shred Design Rules

Retrofitting erasure is the expensive path: a PII field shipped plaintext
later needs a migration + crypto refactor to become backup-proof, and every
retrofit re-discovers the same gotchas. Apply these rules on the FIRST
migration that introduces the PII and the followups never exist.

## The one-line heuristic

> If a field holds PII: on the **first** migration that introduces it, it
> ships **encrypted-at-rest under a per-subject DEK**, **+ a blind index if
> it's ever looked up**, its subject **shreds atomically on erasure**, and
> it's covered by **replay-on-restore + retention**. Append-only logs key
> on an **opaque surrogate**, never the PII.

## Crypto-shred mechanics

- **Envelope encryption, per subject.** Each erasable subject gets its own
  **DEK**, wrapped by a **KEK** living outside the DB and backups (secrets
  manager / agenix). Store only wrapped DEK + ciphertext. **Erasure = null
  the wrapped DEK (tombstone)** → data is crypto-irrecoverable even from
  backups; a stolen backup is ciphertext-only.
- **Tombstones NEVER regenerate.** `get-or-create` must observe the
  tombstone and fail closed (e.g. `INSERT … ON CONFLICT DO UPDATE … WHERE
  shredded_at IS NULL RETURNING`) — never resurrect an erased subject.
- **Fail closed, always.** Missing/shredded DEK → skip/refuse, never
  plaintext fallback. **AAD-bind** every ciphertext to
  `entity:subject_id[:field]` (distinct per field) so ciphertexts can't be
  swapped between rows/fields. WARN loudly (don't silently blank) on a
  present-key AEAD failure — that's an alertable regression.
- **Blind index for any encrypted field that's also a lookup/dedup key**
  (random-nonce crypto breaks equality). Deterministic KEK-keyed HKDF.
  **Scope by checking the lookup callsites — never assume:**
  - *entity-scoped* when dedup lives within one parent record;
  - *GLOBAL* when the lookup spans the dataset (erasure-by-email must find
    ALL records; transient auth rows looked up by email).

## Design-for-erasure rules (first migration, every time)

1. Every PII field encrypted under a per-subject DEK from day one — lookup
   keys included (blind index up front).
2. Append-only/event logs key on an **opaque surrogate** — a log storing
   raw PII is itself un-erasable PII.
3. **Erasure atomic AND concurrency-safe**: delete/null + DEK shred in the
   SAME transaction; the write path observes the tombstone under the row
   lock (else a concurrent write resurrects the subject). Design erase and
   write paths together; pin transaction isolation explicitly.
4. **Replay-on-restore from the start**: keep an erasure log; a DB restore
   re-applies logged erasures, or restoring un-erases people.
5. **Retention reaping on every PII/audit table** from day one (auth
   events, attempt logs, tokens) — Art. 5(1)(e).
6. **Lawful basis per PII class, but encrypt all of it.** Pre-signup
   transient PII (no subject yet) → service-level DEK + global blind;
   erase by blind match.
7. **Shared vs personal, decided early**: content authored into a group
   artifact survives erasure; the individual's identifying fields are the
   target. Don't erase shared content.

## Accepted residuals (document in the DPIA, don't over-engineer)

- A deterministic blind is a **confirmation oracle** for a KEK holder with
  a pre-erasure backup. Global blinds: mitigation is operational (shorter
  backup retention). Entity-scoped blinds: a per-scope blind subkey
  shredded on last-erasure-within-scope partially closes it.
- **Encryption-at-rest defends backups/at-rest only** — never a live
  compromise holding app + KEK. Don't oversell it.

## Red flags

- A `*_events`/audit table with an email/name column
- "We'll add encryption later" on a PII column
- An erase path written without reading the write path (resurrection race)
- "No blind index needed" asserted without listing the lookup callsites —
  this exact claim was wrong every time it was made
- Backup/restore runbook with no erasure-replay step
