"""Project a current personal GraphDB into canonical mutation bytes.

Snapshots are a bootstrap input to fleet synchronization, not a substitute for the
production mutation log: a current SQLite image cannot reconstruct historical
tombstones or overwritten values.  Every projected live row nevertheless uses
the exact same :class:`Mutation` envelope as streamed changes and decoded
checkpoint segments.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any, Iterator

from .codec import CanonicalValue, CodecError, Mutation, encode_stream
from .policies import PolicyKind, TABLE_POLICIES, TablePolicy, audit_schema


def _timestamp_ns(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise CodecError("boolean is not a timestamp")
    if isinstance(value, int):
        # The only integer timestamp in the v7 graph schema is signed_at,
        # documented as Unix milliseconds.
        if value < 0:
            raise CodecError("timestamp must be non-negative")
        return value * 1_000_000
    if not isinstance(value, str):
        raise CodecError(f"unsupported timestamp storage: {type(value).__name__}")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CodecError(f"invalid ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    seconds = int(parsed.timestamp())
    return seconds * 1_000_000_000 + parsed.microsecond * 1_000


def _row_timestamp(policy: TablePolicy, row: dict[str, Any]) -> int:
    for column in policy.timestamp_columns:
        if column in row and row[column] is not None:
            return _timestamp_ns(row[column])
    return 0


def _json_value(table: str, column: str, value: object) -> CanonicalValue:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CodecError(f"{table}.{column} JSON must be stored as text or NULL")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise CodecError(f"{table}.{column} contains invalid JSON") from exc
    # The canonical encoder performs the complete type/bounds validation.
    return decoded


def _setting_role(row: dict[str, Any]) -> str:
    if row.get("supersedes") is not None:
        return f"supersedes:{row['supersedes']}:{row['id']}"
    if row.get("excludes") is not None:
        return f"excludes:{row['excludes']}:{row['id']}"
    return "base"


def _logical_address(
    policy: TablePolicy,
    row: dict[str, Any],
) -> tuple[CanonicalValue, ...]:
    if policy.table == "note_versions":
        content = row.get("content")
        if not isinstance(content, str):
            raise CodecError("note_versions.content must be text")
        return (
            row["source_id"],
            row["created_at"],
            hashlib.sha256(content.encode("utf-8")).hexdigest(),
        )
    if policy.table == "settings":
        address: tuple[CanonicalValue, ...] = (
            row["set_id"],
            row["schema_revision"],
            row["key"],
            row["publication_state"],
            _setting_role(row),
        )
        # One slot per signer (graph://21a0da9e-1c2, auto-y068i): a signed
        # row's address carries its terminal persona, so two members'
        # rows at one natural key are two logical rows on every store. An
        # unsigned row keeps the five-part address, byte-for-byte as before.
        persona = row.get("terminal_persona")
        if persona is not None:
            address = address + (persona,)
        return address
    return tuple(row[column] for column in policy.key)


def _logical_values(
    policy: TablePolicy,
    row: dict[str, Any],
) -> tuple[tuple[str, CanonicalValue], ...]:
    out: list[tuple[str, CanonicalValue]] = []
    for column in sorted(row):
        if column in policy.excluded_columns:
            continue
        value: CanonicalValue = row[column]
        if column in policy.json_columns:
            value = _json_value(policy.table, column, value)
        out.append((column, value))
    return tuple(out)


def iter_snapshot_mutations(conn: sqlite3.Connection) -> Iterator[Mutation]:
    """Yield every replicating live logical row in deterministic table order."""

    audit_schema(conn)
    conn.row_factory = sqlite3.Row
    for table in sorted(TABLE_POLICIES):
        policy = TABLE_POLICIES[table]
        if policy.kind in {PolicyKind.LOCAL, PolicyKind.DERIVED}:
            continue
        rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
        for raw in rows:
            row = dict(raw)
            yield Mutation(
                table=table,
                address=_logical_address(policy, row),
                timestamp_ns=_row_timestamp(policy, row),
                tombstone=False,
                values=_logical_values(policy, row),
            )


def encode_snapshot(conn: sqlite3.Connection) -> bytes:
    """Canonical stream for all currently live, replicating graph rows."""

    return encode_stream(iter_snapshot_mutations(conn))
