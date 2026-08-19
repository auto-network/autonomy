"""Apply converged logical mutations to a real GraphDB schema.

This module is deliberately part of the simulation: it proves that the codec
describes the logical graph rather than merely round-tripping an invented
object model.  Checkpoint and streaming records first converge in a
``MutationInbox``; only its winners are applied here.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Callable, Iterable

from .codec import CanonicalValue, Mutation, encode_value
from .policies import TABLE_POLICIES


class MaterializationError(ValueError):
    """The logical graph cannot be represented safely in the target DB."""


@dataclass(frozen=True)
class MaterializationReport:
    applied: int
    deleted: int
    pending_attachments: tuple[str, ...]


class ContentAddressedBlobStore:
    """Verify and atomically install attachment bytes below one local root."""

    def __init__(self, root: Path, fetch: Callable[[str], bytes | None]) -> None:
        self.root = root
        self.fetch = fetch

    def materialize(self, digest: str, size: int, filename: str) -> Path | None:
        suffix = Path(filename).suffix[:16]
        target = self.root / digest[:2] / f"{digest}{suffix}"
        if target.is_file() and self._valid(target, digest, size):
            return target
        payload = self.fetch(digest)
        if payload is None:
            return None
        if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
            raise MaterializationError(f"attachment bytes do not match {digest}")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{digest}.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return target

    @staticmethod
    def _valid(path: Path, digest: str, size: int) -> bool:
        if path.stat().st_size != size:
            return False
        return hashlib.sha256(path.read_bytes()).hexdigest() == digest


# Parents precede children. Tables without declared foreign keys are still
# placed after the content they describe so failures are understandable.
_TABLE_ORDER = (
    "sources", "entities", "nodes", "tags", "threads", "settings",
    "thoughts", "derivations", "claims", "edges", "entity_mentions",
    "node_refs", "note_comments", "note_reads", "captures", "attachments",
    "note_versions",
)


def _sql_value(value: CanonicalValue) -> object:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    if isinstance(value, bool):
        return int(value)
    return value


def _row(mutation: Mutation) -> dict[str, object]:
    return {key: _sql_value(value) for key, value in mutation.values}


def _where(columns: tuple[str, ...], row: dict[str, object]) -> tuple[str, list[object]]:
    terms: list[str] = []
    values: list[object] = []
    for column in columns:
        value = row[column]
        if value is None:
            terms.append(f'"{column}" IS NULL')
        else:
            terms.append(f'"{column}" = ?')
            values.append(value)
    return " AND ".join(terms), values


def _edge_id(mutation: Mutation) -> str:
    return "fleet-edge-" + hashlib.sha256(
        encode_value([mutation.table, list(mutation.address)])
    ).hexdigest()[:32]


def _delete(conn: sqlite3.Connection, mutation: Mutation) -> None:
    policy = TABLE_POLICIES[mutation.table]
    if mutation.table == "note_versions":
        source_id, created_at, content_hash = mutation.address
        rows = conn.execute(
            "SELECT id,content FROM note_versions WHERE source_id=? AND created_at=?",
            (source_id, created_at),
        ).fetchall()
        for row_id, content in rows:
            if hashlib.sha256(str(content).encode()).hexdigest() == content_hash:
                conn.execute("DELETE FROM note_versions WHERE id=?", (row_id,))
        return
    row = _row(mutation)
    # Tombstones carry no values, so their logical address supplies keys.
    if not row:
        row = dict(zip(policy.key, mutation.address, strict=True))
    key_columns = policy.key
    if mutation.table == "settings":
        # The fifth logical address component is a synthetic row role.
        key_columns = ("set_id", "schema_revision", "key", "publication_state")
        row = dict(zip(policy.key, mutation.address, strict=True))
        role = str(row["row_role"])
        if role != "base":
            try:
                row_id = role.rsplit(":", 1)[1]
            except IndexError as exc:
                raise MaterializationError(
                    f"invalid settings tombstone role: {role}"
                ) from exc
            conn.execute("DELETE FROM settings WHERE id=?", (row_id,))
            return
    where, params = _where(key_columns, row)
    conn.execute(f'DELETE FROM "{mutation.table}" WHERE {where}', params)


def _apply_note_versions(conn: sqlite3.Connection, mutations: list[Mutation]) -> int:
    applied = 0
    for mutation in mutations:
        if mutation.tombstone:
            _delete(conn, mutation)
            continue
        row = _row(mutation)
        duplicate = conn.execute(
            "SELECT content FROM note_versions WHERE source_id=? AND created_at=?",
            (row["source_id"], row["created_at"]),
        ).fetchall()
        digest = str(mutation.address[2])
        if any(hashlib.sha256(str(item[0]).encode()).hexdigest() == digest
               for item in duplicate):
            continue
        # A temporary negative version cannot collide with authored versions.
        temporary_version = -1 - conn.execute(
            "SELECT COUNT(*) FROM note_versions WHERE source_id=?",
            (row["source_id"],),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO note_versions(source_id,version,content,created_at) "
            "VALUES(?,?,?,?)",
            (row["source_id"], temporary_version, row["content"], row["created_at"]),
        )
        applied += 1

    source_ids = {str(m.address[0]) for m in mutations}
    for source_id in source_ids:
        rows = conn.execute(
            "SELECT id,created_at,content FROM note_versions WHERE source_id=?",
            (source_id,),
        ).fetchall()
        ordered = sorted(rows, key=lambda item: (
            str(item[1]), hashlib.sha256(str(item[2]).encode()).hexdigest()
        ))
        # Move out of the positive unique namespace before assigning display order.
        for offset, (row_id, _, _) in enumerate(ordered, 1):
            conn.execute("UPDATE note_versions SET version=? WHERE id=?",
                         (-1_000_000 - offset, row_id))
        for version, (row_id, _, _) in enumerate(ordered, 1):
            conn.execute("UPDATE note_versions SET version=? WHERE id=?",
                         (version, row_id))
    return applied


def _upsert(conn: sqlite3.Connection, mutation: Mutation, row: dict[str, object]) -> None:
    policy = TABLE_POLICIES[mutation.table]
    if mutation.table == "edges":
        row["id"] = _edge_id(mutation)
    if mutation.table == "sources":
        row["file_path"] = None

    key_columns = policy.key
    if mutation.table == "settings":
        # Slot identity, including override/exclusion role, is resolved in the
        # envelope. Personal rows have no signer slot.
        role = str(mutation.address[4])
        clauses = ["set_id=?", "schema_revision=?", '"key"=?',
                   "publication_state=?"]
        params: list[object] = list(mutation.address[:4])
        if role == "base":
            clauses.extend(["supersedes IS NULL", "excludes IS NULL"])
        elif role.startswith("supersedes:"):
            clauses.append("supersedes=?")
            params.append(row["supersedes"])
        elif role.startswith("excludes:"):
            clauses.append("excludes=?")
            params.append(row["excludes"])
        else:
            raise MaterializationError(f"unknown settings row role: {role}")
        conn.execute("DELETE FROM settings WHERE " + " AND ".join(clauses), params)
        _insert(conn, mutation.table, row)
        return

    where, params = _where(key_columns, row)
    existing = conn.execute(
        f'SELECT rowid FROM "{mutation.table}" WHERE {where}', params
    ).fetchone()
    if existing is None:
        try:
            _insert(conn, mutation.table, row)
        except sqlite3.IntegrityError as exc:
            raise MaterializationError(
                f"{mutation.table} has a secondary-identity conflict at "
                f"{mutation.address!r}: {exc}"
            ) from exc
        return
    assignments = ",".join(f'"{column}"=?' for column in sorted(row))
    values = [row[column] for column in sorted(row)]
    conn.execute(
        f'UPDATE "{mutation.table}" SET {assignments} WHERE {where}',
        values + params,
    )


def _insert(conn: sqlite3.Connection, table: str, row: dict[str, object]) -> None:
    columns = sorted(row)
    names = ",".join(f'"{column}"' for column in columns)
    placeholders = ",".join("?" for _ in columns)
    conn.execute(
        f'INSERT INTO "{table}"({names}) VALUES({placeholders})',
        [row[column] for column in columns],
    )


def materialize(
    conn: sqlite3.Connection,
    mutations: Iterable[Mutation],
    *,
    blob_store: ContentAddressedBlobStore | None = None,
    manage_transaction: bool = True,
) -> MaterializationReport:
    """Apply already-converged winners as one foreign-key-safe transaction."""

    grouped: dict[str, list[Mutation]] = {table: [] for table in _TABLE_ORDER}
    for mutation in mutations:
        if mutation.table not in grouped:
            raise MaterializationError(f"no materializer for {mutation.table}")
        grouped[mutation.table].append(mutation)

    applied = 0
    deleted = 0
    pending: list[str] = []
    transaction = conn if manage_transaction else nullcontext()
    with transaction:
        for table in _TABLE_ORDER:
            table_mutations = grouped[table]
            if table == "note_versions":
                applied += _apply_note_versions(conn, table_mutations)
                deleted += sum(m.tombstone for m in table_mutations)
                continue
            for mutation in table_mutations:
                if mutation.tombstone:
                    _delete(conn, mutation)
                    deleted += 1
                    continue
                row = _row(mutation)
                if table == "attachments":
                    if blob_store is None:
                        pending.append(str(row["id"]))
                        continue
                    path = blob_store.materialize(
                        str(row["hash"]), int(row["size_bytes"]),
                        str(row["filename"]),
                    )
                    if path is None:
                        pending.append(str(row["id"]))
                        continue
                    row["file_path"] = str(path)
                _upsert(conn, mutation, row)
                applied += 1
    return MaterializationReport(applied, deleted, tuple(sorted(pending)))
