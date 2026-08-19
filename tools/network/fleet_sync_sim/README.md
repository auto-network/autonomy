# Fleet checkpoint simulation

This package contains the executable 1.0-alpha library and simulator for the
checkpoint synchronization design against the real personal GraphDB schema.
It is not yet installed into the dashboard's production GraphDB write paths.

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

## Transactional mutation catalog

`catalog.py` closes the snapshot-only gap. It adds five local-only tables: a
singleton containing the catalog version, machine incarnation, last authored
timestamp and no-more-before floor; normalized machine and transaction
dictionaries; one skinny current-winner/tombstone row per canonical logical
address; and a bounded unacknowledged transaction journal. The winner catalog
never duplicates live payload.

SQLite insert/update/delete triggers capture the address transition in the
same transaction as the graph write. Writes to a replicated table without an
authored transaction context fail closed; the two excluded identity Settings
remain writable outside replication. A logical-key change emits a live new
address and a tombstone for the old address. Rollback removes both effects.

An authored transaction has one monotonic scalar timestamp and stable operation
indexes. The receiver groups its records, resolves current winners, realizes
them in dependency order, and commits atomically. Store-forward retains the
original machine incarnation and transaction identity. Equal timestamps use
the canonical candidate hash; replay is inert.

On 100,000 400-byte source rows, normalization reduced current-winner tracking
allocation from 269 to 141 bytes per address. After pruning the journal and
VACUUM, the measured steady file delta was 113 bytes/address relative to the
same indexed graph. Canonical logical-key indexes remain a separate ~24
bytes/address on the million-row source corpus, for an estimated 137 MiB of
steady local overhead per million short-key addresses. Without journal
framing, tracked ingestion accepted 18,782 rows/s versus 24,804 rows/s; with
the current per-row Python compression callback it accepted 5,011 versus
24,506 rows/s. Native transaction-level framing/compression is therefore a
performance requirement for production, not a format change.

The current-winner catalog cannot replace transaction history: a hot receiver
that missed a multi-table transaction needs the original frames in their
atomic group. `fleet_sync_journal` therefore retains compressed canonical
frames only until a durably acknowledged exact base covers them. On the same
workload that transient journal costs 390 bytes per unacknowledged changed row
(39.0 MiB for 100,000 changes), then becomes reusable free SQLite space after
pruning. It is measured separately from steady catalog overhead.

The payload-free winner artifact measured 229 bytes/address. That is checkpoint
wire metadata, not another persistent payload copy: each live row's values
appear only in the canonical base, while the winner stream supplies the LWW
timestamp/provenance and tombstones needed to merge it correctly.

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

## 1.0-alpha checkpoint lifecycle

`alpha.py` composes the pieces into one lifecycle:

1. persist the no-more-before floor and establish a frozen WAL read snapshot;
2. release writers and stream the snapshot into key-ordered base chunks;
3. stream one payload-free current winner/tombstone record per logical address
   from that same cut (independent of whether older acknowledged journal
   history has retired);
4. commit every immutable chunk and strict catalog by SHA-256 root;
5. reconstruct each immutable object through real RaptorQ;
6. validate and realize the base in bounded batches into a staging GraphDB;
7. apply transaction-grouped deltas, preserving origin and tombstones;
8. retain the receiving machine's excluded identity armor; and
9. publish the completed SQLite file only after validation and fsync.

The alpha manifest commits the frozen roster epoch and hash, origin
incarnation, watermark, base root, and payload-free winner root. The winner
stream carries address, timestamp, tombstone, origin/transaction identity,
operation index, and candidate hash. The installer recomputes every live
candidate hash from the realized base and refuses a mismatch. Consequently a
full checkpoint contains each live payload once rather than duplicating the
database in a second mutation stream. Incremental hot deltas still carry
payload, because they must be applicable without a new base.

Base order, delta/winner order, and RaptorQ packet order are independent.
Decoders reject wrong versions, policies, sequences, framing, canonical bytes,
hashes, and trailing input.

The alpha is an embedded library. It adds no process, daemon, port, external
service, or dependency beyond the repository's existing SQLite, GraphDB,
RelayKit/swarmkit, and pinned `raptorq` runtime. The current installer requires
the target personal database to be offline during atomic replacement. Before
production activation, catalog installation must become a GraphDB migration
and every production personal-store writer must enter the authored transaction
adapter; installing the triggers first would intentionally reject legacy direct
writes.

