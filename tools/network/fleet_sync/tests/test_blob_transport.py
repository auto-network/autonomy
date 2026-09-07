"""Attachment object transport: framing, corruption, end-to-end drain."""

import hashlib
import sqlite3
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync.blob_transport import (
    BLOB_CHUNK_BYTES,
    BlobReceiver,
    BlobTransportError,
    decode_blob_request,
    encode_blob_request,
    iter_blob_frames,
    pending_attachment_backlog,
)
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync.harness import HarnessFleet
from tools.network.fleet_sync.materialize import (
    ContentAddressedBlobStore,
    MaterializationError,
)


def _insert_attachment(conn, att_id, digest, size, file_path) -> None:
    conn.execute(
        "INSERT INTO attachments(id,hash,filename,mime_type,size_bytes,"
        "file_path,metadata,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (att_id, digest, f"{att_id}.png", "image/png", size,
         str(file_path), "{}", "2026-08-19T00:00:00Z"),
    )


def test_request_roundtrip_and_bounds() -> None:
    digest = "ab" * 32
    assert decode_blob_request(encode_blob_request([digest])) == [digest]
    with pytest.raises(BlobTransportError):
        encode_blob_request([])
    with pytest.raises(BlobTransportError):
        encode_blob_request(["nothex"])
    with pytest.raises(BlobTransportError):
        decode_blob_request(b"{\"v\":1,\"op\":\"blob\"}")


def test_serve_and_receive_multichunk_object(tmp_path: Path) -> None:
    content = b"x" * (BLOB_CHUNK_BYTES + 1024)  # forces two chunks
    digest = hashlib.sha256(content).hexdigest()
    payload = tmp_path / "big.bin"
    payload.write_bytes(content)
    db_path = tmp_path / "server.db"
    graph = GraphDB(db_path)
    try:
        _insert_attachment(graph.conn, "big", digest, len(content), payload)
        graph.conn.commit()
    finally:
        graph.close()

    frames = list(iter_blob_frames(db_path, [digest, "ff" * 32]))
    # begin + 2 chunks + done; the unknown digest is reported missing.
    assert len(frames) == 4

    store = ContentAddressedBlobStore(tmp_path / "cas", lambda _d: None)
    receiver = BlobReceiver(store)
    for frame in frames:
        receiver.feed(frame)
    assert receiver.done
    assert receiver.missing == ["ff" * 32]
    assert receiver.adopted[digest].read_bytes() == content


def test_receiver_refuses_tampered_and_out_of_order(tmp_path: Path) -> None:
    content = b"y" * 2048
    digest = hashlib.sha256(content).hexdigest()
    payload = tmp_path / "obj.bin"
    payload.write_bytes(content)
    db_path = tmp_path / "server.db"
    graph = GraphDB(db_path)
    try:
        _insert_attachment(graph.conn, "obj", digest, len(content), payload)
        graph.conn.commit()
    finally:
        graph.close()
    frames = list(iter_blob_frames(db_path, [digest]))

    # Tampered payload bytes: adoption verifies the digest and refuses.
    store = ContentAddressedBlobStore(tmp_path / "cas1", lambda _d: None)
    receiver = BlobReceiver(store)
    receiver.feed(frames[0])
    tampered = frames[1][:8] + b"Z" + frames[1][9:]
    with pytest.raises(MaterializationError):
        receiver.feed(tampered)
    receiver.close()
    assert not any((tmp_path / "cas1").rglob(digest + "*"))

    # Out-of-order chunk sequence is refused before any adoption.
    store2 = ContentAddressedBlobStore(tmp_path / "cas2", lambda _d: None)
    receiver2 = BlobReceiver(store2)
    receiver2.feed(frames[0])
    wrong_seq = frames[1][:4] + b"\x00\x00\x00\x07" + frames[1][8:]
    with pytest.raises(BlobTransportError):
        receiver2.feed(wrong_seq)
    receiver2.close()


def test_end_to_end_attachment_crosses_the_fleet(tmp_path: Path) -> None:
    """An attachment authored on A arrives byte-identical on B: the row
    defers on delta apply (no local bytes), the post-pull drain fetches the
    object over the blob op, and the backlog clears."""
    fleet = HarnessFleet(tmp_path / "fleet", size=2).build()
    content = b"the-actual-attachment-bytes" * 100
    digest = hashlib.sha256(content).hexdigest()
    payload = tmp_path / "photo.png"
    payload.write_bytes(content)
    try:
        fleet.start_all()
        graph = GraphDB(fleet.machines[0].db_path)
        try:
            _insert_attachment(
                graph.conn, "shared-att", digest, len(content), payload
            )
            graph.conn.commit()
        finally:
            graph.close()

        def realized_on_b() -> bool:
            try:
                with sqlite3.connect(
                    f"file:{fleet.machines[1].db_path}"
                    "?mode=ro&immutable=1",
                    uri=True,
                ) as conn:
                    row = conn.execute(
                        "SELECT file_path FROM attachments "
                        "WHERE id='shared-att'"
                    ).fetchone()
            except sqlite3.Error:
                return False
            if row is None or not Path(row[0]).is_file():
                return False
            # Realization and the quarantine-entry delete commit in
            # separate transactions; converged means both are visible.
            try:
                with sqlite3.connect(
                    f"file:{fleet.machines[1].db_path}"
                    "?mode=ro&immutable=1",
                    uri=True,
                ) as conn:
                    return conn.execute(
                        "SELECT COUNT(*) FROM fleet_sync_quarantine"
                    ).fetchone()[0] == 0
            except sqlite3.Error:
                return False

        fleet.wait(realized_on_b, timeout=120.0, label="attachment realized")
        with sqlite3.connect(
            f"file:{fleet.machines[1].db_path}?mode=ro&immutable=1", uri=True
        ) as conn:
            file_path, stored_hash = conn.execute(
                "SELECT file_path,hash FROM attachments WHERE id='shared-att'"
            ).fetchone()
            backlog = conn.execute(
                "SELECT COUNT(*) FROM fleet_sync_quarantine"
            ).fetchone()[0]
        assert stored_hash == digest
        assert Path(file_path).read_bytes() == content
        assert Path(file_path).is_relative_to(
            fleet.machines[1].db_path.parent / "uploads" / "fleet"
        )
        assert backlog == 0
    finally:
        fleet.shutdown()


def test_backlog_lists_only_replayable_attachment_entries(
    tmp_path: Path,
) -> None:
    source = GraphDB(tmp_path / "source.db")
    target = GraphDB(tmp_path / "target.db")
    try:
        left = MutationCatalog(source.conn, "machine-a")
        right = MutationCatalog(target.conn, "machine-b")
        left.install()
        right.install()
        content = b"backlog-bytes"
        digest = hashlib.sha256(content).hexdigest()
        payload = tmp_path / "obj.png"
        payload.write_bytes(content)
        with left.transaction(100, "tx-1"):
            _insert_attachment(
                source.conn, "att-b", digest, len(content), payload
            )
        page = left.next_transactions_for_origin("machine-a", 0, None, limit=1)
        assert page
        served = (page[0][0], page[0][3])
        assert right.apply_remote_batch(served[1]) == (0, 0)
        entries = pending_attachment_backlog(target.conn)
        assert [e.digest for e in entries] == [digest]
        entry = entries[0]
        assert entry.size == len(content)
        assert entry.origin == "machine-a"
        assert entry.transaction_id == "tx-1"
    finally:
        source.close()
        target.close()
