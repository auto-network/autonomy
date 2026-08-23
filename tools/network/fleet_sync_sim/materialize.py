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
from .policies import PolicyKind, TABLE_POLICIES


class MaterializationError(ValueError):
    """The logical graph cannot be represented safely in the target DB."""


class ForeignKeyOrphanError(MaterializationError):
    """A NOT-NULL foreign key points at a parent absent from the whole
    checkpoint — referentially-orphaned debris, not a fresh conflict.

    The canonical example is a ``thoughts`` row whose ``sources`` conversation
    was deleted long ago without cascading (SQLite ships with foreign keys
    OFF, so such orphans accumulate silently in a source database and then
    surface only when a strict receiver replays them). Because ``_TABLE_ORDER``
    loads every parent table before its children, an insert that still fails a
    foreign key means the parent is genuinely not in the checkpoint at all.
    The receiver skips and quarantines the row rather than aborting the entire
    checkpoint over origin-side referential debris."""


@dataclass(frozen=True)
class MaterializationReport:
    applied: int
    deleted: int
    pending_attachments: tuple[str, ...]
    #: (table, address) of rows skipped as foreign-key orphans (see
    #: ForeignKeyOrphanError). Excluded from ``applied``; the caller must also
    #: exclude them from the winner-catalog install and the base/winner count
    #: invariants, and quarantine them for later repair.
    skipped_orphans: tuple[tuple[str, tuple], ...] = ()


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
    "sources", "entities", "nodes", "tags", "threads",
    "vault_content_bodies", "keycontrol_state", "keycontrol_grant",
    "keycontrol_credential",
    "keycontrol_bridge", "vault_content_objects", "settings",
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
    if policy.kind in {PolicyKind.IMMUTABLE, PolicyKind.IMMUTABLE_PRUNABLE}:
        raise MaterializationError(
            f"immutable table does not accept tombstones: {mutation.table}"
        )
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
        f'SELECT * FROM "{mutation.table}" WHERE {where}', params
    ).fetchone()
    if existing is None:
        _validate_immutable_row(mutation.table, row)
        try:
            _insert(conn, mutation.table, row)
        except sqlite3.IntegrityError as exc:
            if "FOREIGN KEY constraint failed" in str(exc):
                raise ForeignKeyOrphanError(
                    f"{mutation.table} row at {mutation.address!r} references a "
                    f"parent absent from the checkpoint: {exc}"
                ) from exc
            raise MaterializationError(
                f"{mutation.table} has a secondary-identity conflict at "
                f"{mutation.address!r}: {exc}"
            ) from exc
        return
    if policy.kind in {PolicyKind.IMMUTABLE, PolicyKind.IMMUTABLE_PRUNABLE}:
        _merge_immutable_row(conn, mutation.table, where, params, row, existing)
        return
    assignments = ",".join(f'"{column}"=?' for column in sorted(row))
    values = [row[column] for column in sorted(row)]
    conn.execute(
        f'UPDATE "{mutation.table}" SET {assignments} WHERE {where}',
        values + params,
    )


def _validate_immutable_row(table: str, row: dict[str, object]) -> None:
    if table != "vault_content_bodies":
        return
    body = row.get("body")
    digest = row.get("ciphertext_hash")
    size = row.get("size_bytes")
    if not isinstance(body, bytes) or not isinstance(digest, str):
        raise MaterializationError("vault ciphertext body has invalid storage types")
    if not isinstance(size, int) or isinstance(size, bool):
        raise MaterializationError("vault ciphertext size has invalid storage type")
    if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
        raise MaterializationError("vault ciphertext body does not match its hash/size")


def _merge_immutable_row(
    conn: sqlite3.Connection,
    table: str,
    where: str,
    params: list[object],
    incoming: dict[str, object],
    existing_raw: sqlite3.Row | tuple[object, ...],
) -> None:
    """Insert-once semantics, with one explicitly local nullable body."""

    columns = [str(item[1]) for item in conn.execute(f'PRAGMA table_info("{table}")')]
    existing = dict(zip(columns, existing_raw, strict=True))
    if set(existing) != set(incoming):
        raise MaterializationError(f"immutable {table} row has incomplete columns")
    _validate_immutable_row(table, incoming)
    if existing == incoming:
        return
    policy = TABLE_POLICIES[table]
    if policy.kind is PolicyKind.IMMUTABLE_PRUNABLE:
        non_wire = set(existing) - {"wire"}
        if all(existing[column] == incoming[column] for column in non_wire):
            old_wire = existing["wire"]
            new_wire = incoming["wire"]
            if old_wire is None and new_wire is not None:
                conn.execute(f'UPDATE "{table}" SET wire=? WHERE {where}', [new_wire] + params)
                return
            if old_wire is not None and new_wire is None:
                # A remote/local prune cannot erase a body this peer holds.
                return
    raise MaterializationError(
        f"immutable {table} row conflicts at its logical address"
    )


def _finish_vault_materialization(conn: sqlite3.Connection) -> None:
    missing = conn.execute(
        "SELECT object_id,revision_id FROM vault_content_objects o "
        "WHERE NOT EXISTS (SELECT 1 FROM vault_content_bodies b "
        "WHERE b.ciphertext_hash=o.ciphertext_hash) LIMIT 1"
    ).fetchone()
    if missing is not None:
        raise MaterializationError(
            "vault content object references a missing ciphertext body"
        )
    conn.execute("DELETE FROM vault_state_object_counts")
    conn.execute(
        "INSERT INTO vault_state_object_counts(storage_state_id,object_count) "
        "SELECT storage_state_id,COUNT(*) FROM vault_content_objects "
        "GROUP BY storage_state_id"
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
    skipped: list[tuple[str, tuple]] = []
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
                try:
                    _upsert(conn, mutation, row)
                except ForeignKeyOrphanError:
                    # A NOT-NULL parent is absent from the whole checkpoint:
                    # skip the row, record it for the caller to exclude from
                    # the winner catalog and to quarantine. A statement-level
                    # constraint abort leaves the transaction usable, so the
                    # rest of the batch still applies.
                    skipped.append((mutation.table, tuple(mutation.address)))
                    continue
                applied += 1
        _finish_vault_materialization(conn)
    return MaterializationReport(
        applied, deleted, tuple(sorted(pending)), tuple(skipped)
    )
