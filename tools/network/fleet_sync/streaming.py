"""Indexed, bounded-memory canonical-base projection for fleet synchronization.

This is intentionally a second format beside the mutation-stream oracle.
Mutation streams are ordered by winner timestamp; compacted bases are ordered
by table and logical key so SQLite can provide the order directly.  The
encoder writes record-framed immutable chunks without a whole-database Python
list, global external sort, or whole-artifact byte string.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import struct
from typing import Iterator

from .codec import (
    MAX_FRAME_BYTES,
    Mutation,
    decode_mutation_frame,
    encode_mutation_frame,
    encode_value,
)
from .policies import PolicyKind, TABLE_POLICIES, TablePolicy, audit_schema
from .snapshot import _logical_address, _logical_values, _row_timestamp
from .materialize import (
    ContentAddressedBlobStore,
    MaterializationReport,
    materialize,
)


BASE_CHUNK_MAGIC = b"AUTONOMY-PERSONAL-BASE-CHUNK\x00v1\n"
CATALOG_VERSION = 1
_U32 = struct.Struct(">I")

# Dependency-safe numeric table order for canonical bases.  The number is the
# tuple position; production code generation can freeze those IDs explicitly.
BASE_TABLE_ORDER = (
    "sources", "entities", "nodes", "tags", "threads",
    "root_anchors", "vault_factors", "policy_classes", "vault_secrets",
    "vault_content_bodies", "keycontrol_state", "keycontrol_grant",
    "keycontrol_credential",
    "keycontrol_bridge", "vault_content_objects", "settings",
    "thoughts", "derivations", "claims", "edges", "entity_mentions",
    "node_refs", "note_comments", "note_reads", "captures", "attachments",
    "note_versions",
)


def _audit_base_order() -> None:
    expected = {
        table for table, policy in TABLE_POLICIES.items()
        if policy.kind not in {PolicyKind.LOCAL, PolicyKind.DERIVED}
    }
    actual = set(BASE_TABLE_ORDER)
    if actual != expected or len(BASE_TABLE_ORDER) != len(actual):
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise StreamingCodecError(
            f"canonical base order mismatch; missing={missing}, extra={extra}"
        )


class StreamingCodecError(ValueError):
    pass


def _sha256_text(value: object) -> str:
    if not isinstance(value, str):
        raise StreamingCodecError("note version content must be text")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def register_streaming_functions(conn: sqlite3.Connection) -> None:
    conn.create_function("fleet_sha256_text", 1, _sha256_text, deterministic=True)


def _role_expression() -> str:
    return (
        "CASE WHEN supersedes IS NOT NULL THEN "
        "'supersedes:' || supersedes || ':' || id "
        "WHEN excludes IS NOT NULL THEN 'excludes:' || excludes || ':' || id "
        "ELSE 'base' END"
    )


def _key_expressions(policy: TablePolicy) -> tuple[str, ...]:
    if policy.table == "note_versions":
        return ('"source_id"', '"created_at"', 'fleet_sha256_text("content")')
    if policy.table == "settings":
        return (
            '"set_id"', '"schema_revision"', '"key"',
            '"publication_state"', _role_expression(),
        )
    return tuple(f'"{column}"' for column in policy.key)


def _index_name(table: str) -> str:
    return f"idx_fleet_sync_{table}_logical_key"


def ensure_streaming_indexes(
    conn: sqlite3.Connection, *, manage_transaction: bool = True,
    audit: bool = True,
) -> dict[str, str]:
    """Install only skinny logical-key indexes and return their SQL.

    ``manage_transaction=False`` lets the production catalog migration keep
    the indexes, tracking tables, and initial winner bootstrap inside one
    caller-owned SQLite transaction.  The default retains the standalone
    Alpha helper's commit/rollback behavior.
    """
    register_streaming_functions(conn)
    if audit:
        audit_schema(conn)
    _audit_base_order()
    created: dict[str, str] = {}

    def create() -> None:
        for table in sorted(TABLE_POLICIES):
            policy = TABLE_POLICIES[table]
            if policy.kind in {PolicyKind.LOCAL, PolicyKind.DERIVED}:
                continue
            expressions = ",".join(_key_expressions(policy))
            name = _index_name(table)
            sql = f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}"({expressions})'
            conn.execute(sql)
            created[table] = sql

    if manage_transaction:
        with conn:
            create()
    else:
        create()
    return created


def indexed_query_plan(conn: sqlite3.Connection, table: str) -> str:
    policy = TABLE_POLICIES[table]
    expressions = ",".join(_key_expressions(policy))
    rows = conn.execute(
        f'EXPLAIN QUERY PLAN SELECT * FROM "{table}" ORDER BY {expressions}'
    ).fetchall()
    return " | ".join(str(row[3]) for row in rows)


def iter_indexed_snapshot_mutations(
    conn: sqlite3.Connection,
    *,
    start_after: tuple[str, tuple[object, ...]] | None = None,
    audit: bool = True,
) -> Iterator[Mutation]:
    """Yield a WAL-snapshot base in table/key order using keyset seeks.

    ``start_after`` is an exclusive ``(table, logical_address)`` cursor.  It
    provides direct resume without OFFSET or replaying prior tables.
    """
    register_streaming_functions(conn)
    if audit:
        audit_schema(conn)
    _audit_base_order()
    conn.row_factory = sqlite3.Row
    start_table = start_after[0] if start_after else None
    start_address = start_after[1] if start_after else ()
    table_ranks = {table: rank for rank, table in enumerate(BASE_TABLE_ORDER)}
    if start_table is not None and start_table not in table_ranks:
        raise StreamingCodecError("resume table is not in canonical base order")
    start_rank = table_ranks[start_table] if start_table is not None else -1
    for table in BASE_TABLE_ORDER:
        policy = TABLE_POLICIES[table]
        if policy.kind in {PolicyKind.LOCAL, PolicyKind.DERIVED}:
            continue
        if table_ranks[table] < start_rank:
            continue
        expressions = _key_expressions(policy)
        where = ""
        params: tuple[object, ...] = ()
        if table == "settings":
            # A logical setting has exactly one live base value per natural key
            # but may carry many override/exclusion patches. Base rows collapse
            # to a single address (role 'base', no id), and the platform keeps
            # that invariant by DEPRECATING all but the newest live base row per
            # key (GraphDB open heal + the partial unique indexes, both gated on
            # ``deprecated = 0``). A store that accumulated duplicate base rows
            # therefore retains the superseded ones as deprecated history -- and
            # the snapshot must skip them, or it streams several records for one
            # catalog address and breaks the checkpoint's row-count invariant
            # against the catalog's ON CONFLICT(address) collapse. Skip
            # deprecated BASE rows only (the winner is the sole ``deprecated=0``
            # base row); override/exclusion rows keep their own per-id addresses
            # and all stream, matching the catalog exactly.
            where = (
                ' WHERE (supersedes IS NOT NULL OR excludes IS NOT NULL'
                ' OR deprecated = 0)'
            )
        if start_table == table:
            comparison = f"({','.join(expressions)}) > ({','.join('?' for _ in expressions)})"
            where += (" AND " if where else " WHERE ") + comparison
            params += tuple(start_address)
        query = (
            f'SELECT * FROM "{table}"{where} '
            f'ORDER BY {",".join(expressions)}'
        )
        # Iterating the cursor makes SQLite's page cache the only row buffer;
        # there is deliberately no fetchall here.
        for raw in conn.execute(query, params):
            row = dict(raw)
            yield Mutation(
                table=table,
                address=_logical_address(policy, row),
                timestamp_ns=_row_timestamp(policy, row),
                tombstone=False,
                values=_logical_values(policy, row),
            )


def policy_digest() -> str:
    rows = []
    for name in sorted(TABLE_POLICIES):
        policy = TABLE_POLICIES[name]
        row = asdict(policy)
        row["kind"] = policy.kind.value
        rows.append(row)
    body = json.dumps({
        "base_table_order": list(BASE_TABLE_ORDER), "policies": rows,
    }, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


@dataclass(frozen=True)
class ChunkEntry:
    sequence: int
    filename: str
    records: int
    bytes: int
    sha256: str
    first_key: str
    last_key: str


@dataclass(frozen=True)
class BaseCatalog:
    version: int
    policy_digest: str
    target_chunk_bytes: int
    total_records: int
    total_bytes: int
    root_sha256: str
    chunks: tuple[ChunkEntry, ...]


class _ChunkWriter:
    def __init__(self, directory: Path, sequence: int, digest_hex: str) -> None:
        self.directory = directory
        self.sequence = sequence
        self.digest_hex = digest_hex
        self.temporary = directory / f".{sequence:08d}.tmp"
        self.handle = self.temporary.open("w+b")
        self.handle.write(BASE_CHUNK_MAGIC)
        self.handle.write(bytes.fromhex(digest_hex))
        self.handle.write(_U32.pack(sequence))
        self.count_offset = self.handle.tell()
        self.handle.write(_U32.pack(0))
        self.records = 0
        self.first_key = ""
        self.last_key = ""

    @property
    def size(self) -> int:
        return self.handle.tell()

    def append(self, mutation: Mutation, frame: bytes) -> None:
        key = encode_value([mutation.table, list(mutation.address)]).hex()
        if not self.first_key:
            self.first_key = key
        self.last_key = key
        self.handle.write(_U32.pack(len(frame)))
        self.handle.write(frame)
        self.records += 1

    def finish(self) -> ChunkEntry:
        self.handle.seek(self.count_offset)
        self.handle.write(_U32.pack(self.records))
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()
        digest = hashlib.sha256()
        size = 0
        with self.temporary.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
                size += len(block)
        target = self.directory / f"{self.sequence:08d}-{digest.hexdigest()}.base"
        self.temporary.replace(target)
        descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return ChunkEntry(
            self.sequence, target.name, self.records, size,
            digest.hexdigest(), self.first_key, self.last_key,
        )


def stream_snapshot_to_chunks(
    conn: sqlite3.Connection,
    directory: Path,
    *,
    target_chunk_bytes: int = 4 * 1024 * 1024,
    start_after: tuple[str, tuple[object, ...]] | None = None,
) -> BaseCatalog:
    if target_chunk_bytes < 4096:
        raise ValueError("target_chunk_bytes is too small")
    directory.mkdir(parents=True, exist_ok=True)
    owns_snapshot = not conn.in_transaction
    if owns_snapshot:
        conn.execute("BEGIN")
    digest_hex = policy_digest()
    entries: list[ChunkEntry] = []
    writer = _ChunkWriter(directory, 0, digest_hex)
    total_records = 0
    try:
        for mutation in iter_indexed_snapshot_mutations(
            conn, start_after=start_after
        ):
            frame = encode_mutation_frame(mutation)
            if writer.records and writer.size + 4 + len(frame) > target_chunk_bytes:
                entries.append(writer.finish())
                writer = _ChunkWriter(directory, len(entries), digest_hex)
            writer.append(mutation, frame)
            total_records += 1
        if writer.records:
            entries.append(writer.finish())
        else:
            writer.handle.close()
            writer.temporary.unlink(missing_ok=True)
    except Exception:
        if not writer.handle.closed:
            writer.handle.close()
        writer.temporary.unlink(missing_ok=True)
        raise
    finally:
        if owns_snapshot:
            conn.rollback()
    root = hashlib.sha256(json.dumps(
        [entry.sha256 for entry in entries], separators=(",", ":")
    ).encode()).hexdigest()
    catalog = BaseCatalog(
        CATALOG_VERSION, digest_hex, target_chunk_bytes, total_records,
        sum(entry.bytes for entry in entries), root, tuple(entries),
    )
    body = json.dumps({
        **asdict(catalog),
        "chunks": [asdict(entry) for entry in entries],
    }, sort_keys=True, separators=(",", ":")).encode()
    temporary = directory / ".catalog.tmp"
    with temporary.open("wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(directory / "catalog.json")
    return catalog


def iter_chunk_mutations(
    path: Path,
    *,
    expected_sequence: int | None = None,
) -> Iterator[Mutation]:
    with path.open("rb") as handle:
        if handle.read(len(BASE_CHUNK_MAGIC)) != BASE_CHUNK_MAGIC:
            raise StreamingCodecError("unsupported base chunk")
        digest = handle.read(32).hex()
        if digest != policy_digest():
            raise StreamingCodecError("base chunk policy mismatch")
        sequence_raw = handle.read(4)
        count_raw = handle.read(4)
        if len(sequence_raw) != 4 or len(count_raw) != 4:
            raise StreamingCodecError("truncated base chunk header")
        sequence = _U32.unpack(sequence_raw)[0]
        if expected_sequence is not None and sequence != expected_sequence:
            raise StreamingCodecError("base chunk sequence mismatch")
        count = _U32.unpack(count_raw)[0]
        for _ in range(count):
            length_raw = handle.read(4)
            if len(length_raw) != 4:
                raise StreamingCodecError("truncated base frame length")
            length = _U32.unpack(length_raw)[0]
            if length > MAX_FRAME_BYTES:
                raise StreamingCodecError("base frame exceeds size bound")
            frame = handle.read(length)
            if len(frame) != length:
                raise StreamingCodecError("truncated base frame")
            yield decode_mutation_frame(frame)
        if handle.read(1):
            raise StreamingCodecError("trailing base chunk bytes")


def iter_catalog_mutations(
    directory: Path,
    catalog: BaseCatalog,
    *,
    start_chunk: int = 0,
) -> Iterator[Mutation]:
    if catalog.policy_digest != policy_digest():
        raise StreamingCodecError("base catalog policy mismatch")
    if [entry.sequence for entry in catalog.chunks] != list(range(len(catalog.chunks))):
        raise StreamingCodecError("base catalog sequence is not contiguous")
    root = hashlib.sha256(json.dumps(
        [entry.sha256 for entry in catalog.chunks], separators=(",", ":")
    ).encode()).hexdigest()
    if root != catalog.root_sha256:
        raise StreamingCodecError("base catalog root mismatch")
    for entry in catalog.chunks[start_chunk:]:
        if entry.filename != f"{entry.sequence:08d}-{entry.sha256}.base":
            raise StreamingCodecError("base artifact filename is not canonical")
        path = directory / entry.filename
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != entry.sha256:
            raise StreamingCodecError("base chunk digest mismatch")
        yield from iter_chunk_mutations(path, expected_sequence=entry.sequence)


def materialize_catalog(
    conn: sqlite3.Connection,
    directory: Path,
    catalog: BaseCatalog,
    *,
    batch_records: int = 1024,
    blob_store: ContentAddressedBlobStore | None = None,
) -> MaterializationReport:
    """Incrementally realize a base into a staging GraphDB.

    The target database is not published until its caller has validated the
    complete catalog root.  Batches are dependency-safe because the canonical
    numeric table order is dependency-safe; memory is bounded by one batch.
    """
    if batch_records < 1:
        raise ValueError("batch_records must be positive")
    ranks = {table: rank for rank, table in enumerate(BASE_TABLE_ORDER)}
    current_rank = -1
    batch: list[Mutation] = []
    applied = 0
    deleted = 0
    pending: set[str] = set()
    skipped: list[tuple[str, tuple]] = []

    def flush() -> None:
        nonlocal applied, deleted
        if not batch:
            return
        report = materialize(conn, batch, blob_store=blob_store)
        applied += report.applied
        deleted += report.deleted
        pending.update(report.pending_attachments)
        skipped.extend(report.skipped_orphans)
        batch.clear()

    for mutation in iter_catalog_mutations(directory, catalog):
        rank = ranks[mutation.table]
        if rank < current_rank:
            raise StreamingCodecError("base records are not in dependency order")
        if rank != current_rank or len(batch) >= batch_records:
            flush()
            current_rank = rank
        batch.append(mutation)
    flush()
    return MaterializationReport(
        applied, deleted, tuple(sorted(pending)), tuple(skipped)
    )
