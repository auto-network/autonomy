# Fleet synchronization

This package contains the executable 1.0-alpha synchronization engine against
the real personal GraphDB schema. Its catalogue preparation and authored-write
boundary are integrated into the production personal database stores, and the
Dashboard owns an idle authenticated peer scheduler. The unlock-time handoff
of machine identity and discovered peer addresses is the remaining activation
boundary.

## Replication boundary

The unit is the entire logical personal graph.  The codec currently carries
the durable rows from these tables:

`attachments` (metadata), `captures`, `claims`, `derivations`, `edges`,
`node_refs`, `nodes`, `note_comments`, `note_reads`, `note_versions`,
`settings`, `sources`, `tags`, `thoughts`, and `threads`; encrypted `vault_content_bodies` and `vault_content_objects`; and
the `policy_classes`, `root_anchors`, `vault_factors`, `vault_secrets`,
`keycontrol_state`, `keycontrol_credential`, and `keycontrol_bridge` records
required to open those secrets on another fleet machine.

It deliberately does not carry:

- `orgs` or `sqlite_sequence`, which are physical local bootstrap state;
- FTS virtual/shadow tables, indexes, and triggers, which are rebuilt locally;
- `file_path` values, which have meaning only on the originating machine;
- derived `vault_state_object_counts` (rebuilt exactly from object rows) or
  machine-local key-control metadata, pending queues, and usage counters; and
- attachment bytes inside graph-row frames.  Attachment metadata names the
  SHA-256 object; the bytes are a separately coded RaptorQ artifact and the
  target path is published only after hash/size verification and atomic rename.

`audit_schema()` fails when a new durable table lacks an explicit policy.  A
schema migration therefore cannot silently fall outside replication.

## Strict byte algorithm

All integers are length-framed minimal decimal text — the shipped format,
kept deliberately: candidate hashes are computed over these bytes, so a
denser integer encoding is a fleet-wide compatibility break, and profiling
shows integer encoding is not a measurable cost.  Text is UTF-8.  Bytes and
containers are length framed.  Maps sort by their encoded key bytes.  Floats
are finite IEEE-754 binary64 with negative zero normalized to positive zero.
JSON stored by SQLite is parsed into those typed values rather than
serialized as presentation text.

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

That order is the mutation-envelope oracle used by this engine. It is
not the production base scan order.  A production compacted base is ordered
by numeric table id and canonical logical key so SQLite indexes can stream it
without a global timestamp sort.  Winner/watermark order, canonical base byte
order, and RaptorQ transport order are three separate concerns.

For one logical address, the winner is the maximum `(timestamp,
candidate_hash)`.  This timestamp LWW rule is associative, commutative, and
idempotent.  A bootstrap sweep is fed through the same inbox as live
mutations and is always a bulk merge, never a database replacement.

The vault families intentionally narrow that general rule. Ciphertext bodies,
object headers, and state descriptors are immutable: a byte-identical replay
is inert and any same-key difference fails closed. Credential and bridge
records are immutable except for a local `wire = NULL` tail-body prune. A
non-NULL copy always wins over NULL regardless of timestamp, so synchronizing
with any complete peer restores a pruned body and a prune never erases a body
another peer still holds.

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
the normal database triggers, verifies embedded vault ciphertext by size and
SHA-256, installs bodies before their object headers, rebuilds per-state vault
counts, and stages attachment metadata until verified bytes exist locally.

## Transactional mutation catalog

`catalog.py` closes the snapshot-only gap. It adds four local-only tables: a
singleton containing the catalog version, machine incarnation, last authored
timestamp and no-more-before floor; normalized machine and transaction
dictionaries; and one skinny current-winner/tombstone row per canonical logical
address. The catalog never duplicates live payload.

SQLite insert/update/delete triggers capture the address transition in the
same transaction as the graph write. Writes to a replicated table without an
authored transaction context fail closed. A logical-key change emits a live new
address and a tombstone for the old address. Rollback removes both effects.

An authored transaction has one monotonic scalar timestamp and stable operation
indexes. The receiver groups its records, resolves current winners, realizes
them in dependency order, and commits atomically. Store-forward retains the
original machine incarnation and transaction identity. Equal timestamps use
the canonical candidate hash; replay is inert.

