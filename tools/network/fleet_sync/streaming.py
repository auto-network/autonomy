"""Indexed canonical-base ordering and projection for fleet synchronization.

Rows are ordered by table and logical key so SQLite can provide the order
directly. The sweep bootstrap walks the keyspace in exactly this order, so
this module owns the index set, the key expressions, and the policy digest
that pins the replicated surface.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from typing import Iterator

from .codec import Mutation
from .policies import PolicyKind, TABLE_POLICIES, TablePolicy, audit_schema
from .snapshot import _logical_address, _logical_values, _row_timestamp


_U32 = struct.Struct(">I")

# Dependency-safe numeric table order for canonical bases.  The number is the
# tuple position; production code generation can freeze those IDs explicitly.
BASE_TABLE_ORDER = (
    "sources", "nodes", "tags", "threads",
    "root_anchors", "vault_factors", "policy_classes", "vault_secrets",
    "vault_content_bodies", "keycontrol_state", "keycontrol_grant",
    "keycontrol_credential",
    "keycontrol_bridge", "vault_content_objects", "settings",
    "thoughts", "derivations", "claims", "edges",
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
            'COALESCE("terminal_persona",\'\')',
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
            # catalog address and breaks the row-count invariant against the
            # catalog's ON CONFLICT(address) collapse. Skip
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
            resume_key = tuple(start_address)
            if table == "settings" and len(resume_key) == len(expressions) - 1:
                resume_key += ("",)  # an unsigned row's address has no persona
            params += resume_key
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