`live_workload.py` runs actual concurrent SQLite writer and reader connections
while repeatedly freezing checkpoints, reconstructing every artifact through
RaptorQ, and installing them on a receiver. It asserts coherent cuts, no read
errors, dependency-safe transactions, retained tombstones, and zero final lag
after the writer stops. This is sustained-load evidence, not a pre-generated
event schedule.

The final six-second Alpha run committed 507 eight-row transactions while 900
source reads and 900 receiver reads ran concurrently. Thirteen incremental
installs bounded observed lag at 73 transactions; the final reliable delta
drained it to zero in 1.27 seconds. Both databases ended with the same 3,551
rows and no read errors. The measured authored rate was 81.7 transactions/s
(about 654 inserted rows/s, plus deletes) with the current Python trigger and
compression path.

`alpha_benchmark.py` drives the complete authored-write → frozen checkpoint →
RaptorQ reconstruction → verified staging install lifecycle with a configurable
real-schema corpus and records per-stage wall/CPU time, artifact/database size,
logical digest, and peak RSS. `chain_benchmark.py` holds the logical corpus
constant while sweeping 2/4/8/16/32/64 MiB objects and one/two/four RaptorQ
workers. On exactly 524,000,000 logical bytes (one million rows), 4 MiB was the
best measured lifecycle balance: 125 objects, 216/392/590 MiB/s at one/two/four
workers, a 297 MiB conservative four-worker RSS upper bound, and 163.65 seconds
for encode, RaptorQ, and verified realization. Two MiB improved four-worker
coding to 670 MiB/s and reduced that memory bound to 215 MiB, but doubled the
object count to 250 and slowed encoding enough to make the lifecycle 166.78
seconds. The Alpha checkpoint default is therefore 4 MiB. These are local
coding rates, not a claim about production network capacity; an actual channel
remains bounded by its transport and TURN allocation limits.

The final 100,000-row Alpha lifecycle used 400-byte payloads and the 4 MiB
default. It produced a 102,188,863-byte checkpoint from a 116,568,064-byte
tracked database: 12.93 seconds to author the corpus, 8.36 seconds to freeze
and encode the exact base plus winner metadata, 0.74 seconds to reconstruct
all immutable artifacts through RaptorQ, and 17.93 seconds to verify, realize,
fsync, and atomically publish the receiver. Peak process RSS was 218.4 MiB and
the receiver's logical digest matched the source.

## Installation and operational boundary

Alpha 1.0 is a Python library and evidence harness inside the existing
Autonomy repository. It uses only dependencies already present in the runtime:
SQLite/GraphDB, RelayKit/swarmkit, and the pinned `raptorq` package. It opens no
listener and adds no daemon, service unit, background process, account, port,
or external database.

Calling `FleetSyncAlpha` on a database currently installs the five local
tracking tables, one local catalog-order index, logical-key indexes for the 17
replicated tables, and insert/update/delete triggers on those tables. It does
not increment GraphDB's `user_version` because this package is not activated in
production. Production adoption therefore requires a real GraphDB migration,
including a one-time initial winner for every existing logical row, followed
atomically by converting every personal-store writer to the authored
transaction API. The alpha refuses to checkpoint a populated database whose
live-row count is not completely covered by its catalog. Installing the
fail-closed triggers before bootstrapping existing rows or converting all
writers would intentionally reject the migration or those legacy writes.

Checkpoint creation is online: it briefly serializes an `IMMEDIATE` cut,
persists the no-more-before floor, establishes a WAL snapshot, and releases
writers before streaming. Alpha installation is offline: it realizes into a
new staging database, preserves the receiving machine's local `orgs` bootstrap
row and excluded identity armor, validates and fsyncs the result, then replaces
the target through a recoverable backup. Production online handoff, scheduler,
fleet discovery/enrollment, RelayKit channel establishment, durable ACK
collection, and exact-base round coordination remain outside this package.

Attachment graph metadata participates in the base. Attachment bytes are
content-addressed artifacts fetched through the supplied blob-store adapter;
an install refuses publication while any referenced object is unavailable or
fails its size/SHA-256 check. FTS and other derived indexes rebuild locally.

Transaction memory is explicitly bounded: at most 16,384 operations and 128
MiB of canonical mutation frames per authored transaction. Base memory is one
record-aligned chunk plus one materialization batch; hot-delta memory is at
most one bounded transaction. RaptorQ's current Python wrapper materializes
one immutable segment at a time, never the complete personal database.