There is no separate history of wire frames. A delta is served by rebuilding
each frame from the catalog row (address, timestamp, tombstone, transaction,
operation) and the live row it points at — exactly how a swept page is built —
so any machine can serve any origin's writes from any watermark, whether it
learned them by delta or by a bootstrap sweep. A transaction none of
whose rows survive (every one overwritten later) is named to the puller with
its timestamp so the puller's watermark still passes it. Rows a receiver could
not realize yet (an attachment awaiting bytes) are forwarded from the frame the
quarantine keeps, so a relaying machine never serves a transaction short.

Until 2026-09-07 a fifth table, `fleet_sync_journal`, stored every frame at
write time "until a durably acknowledged exact base covers it". Every field
in it was derivable from the catalog and the live row, its only unique content
was superseded row versions that last-writer-wins discards on arrival, and its
recorded cost was 1,024 bytes per mutation on 400-byte rows — a tracked store
at 149–157% of a plain indexed store while any roster peer had not
acknowledged, plus a per-row frame callback on the write path. Measured on the
removal (perf suite kernel + storage, same box, 2026-09-07): storage overhead
versus a plain indexed store 157% → 17.6%; tracked ingestion 8,719 → 15,816
rows/s; kernel inserts 8,439 → 17,255 rows/s, updates 10,268 → 26,664 rows/s;
steady (post-prune) size unchanged. Historical numbers below that mention the
journal describe the removed design.

On 100,000 400-byte source rows, normalization reduced current-winner tracking
allocation from 269 to 141 bytes per address. Canonical logical-key indexes
remain a separate ~24 bytes/address on the million-row source corpus.

The payload-free winner artifact measured 229 bytes/address: wire metadata,
not another persistent payload copy. A row's values cross the wire once, while
the winner metadata supplies the LWW timestamp/provenance and tombstones
needed to merge it correctly.

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

## Bootstrap by sweep

A machine that holds no sync state bootstraps by **sweeping** the serving
store's keyspace, never by installing a copy of its database. Bulk database
snapshots ("checkpoints") are retired and deleted.

The partition is the whole design, and it rests on one value:

```
F = the serving store's per-origin frontier, read ONCE at sweep start
    SWEEP delivers every key <= F
    PULL  delivers every key >  F
```

`F` is captured before the first page is served and is **never advanced**. A
frontier read later would move the boundary and strand every key written
between the two readings. Anything landing after that read is above `F` by
construction and belongs to the joiner's ordinary delta.

The serving side emits `sweep.begin` carrying `F` exactly once, before any
page, then pages the live rows at or below it in canonical address order. The
framing is the v4 delta framing unchanged: a swept page is contiguous runs of
one `(origin, transaction)`, which is exactly what a transaction group already
is. `sweep.end` closes the `<= F` half; the `> F` half then arrives as a
normal delta.

The joiner persists `F` and its phase (`sweeping` → `pulling` → `complete`) in
`fleet_sync_bootstrap`, because `F` is not derivable from the database. Until
the phase reaches `complete` the store **must not advertise its frontier**: it
holds rows anchored to a boundary it has not finished honouring. That gate is
durable, so a crash mid-sweep resumes rather than silently claiming coverage
it lacks. A store with a bootstrap in progress also refuses to negotiate below
`SWEEP_PROTOCOL_VERSION`, since a downgrade would abandon `F` while keeping
the rows anchored to it.

Bootstrap is requested with the `bootstrap` flag and granted only to a puller
whose resume trail resolves to nothing — see `serve_bootstrap_decision`. An
empty server has nothing to deliver, so two freshly prepared machines meet
through (empty) deltas instead of seeding each other's blank databases.

## Installation and operational boundary

Alpha 1.0 is a Python library and evidence harness inside the existing
Autonomy repository. It uses only dependencies already present in the runtime:
SQLite/GraphDB, RelayKit/swarmkit, and the pinned `raptorq` package. It opens no
listener and adds no daemon, service unit, background process, account, port,
or external database.

