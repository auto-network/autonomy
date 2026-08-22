# Official accounting primitives

`tools.network.accounting` is the provider- and service-neutral physical usage
boundary for auto.network. It does not calculate allowances, prices, margin,
contribution credit, balances, or charges.

## V1 batch

A `UsageBatch` contains one Ed25519 producer's counters for one canonical
organization UUID and one epoch-aligned five-minute interval. The counter map is
wide and contains integer physical units such as:

- `relay.egress_bytes`
- `turn.egress_bytes`
- `mailbox.stored_byte_seconds`
- `artifact_cache.egress_bytes`
- `shared_volume.stored_byte_seconds`

Producer adapters freeze the exact names they emit. Counter names never encode
an object, link, member, session, token, source, allocation, or path.

The `batch_id` derives only from version, producer key, organization, sequence,
and interval. The checksum covers that identity plus creation time and counters.
Therefore an identical retry is one logical batch, while the same identity with
different content has the same ID and a different checksum and must be rejected
as a conflict by the sink.

The producer's public key is its stable ID and verifies the batch signature.
The sink must additionally authorize that key for the organization and counter
families; a valid signature alone is not authorization.

## Producer rule

Create a batch once, append `batch.to_json()` to the crash-safe spool, and retry
those exact bytes until the sink acknowledges the exact `(batch_id, checksum)`.
Never recreate a retry with a new `created_at`.

Accounting performs no remote I/O on a relay, TURN, or storage data path.
Prometheus may report spool and sink health but is never official usage.

## Durable spool

`UsageSpool` is the service-neutral, single-process durable handoff. It uses
full-synchronous SQLite transactions and an exclusive process lock. Append
validates and commits the exact canonical bytes before returning. Per
producer/organization sequence continuity rejects gaps and stale replay.

Delivery reads records in durable append order and may remove bytes only after
an acknowledgement names the exact batch ID and checksum. Acknowledgement
retains the exact bytes because ingest success alone does not prove that an
off-host backup contains them. `prune_acked(...)` accepts one
producer/organization sequence watermark that the caller knows is covered by
the sink's recovery point; it never compares clocks. Pruning retains the local
stream high-water so old batches cannot be reintroduced. Record and
retained-byte limits apply backpressure without deleting official usage.
`health()` exposes pending and acknowledged counts/bytes, total retained bytes,
interval age bounds, and stream count. Normal delivery reads `pending()`;
clean-sink recovery reads `recoverable()` so acknowledged-but-not-yet-pruned
bytes can be replayed idempotently.

## Authoritative ledger

`UsageLedger` is the selected v1 single-writer SQLite ingest boundary. A
control-plane authorization binds one Ed25519 producer key to one canonical
organization and a fixed set of counter families. A service producer may hold
separate narrow bindings for several organizations. Ingest verifies the batch
before its exact producer/organization binding, commits new bytes with
`synchronous=FULL`, returns the exact ID/checksum receipt, and treats an
identical retry as unchanged.
Same-ID mutations and reuse of a producer sequence for another interval fail
closed.

Out-of-order batches are accepted because reconnects and recovery replays can
reorder delivery. Per-stream reconciliation reports the highest contiguous
sequence, highest observed sequence, and exact missing ranges. Public traffic
never reads this ledger. Sink topology and measured migration gates are frozen
in `graph://d1a27da5-679`.

Idle intervals emit no zero-valued usage batch. A signed `UsageProgress`
therefore advances one producer/organization stream's closed interval and last
usage sequence after its spool drains. The ledger settles an organization only
through the minimum progress of every enabled binding. Usage at or before an
accepted closure is rejected, so delayed delivery cannot silently rewrite a
settled or billed interval. Progress carries no member, session, token, address,
or content identity.

Decision: `graph://31ab60ae-647`.
