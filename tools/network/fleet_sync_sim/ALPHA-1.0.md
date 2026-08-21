# Personal fleet synchronization 1.0 alpha

## Release claim

This alpha is an executable, bounded-memory synchronization engine over the
complete logical personal GraphDB schema. Production personal stores prepare
its catalogue and can explicitly activate the authored-write boundary; the
Dashboard owns its idle-safe authenticated delta scheduler. Checkpoint
publication remains an offline acceptance path rather than a live Dashboard
handoff.

The exercised lifecycle is:

1. attribute graph writes to a monotonic machine transaction;
2. capture current winners, tombstones, and unacknowledged transaction frames
   atomically with those writes;
3. freeze one coherent WAL cut and persistent no-more-before floor;
4. stream the logical graph directly from SQLite indexes into immutable,
   record-aligned base objects without a global Python list or external sort;
5. stream payload-free winner/tombstone metadata from the same cut;
6. reconstruct immutable objects through real RaptorQ;
7. verify and realize the base incrementally into a staging GraphDB;
8. verify every winner candidate against that realized state;
9. preserve receiver-local identity/bootstrap state; and
10. publish the database atomically, or retain the old database on failure.

Between bases, bounded transaction deltas stream over the existing reliable
authenticated channel. A receiver retains origin identity when forwarding a
mutation. Replays are inert under timestamp LWW and the canonical candidate
hash tie-break.

## Correctness boundaries

- The active roster epoch and hash are committed by every full checkpoint.
- The scalar compaction frontier is the minimum *earned* watermark of the
  frozen active roster. An earned watermark requires an atomic writer cut, a
  persistent no-more-before floor, a sealed prefix, and a second durable
  holder; an observed timestamp is not a watermark.
- Exact state compaction and semantic garbage collection are separate. Exact
  base ACKs retire represented source artifacts; the minimum earned watermark
  controls tombstone/suppression deletion and replay refusal.
- A root-authorized kick changes the active roster epoch. It can release a
  stale peer's hold on the minimum only after the remaining active peers have
  incorporated the kick.
- Base order, winner order, and RaptorQ packet order are independent. Changing
  one cannot silently redefine either of the others.
- The two identity-armor Settings sets never enter replication. Fleet roster
  rows remain ordinary raw personal graph data and do replicate.
- Attachment metadata is graph state; bytes are separately content addressed
  and must pass size and SHA-256 verification before database publication.
- Vault ciphertext, object headers, and key-control records are embedded
  immutable graph state. Same-key differences fail closed; nullable local
  key-control body prunes never override a complete peer copy. Derived vault
  counts and machine-local key-control queues do not replicate.

## Resource bounds

- Base encoding holds one SQLite row and one configured immutable segment.
- Base realization holds one configured batch and one segment.
- Winner installation streams metadata; it does not collect the winner set.
- Hot-delta application holds one authored transaction, capped at 16,384
  operations and 128 MiB of canonical frames.
- The current Python RaptorQ adapter holds one segment. It never materializes
  the full database.
- The measured/default immutable segment is 4 MiB. On the fixed 524,000,000
  byte corpus it balanced 125 objects, 590 MiB/s four-worker coding, a 297 MiB
  conservative pool-memory bound, and the lowest measured lifecycle time.
- The steady catalog contains no live payload. The compressed journal is
  temporary payload history retained only until a covering exact base is
  durably acknowledged.

## External deep-review disposition

The alpha was attacked against `graph://b298d988-d49` and its compaction
addendum `graph://53828897-327`.

Adopted now:

- atomic watermark cuts and durable no-more-before floors;
- separate winner/watermark, canonical-base, and transport orderings;
- a skinny current-winner/tombstone catalogue;
- bounded unacknowledged transaction history;
- direct indexed SQLite streaming and keyset resume;
- roster-epoch/hash-pinned artifacts;
- durable staging, integrity validation, recoverable publication;
- exact-base versus semantic-GC separation; and
- reliable-channel hot deltas instead of fountain-coding tiny changes.

Deliberately deferred to production/native work:

- a generated Rust codec and schema bindings;
- native systematic-first RaptorQ with arbitrary repair ESI ranges;
- record/key-aware stable chunk boundaries;
- compression selection (including zstd) from a whole-chain benchmark;
- remote checkpoint offer and address negotiation;
- durable ACK exchange and RelayKit address discovery; and
- a startup write gate after an externally restored machine snapshot. The
  alpha persists and enforces its floor across ordinary restarts, but only the
  production lifecycle can distinguish a restored old image from a normal
  restart and recover the fleet-held floor or rotate the incarnation.

Not adopted:

- per-row cryptographic signatures, organization-ledger semantics, or causal
  version vectors. The operator's bounded personal fleet trusts a
  roster-authorized, proof-of-possession-authenticated peer at the application
  boundary. These mechanisms add a different threat model without improving
  the chosen one.

## Production activation requirements

GraphDB schema version 8 supplies the vault/key-control receiver tables but
does not activate fleet synchronization. The production preparation migration
now installs the five tracking tables, one local peer-state table,
logical-key/catalog indexes, and deterministic initial winner metadata for
every existing logical row in one rollback-safe transaction. It leaves capture
triggers disabled. The explicit writer activation gate now rechecks complete
coverage and rebuilds bootstrap provenance under the SQLite writer lock,
installs all rejecting triggers, and binds GraphDB, encrypted-content, and
key-control connection commits to automatic authored transactions. Raw or
unrecognized writes fail closed, and ordinary startup never activates a
prepared store implicitly. The Dashboard now owns an idle-safe scheduler that
uses the active personal-root roster, mutual machine-key proof, RelayKit direct
channels, transaction-atomic journal replay, bounded retry, and durable local
peer counters. The Dashboard service can now stop its scheduler, close its
pooled personal store, prove that no production writer remains, merge a
received checkpoint with locally authored winners in staging, publish it
recoverably, record a durable local install receipt, and resume delta pulls.
Remaining work is the unlock-to-runtime machine-key handoff, address and
checkpoint-offer discovery, exact remote ACK floors, and attachment-object
transport. No new daemon or network service is required by the engine itself.