GraphDB schema version 8 creates the scoped vault and key-control tables so a
fresh joiner has the exact durable schema before materializing
records. `GraphDB.migrate_fleet_sync_catalog(origin_incarnation)` is the
explicit production preparation step: one SQLite transaction installs the
five Alpha tracking tables, one machine-local peer-state table, catalog and
logical-key indexes, and deterministic initial winner metadata for every
existing logical row. It fails before DDL on unknown durable tables, preserves
identity exclusions, and can be repeated to cover writes made before the next
rollout when those writes add new logical rows. A durable bootstrap generation
prevents transaction/operation identity reuse across those repeats. The final
writer-conversion rollout performs its own integrity gate before activation;
an intervening delete or logical-key rewrite fails closed rather than guessing
legacy mutation metadata.

The production migration deliberately installs no capture triggers.
`GraphDB.activate_fleet_sync_writers(origin_incarnation)` repeats preparation,
takes the SQLite writer lock, rebuilds bootstrap winner metadata from the
locked current rows, runs the final live-row integrity gate, installs the
complete trigger set, and attaches automatic authorship to that connection in
one activation boundary. Triggerless authored/imported history is rejected
rather than reinterpreted as bootstrap state. Subsequent GraphDB,
`DbContentStore`, and
`KeyControlStore` connections discover the active catalog and attach the same
commit/rollback lifecycle automatically. Their application rows and compact
catalog metadata therefore commit or roll back together; caller-owned
multi-row transactions retain one transaction identity and stable operation
indexes. A raw SQLite writer or an unrecognized mutation form reaches the
trigger without context and fails closed.

Calling `FleetSyncAlpha` still uses its explicit caller-supplied authored
context. Production activation is explicit and is not run by ordinary startup.
Once active, ad-hoc `executescript` is refused because SQLite would commit it
outside the connection lifecycle; future schema upgrades require a coordinated
writer-gated path. A populated database whose live-row count is not
completely covered by its catalog cannot serve its rows: everything that
reads through the catalog would silently omit the untracked ones.

The Dashboard lifespan now owns one `DashboardFleetSyncService`. Before an
unlocked fleet runtime is supplied it is an idle task: it opens no database,
dials no address, and leaves zero-peer startup healthy. A runtime supplies the
derived machine signing key, personal-root public pin, live roster reader,
fixed local personal-database path, and discovered direct candidates. The
scheduler intersects candidates with the currently resolved roster, mutually
authenticates both machine keys over RelayKit's direct transport, and rechecks
roster authorization before every application request and streamed response.
Organization credentials and arbitrary presented roster rows are not accepted
at this boundary.

Each synchronization pass opens short worker-thread SQLite connections,
reads and releases one complete authored transaction at a time, streams it
through RelayKit's existing encrypted record layer, applies one transaction
atomically, and stores bytes, retries,
watermarks, completed pulls, and applied-transaction counts in local peer
state. The resume position is content-addressed, never a served row number:
the puller presents its per-origin watermark map — for every origin it has
learned, the newest transaction timestamp it holds — and the server serves,
per origin, every transaction above that mark, rebuilding frames from its
rows. (A request without a map is served from the newest timestamp per origin
named in its breadcrumb trail.) A serving database restored from a backup
therefore re-serves exactly what the puller's map says it lacks (deterministic
merge makes any overlap inert), and a restored PULLER recovers its own lost
writes from any peer, since its own origin is served like every other; its
rolled-back authoring floor recovers automatically through the ordinary apply
path. The stream carries
a bounded message count and digest, and refuses malformed framing, incomplete
transaction groups, an unauthorized machine, or an individual mutation that
cannot fit the channel's message bound.

`process_dashboard_sync.py` is the retained process-bound acceptance probe. It
launches independent scheduler processes and databases, records a refused dial,
crosses one graph note, restarts the receiver, crosses another, and reports the
two machine ids, byte totals, retry count, applied transactions, completed
pulls, final note-table digests, and post-shutdown connection count as JSON.

