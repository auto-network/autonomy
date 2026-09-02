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
from tools.network.fleet_sync.sync import FleetSyncAlpha, install_checkpoint

EPOCH = 7
ROSTER = ("machine-a", "machine-b")


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


def test_install_realizes_attachment_when_bytes_local(tmp_path: Path) -> None:
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target" / "target.db"
    content = b"attachment-bytes-1"
    payload, digest = _payload(tmp_path, "img.png", content)
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(100, "tx-1"):
            _insert_attachment(
                origin.graph.conn, "att-1", digest, len(content), payload
            )
        checkpoint = origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=EPOCH,
            active_roster=ROSTER, target_chunk_bytes=4096,
        )
    assert checkpoint.winner_records >= 1
    _identity(target_path, "target-secret")
    install_checkpoint(
        tmp_path / "checkpoint", target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=EPOCH,
        expected_active_roster=ROSTER,
        blob_store=production_blob_store(
            target_path, extra_source=origin_path
        ),
    )
    conn = sqlite3.connect(target_path)
    try:
        row = conn.execute(
            "SELECT hash,file_path FROM attachments WHERE id='att-1'"
        ).fetchone()
        assert row is not None and row[0] == digest
        realized = Path(row[1])
        assert realized.is_relative_to(target_path.parent / "uploads" / "fleet")
        assert realized.read_bytes() == content
        quarantined = conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_quarantine"
        ).fetchone()[0] if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='fleet_sync_quarantine'"
        ).fetchone() else 0
        assert quarantined == 0
    finally:
        conn.close()


def test_install_quarantines_missing_bytes_and_carries_backlog(
    tmp_path: Path,
) -> None:
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target" / "target.db"
    content = b"attachment-bytes-2"
    payload, digest = _payload(tmp_path, "img2.png", content)
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(100, "tx-1"):
            _insert_attachment(
                origin.graph.conn, "att-2", digest, len(content), payload
            )
        origin.checkpoint(
            tmp_path / "cp1", roster_epoch=EPOCH,
            active_roster=ROSTER, target_chunk_bytes=4096,
        )
    payload.unlink()  # No local file satisfies the digest anywhere.
    _identity(target_path, "target-secret")
    install_checkpoint(
        tmp_path / "cp1", target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=EPOCH,
        expected_active_roster=ROSTER,
        blob_store=production_blob_store(target_path),
    )
    conn = sqlite3.connect(target_path)
    rows = _quarantine_rows(conn)
    conn.close()
    assert [
        (r["table_name"], r["reason"]) for r in rows
    ] == [("attachments", "attachment_bytes_unavailable")]

    # Delta-defer a second attachment from a third machine onto the target:
    # its quarantine entry carries the canonical frame, and the next
    # checkpoint (which never mentions it) must not erase it.
    graph = GraphDB(target_path)
    try:
        catalog = MutationCatalog(graph.conn, "machine-b")
        catalog.install()
        with FleetSyncAlpha(tmp_path / "third.db", "machine-c") as third:
            other, other_digest = _payload(tmp_path, "img3.png", b"third-bytes")
            with third.author(200, "tx-c"):
                _insert_attachment(
                    third.graph.conn, "att-3", other_digest,
                    len(b"third-bytes"), other,
                )
            served = third.catalog.next_journal_transaction_ref(0)
        other.unlink()
        assert served is not None
        applied, ignored = catalog.apply_remote_batch(served[1])
        assert (applied, ignored) == (0, 0)
    finally:
        graph.close()

    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(300, "tx-2"):
            origin.graph.conn.execute(
                "INSERT INTO sources(id,type,title,metadata,created_at,"
                "ingested_at) VALUES('s1','note','t','{}',"
                "'2026-08-19T00:00:00Z','2026-08-19T00:00:00Z')"
            )
        origin.checkpoint(
            tmp_path / "cp2", roster_epoch=EPOCH,
            active_roster=ROSTER, target_chunk_bytes=4096,
        )
    install_checkpoint(
        tmp_path / "cp2", target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=EPOCH,
        expected_active_roster=ROSTER,
        blob_store=production_blob_store(target_path),
        merge_existing=True,
    )
    conn = sqlite3.connect(target_path)
    rows = _quarantine_rows(conn)
    conn.close()
    by_address = {json.loads(r["logical_address"])[0]: r for r in rows}
    assert set(by_address) == {"att-2", "att-3"}
    # The delta-deferred entry kept its frame; the checkpoint entry has none.
    assert by_address["att-3"]["frame"] is not None
    assert by_address["att-2"]["frame"] is None


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
        served = left.next_journal_transaction_ref(0)
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
        # The deferred frame was still journaled for onward serving.
        assert target.conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_journal"
        ).fetchone()[0] == 2

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
        served = left.next_journal_transaction_ref(0)
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
