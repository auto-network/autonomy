"""A server restored from backup must recover and resume — with no ceremony.

These tests emulate the honest restore path: the operator deletes the
database files and copies a backup into place with plain filesystem
commands. Nothing stamps the file, nothing notifies the fleet. The
synchronization protocol itself must detect the rollback from evidence and
reconverge.

Two independent failure modes are pinned:

1. A peer's saved resume cursor is denominated in the server's local
   journal row ids. A restore rolls the id sequence back, so the stale
   cursor silently skips everything the server authors afterwards.
2. The restored server's own ``last_timestamp`` promise has rolled back.
   When the fleet hands the server its own post-backup history back
   through the ordinary apply path, the floor must fast-forward to cover
   it, restoring the origin's monotonic-timestamp promise.
"""

from __future__ import annotations

import sqlite3
import shutil
from pathlib import Path

from tools.graph.db import GraphDB
from tools.graph.models import Source
from tools.network.fleet_sync_scheduler import SQLiteFleetSyncStore
from tools.network.idkit import KeyPair


def _prepare(path: Path, machine: KeyPair) -> None:
    db = GraphDB(path)
    try:
        db.activate_fleet_sync_writers(machine.public_hex)
    finally:
        db.close()


def _insert(path: Path, source_id: str, title: str) -> None:
    db = GraphDB(path)
    try:
        db.insert_source(Source(id=source_id, type="note", title=title))
    finally:
        db.close()


def _backup(path: Path, dest: Path) -> None:
    source = sqlite3.connect(path)
    target = sqlite3.connect(dest)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _cli_restore(backup: Path, path: Path) -> None:
    """Replace the database exactly as an operator with a shell would."""
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()
    shutil.copy2(backup, path)


def _drain(store: SQLiteFleetSyncStore, cursor: int):
    items = []
    while (page := store.next_transaction(cursor)) is not None:
        cursor, batch = page
        items.extend(batch)
    return cursor, items


def _pull(store: SQLiteFleetSyncStore, trail: list):
    """One pull exactly as the scheduler performs it: the client presents its
    verified breadcrumb trail, the server recomputes the position from its
    own journal, and the client prepends the acknowledged stream position."""
    cursor = store.resume_ref(trail)
    cursor, items = _drain(store, cursor)
    breadcrumb = store.breadcrumb(cursor) if cursor else None
    if breadcrumb is not None:
        trail.insert(0, breadcrumb)
    return items


def _last_timestamp(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute(
            "SELECT last_timestamp FROM fleet_sync_state WHERE singleton=1"
        ).fetchone()[0])


def test_peer_with_stale_trail_still_receives_post_restore_writes(tmp_path):
    machine = KeyPair.generate()
    path = tmp_path / "server.db"
    _prepare(path, machine)
    _insert(path, "src-before", "written before the backup")
    store = SQLiteFleetSyncStore(path)

    # The peer consumes everything, keeping its breadcrumb trail.
    trail: list = []
    _pull(store, trail)

    _backup(path, tmp_path / "backup.db")

    # Post-backup work advances the journal; the peer stays caught up.
    for index in range(20):
        _insert(path, f"src-lost-{index}", "after the backup")
    _pull(store, trail)

    # Disk dies; the operator restores the backup with plain file commands.
    _cli_restore(tmp_path / "backup.db", path)

    # The restored server keeps working.
    _insert(path, "src-after-restore", "authored after the restore")

    # The peer returns with its trail. Its newest breadcrumb names a
    # transaction the restore erased, so the server resumes from the
    # newest breadcrumb it still knows — the pre-backup pull position.
    items = _pull(store, trail)
    addresses = {
        item.mutation.address
        for item in items
        if item.mutation.table == "sources"
    }
    assert ("src-after-restore",) in addresses
    # Bounded replay: the divergence window only, never the whole journal.
    # Everything consumed under the still-valid breadcrumb stays unserved.
    assert ("src-before",) not in addresses


def test_apply_of_own_history_fast_forwards_the_write_floor(tmp_path):
    machine = KeyPair.generate()
    path = tmp_path / "server.db"
    _prepare(path, machine)

    _backup(path, tmp_path / "backup.db")

    # Post-backup authored history, held by the fleet.
    _insert(path, "src-lost", "after the backup")
    store = SQLiteFleetSyncStore(path)
    _, items = _drain(store, 0)
    authored = [
        item for item in items if not item.transaction_id.startswith("bootstrap")
    ]
    assert authored
    authored_timestamp = max(item.mutation.timestamp_ns for item in authored)

    _cli_restore(tmp_path / "backup.db", path)
    assert _last_timestamp(path) < authored_timestamp

    # A peer hands the restored server its own history back through the
    # ordinary apply path. The origin's monotonic promise must recover.
    store.apply(authored)
    assert _last_timestamp(path) >= authored_timestamp