Bootstrap is online on both sides. The serving store reads its frontier and
pages live rows while writers keep running; the joiner merges each page
through the ordinary mutation inbox and commits in bounded batches. Nothing is
staged and no database file is ever replaced, so there is no swap window, no
recoverable backup, and no writer-gate ceremony to recover from. A joiner
killed mid-sweep restarts from its persisted phase and frontier. Production
supplies the browser-to-process handoff as a short-lived machine-signed idkit
delegation and uses the enrolled machine's local RelayKit route for the first
roster-authenticated pull. Exact remote ACK floors, dedicated post-enrollment
route rotation, and continuous multi-peer discovery remain outside this
package.

Attachment graph metadata participates in the base. Attachment bytes are
content-addressed artifacts fetched through the supplied blob-store adapter;
an install refuses publication while any referenced object is unavailable or
fails its size/SHA-256 check. FTS and other derived indexes rebuild locally.

Transaction memory is explicitly bounded: at most 16,384 operations and 128
MiB of canonical mutation frames per authored transaction. Base memory is one
record-aligned chunk plus one materialization batch; hot-delta memory is at
most one bounded transaction. RaptorQ's current Python wrapper materializes
one immutable segment at a time, never the complete personal database.

## Opt-in performance suite

Performance evidence lives in `tools/network/fleet_sync/perf` and runs only
when explicitly invoked — never from default sweeps, directory runs of the
engine tests, or agent-test's changed-line plan. The package contains no
pytest-collectable files by design; `tests/test_perf_isolation.py` trips if
one is added. The one documented command:

```bash
python3 -m tools.network.fleet_sync.perf run --scale quick   # smoke, ~15 s
python3 -m tools.network.fleet_sync.perf run                 # full baselines
```

It runs two benchmarks — `kernel` (insert/update throughput with the
served-ack floor active, steady tracking overhead)
and the `crsqlite` yardstick (auto-skipped unless
the loadable extension is present; set `FLEET_SYNC_CRSQLITE_EXT` or place it
under `<baseline store>/crsqlite/`) — then prints a direction-aware
comparison against the retained baseline for that scale and promotes the new
result. Baselines persist in `/opt/autonomy-developer/fleet-sync-perf` (or
`FLEET_SYNC_PERF_BASELINES`), one per scale, with full history under
`runs/`. Regressions past 10% are flagged in the table but never gate;
the exit status reflects benchmark errors only. `--only NAME` runs a
subset without touching the baseline, `--output PATH` writes the raw
result JSON, and the `compare A.json B.json` subcommand diffs any two
retained results offline.

## Stream liveness policy

Liveness on the direct pull channel is layered, and each layer owns one
failure class (decided in auto-fzy8s; defect and boundary both proven by
experiment on 2026-09-03):

- **Dead and frozen peers** belong to the transport. Both direct-channel
  endpoints pin websocket `ping_interval=20, ping_timeout=20`; a peer that
  stops answering pongs — killed, SIGSTOPped, or event-loop-starved —
  breaks the socket and unblocks a pending receive in
  `ping_interval + ping_timeout + close_timeout` (measured 50.0 s).
- **Wedged-but-responsive serves** — a stream that stops producing frames
  while its event loop keeps answering pongs — belong to the client's
  silence bounds in `bounded_stream_frames`, configured on
  `FleetSyncRuntimeConfig`. The first frame of a pull gets
  `pull_first_frame_allowance_s` (default 900 s): a bootstrap serve can be
  legitimately silent while it reads the frontier and assembles its first
  page, so the default is generous. Every later gap is structurally one
  bounded DB query or one ≤4 MB file read and gets
  `pull_stream_silence_limit_s` (default 60 s, just above the transport's
  50 s so a dead transport still surfaces as the more diagnostic
  `ConnectionClosed`). The blob drain uses the inter-frame bound in both
  positions — blob serves have no build phase.

A silence timeout raises `FleetSyncFirstFrameSilence` or
`FleetSyncStreamSilence`; the type name is the recorded error code, so a
wedged peer is named in peer state and telemetry instead of sitting at
`online=1` with zero deltas forever (the pre-policy failure fingerprint).
Silence never triggers the protocol-version downgrade — it is a liveness
verdict, not a version one.

Clients tolerate (ignore, outside digest and count) a
`{"kind": "keepalive"}` control frame that no server emits yet: a future
protocol revision may keep long build phases live with it, at which point
the first-frame allowance can tighten without a mixed-fleet compatibility
window.
