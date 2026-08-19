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

For one logical address, the winner is the maximum `(timestamp,
candidate_hash)`.  This timestamp LWW rule is associative, commutative, and
idempotent.  A checkpoint is fed through the same inbox as live mutations and
is always a bulk merge, never a database replacement.

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
