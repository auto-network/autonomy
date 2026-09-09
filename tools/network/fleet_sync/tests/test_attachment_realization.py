"""Attachment realization: local blob store wiring, quarantine carry, deltas."""

import hashlib
import json
import sqlite3
from pathlib import Path

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync.materialize import (
    ContentAddressedBlobStore,
    production_blob_store,
)

EPOCH = 7
ROSTER = ("machine-a", "machine-b")



def _first_transaction(catalog, origin: str):
    """(ref, items) of the origin's first transaction, served from rows."""
    page = catalog.next_transactions_for_origin(origin, 0, None, limit=1)
    if not page:
        return None
    ref, _ts, _tx, items = page[0]
    return ref, items

def _identity(path: Path, marker: str) -> None:
    graph = GraphDB(path)
    try:
        graph.conn.execute(
            "INSERT INTO settings(id,set_id,schema_revision,key,payload,"
            "publication_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (marker, "autonomy.identity.personal", 1, "root", '{}', "raw",
             "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
        )
        graph.conn.execute(
            "INSERT INTO orgs(id,slug,type,created_at) VALUES(?,?,?,?)",
            ("local-org", "personal", "personal", "2026-08-19T00:00:00Z"),
        )
        graph.conn.commit()
    finally:
        graph.close()


def _payload(tmp_path: Path, name: str, content: bytes) -> tuple[Path, str]:
    path = tmp_path / name
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest()


def _insert_attachment(conn, att_id, digest, size, file_path) -> None:
    conn.execute(
        "INSERT INTO attachments(id,hash,filename,mime_type,size_bytes,"
        "file_path,metadata,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (att_id, digest, f"{att_id}.png", "image/png", size,
         str(file_path), "{}", "2026-08-19T00:00:00Z"),
    )


def _quarantine_rows(conn) -> list[dict]:
    return [dict(zip((c[1] for c in conn.execute(
        "PRAGMA table_info(fleet_sync_quarantine)")), row))
        for row in conn.execute(
            "SELECT * FROM fleet_sync_quarantine ORDER BY table_name")]


def test_delta_defer_does_not_poison_batch(tmp_path: Path) -> None:
    source = GraphDB(tmp_path / "source.db")
    target = GraphDB(tmp_path / "target.db")
    try:
        left = MutationCatalog(source.conn, "machine-a")
        right = MutationCatalog(target.conn, "machine-b")
        left.install()
        right.install()
        content = b"delta-bytes"
        payload, digest = _payload(tmp_path, "img4.png", content)
        with left.transaction(100, "tx-1"):
            _insert_attachment(
                source.conn, "att-4", digest, len(content), payload
            )
            source.conn.execute(
                "INSERT INTO sources(id,type,title,metadata,created_at,"
                "ingested_at) VALUES('s1','note','t','{}',"
                "'2026-08-19T00:00:00Z','2026-08-19T00:00:00Z')"
            )
        served = _first_transaction(left, "machine-a")
        assert served is not None and len(served[1]) == 2

        # Without a store the attachment defers; the batch must not raise
        # and the ordinary row must land.
        assert right.apply_remote_batch(served[1]) == (1, 0)
        assert target.conn.execute(
            "SELECT COUNT(*) FROM sources"
        ).fetchone()[0] == 1
        assert target.conn.execute(
            "SELECT COUNT(*) FROM attachments"
        ).fetchone()[0] == 0
        rows = _quarantine_rows(target.conn)
        assert [r["reason"] for r in rows] == ["attachment_bytes_unavailable"]
        assert rows[0]["frame"] is not None
        # The deferred row is still forwarded onward: the quarantine keeps
        # its frame, and the served transaction is never short.
        onward = _first_transaction(right, "machine-a")
        assert onward is not None and len(onward[1]) == 2
        assert sorted(i.mutation.table for i in onward[1]) == ["attachments", "sources"]

        # With a store that can fetch the bytes, replaying the same batch
        # realizes the attachment (the drain path) and duplicates stay inert.
        right.blob_store = ContentAddressedBlobStore(
            tmp_path / "cas", lambda d: content if d == digest else None
        )
        assert right.apply_remote_batch(served[1]) == (1, 1)
        row = target.conn.execute(
            "SELECT hash,file_path FROM attachments WHERE id='att-4'"
        ).fetchone()
        assert row is not None and row[0] == digest
        assert Path(row[1]).read_bytes() == content
    finally:
        source.close()
        target.close()


def test_delta_realizes_attachment_with_local_bytes(tmp_path: Path) -> None:
    source = GraphDB(tmp_path / "source.db")
    target = GraphDB(tmp_path / "target.db")
    try:
        left = MutationCatalog(source.conn, "machine-a")
        right = MutationCatalog(target.conn, "machine-b")
        left.install()
        right.install()
        content = b"already-here"
        payload, digest = _payload(tmp_path, "img5.png", content)
        with left.transaction(100, "tx-1"):
            _insert_attachment(
                source.conn, "att-5", digest, len(content), payload
            )
        served = _first_transaction(left, "machine-a")
        assert served is not None
        right.blob_store = production_blob_store(
            tmp_path / "target.db", extra_source=tmp_path / "source.db"
        )
        assert right.apply_remote_batch(served[1]) == (1, 0)
        row = target.conn.execute(
            "SELECT file_path FROM attachments WHERE id='att-5'"
        ).fetchone()
        assert row is not None
        assert Path(row[0]).read_bytes() == content
        # Nothing quarantined: the lazily created table never appeared.
        assert target.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='fleet_sync_quarantine'"
        ).fetchone() is None
    finally:
        source.close()
        target.close()
