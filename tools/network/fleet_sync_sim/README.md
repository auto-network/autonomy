# Fleet checkpoint simulation

This package proves the checkpoint synchronization design against the real
personal GraphDB schema before the production engine is introduced.

## Replication boundary

The unit is the entire logical personal graph.  The codec currently carries
the durable rows from these tables:

`attachments` (metadata), `captures`, `claims`, `derivations`, `edges`,
`entities`, `entity_mentions`, `node_refs`, `nodes`, `note_comments`,
`note_reads`, `note_versions`, `settings`, `sources`, `tags`, `thoughts`, and
`threads`.

It deliberately does not carry:

- `orgs` or `sqlite_sequence`, which are physical local bootstrap state;
- FTS virtual/shadow tables, indexes, and triggers, which are rebuilt locally;
- `file_path` values, which have meaning only on the originating machine;
- personal-root and passkey Settings rows; and
- attachment bytes inside graph-row frames.  Attachment metadata names the
  SHA-256 object; the bytes are a separately coded RaptorQ artifact and the
  target path is published only after hash/size verification and atomic rename.

`audit_schema()` fails when a new durable table lacks an explicit policy.  A
schema migration therefore cannot silently fall outside the checkpoint.

## Strict byte algorithm

All integers use minimal base-128 unsigned magnitude plus a sign byte.  Text is
UTF-8.  Bytes and containers are length framed.  Maps sort by their encoded key
bytes.  Floats are finite IEEE-754 binary64 with negative zero normalized to
positive zero.  JSON stored by SQLite is parsed into those typed values rather
than serialized as presentation text.

A mutation frame contains, in order:

1. table name;
2. logical row address;
3. non-negative nanosecond timestamp;
4. tombstone marker; and
5. sorted logical column/value pairs (empty for a tombstone).

The candidate hash is SHA-256 over the canonical table, address, tombstone, and
values, excluding the timestamp.  Stream position is `(timestamp,
candidate_hash)`.  Frames sort by that position and then by their complete
bytes; the stream is prefixed by a versioned domain magic and every frame is
length-delimited.  The decoder re-encodes the result and rejects any byte
sequence that is valid-looking but non-canonical.

That order is the mutation-envelope oracle used by this simulation.  It is
not the production base scan order.  A production compacted base is ordered
by numeric table id and canonical logical key so SQLite indexes can stream it
without a global timestamp sort.  Winner/watermark order, canonical base byte
order, and RaptorQ transport order are three separate concerns.

For one logical address, the winner is the maximum `(timestamp,
candidate_hash)`.  This timestamp LWW rule is associative, commutative, and
idempotent.  A checkpoint is fed through the same inbox as live mutations and
is always a bulk merge, never a database replacement.

## Earned watermarks and exact bases

For every active origin `p`, a published watermark `Wp` is a durable promise,
not a maximum timestamp somebody observed.  The origin serializes a cut with
its writers, persists a no-more-before floor, seals every origin mutation
through the cut, and places that sealed prefix on at least one other active
machine before publication.  Any later local write at or below the floor is
refused.  The closed semantic garbage-collection floor is:

`F = min(Wp for p in the frozen active roster)`

Timeout never changes the roster.  Enrollment or a root-authorized kick
changes the roster epoch and invalidates an in-flight base round.  A restored
machine whose floor may have rolled back cannot author under that incarnation
until it recovers the fleet-held floor or enrolls as a fresh incarnation.

State compaction and semantic garbage collection are distinct.  An exact base
folds the current table-specific join state at every peer's included cut, so
it may contain faster-peer state above `F`.  Durable installation and exact
digest acknowledgment let the represented source artifacts retire.  `F`
governs when tombstones and replay-suppression state may disappear and when
replay can be ignored.  The base certificate pins the roster epoch/hash, every
peer cut and watermark, included artifacts, codec/policy version, state bytes,
and digest; acknowledgments happen only after atomic durable installation.

## Real-schema materialization

`materialize()` applies converged winners to an actual GraphDB in dependency
order.  It derives local edge IDs, resolves Settings slots, assigns note
display-version numbers from `(created_at, content_hash)`, regenerates FTS via
the normal database triggers, and stages attachment metadata until verified
bytes exist locally.

This remains simulation code, not the production database adapter.  In
particular, a current SQLite snapshot cannot recover overwritten values or
tombstones that were never logged.  The production engine must capture each
mutation at write time and retain tombstones through the compaction frontier.

## Indexed bounded-memory base codec

`streaming.py` is the production-shape counterpart to the in-memory oracle.
It installs skinny SQLite indexes over every replicated table's logical key,
holds one WAL-consistent read snapshot, and iterates those indexes in a fixed
dependency-safe numeric table order. A keyset cursor resumes exclusively from
`(table, logical_address)` without `OFFSET` or visiting prior tables.

One canonical row frame at a time is written into immutable record-aligned
chunks. The durable catalog records the policy digest, chunk sequence, first
and last keys, sizes, record counts, SHA-256 commitments, and an ordered root.
Decoding verifies those commitments one chunk at a time and applies bounded
batches into a staging GraphDB. The staging database is published only after
the complete root is validated.

On a one-million-row, 437 MiB SQLite corpus, the 8 MiB configuration emitted
650 MiB in 82 chunks at 15.2 MiB/s with 187 MiB peak RSS. Full realization ran
at 7,570 rows/s with the same 187 MiB peak. The earlier oracle used about
2,982 MiB on a smaller 207 MiB encoded corpus. The logical-key indexes add
23.0 MiB (5.3%) to the million-row database and every query plan is an index
walk with no temporary sort.
