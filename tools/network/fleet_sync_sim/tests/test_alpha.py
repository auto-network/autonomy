from pathlib import Path
import json

import pytest
import tools.network.fleet_sync_sim.alpha as alpha_module

from tools.graph.db import GraphDB
from tools.network.fleet_sync_sim.alpha import (
    ALPHA_VERSION,
    AlphaError,
    FleetSyncAlpha,
    install_checkpoint,
    transport_checkpoint_via_raptorq,
)
from tools.network.fleet_sync_sim.compaction import WatermarkError
from tools.network.fleet_sync_sim.streaming import StreamingCodecError


def _source(conn, identity: str, title: str) -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
        "VALUES(?,?,?,?,?,?)",
        (identity, "note", title, "{}", "2026-08-19T00:00:00Z",
         "2026-08-19T00:00:00Z"),
    )


def _thought(conn, identity: str, source_id: str) -> None:
    conn.execute(
        "INSERT INTO thoughts(id,source_id,content,turn_number,created_at) "
        "VALUES(?,?,?,?,?)",
        (identity, source_id, "body", 0, "2026-08-19T00:00:00Z"),
    )


def _attachment(conn, identity: str, source_id: str) -> None:
    conn.execute(
        "INSERT INTO attachments"
        "(id,hash,filename,size_bytes,file_path,source_id,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (identity, "hash-" + identity, identity + ".png", 10,
         "data/attachments/xx/" + identity + ".png", source_id,
         "2026-08-19T00:00:00Z"),
    )


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


def test_alpha_checkpoint_installs_atomically_and_restarts(tmp_path: Path) -> None:
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    _identity(origin_path, "origin-secret")
    _identity(target_path, "target-secret")
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(100, "tx-1"):
            _source(origin.graph.conn, "live", "carried")
            _source(origin.graph.conn, "gone", "temporary")
        with origin.author(110, "tx-2"):
            origin.graph.conn.execute("DELETE FROM sources WHERE id='gone'")
        checkpoint = origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=7,
            active_roster=("machine-a", "machine-b"), target_chunk_bytes=4096,
        )
    assert checkpoint.alpha_version == ALPHA_VERSION
    assert checkpoint.watermark == 110
    # Full checkpoints carry one current winner/tombstone per address, not
    # retired transaction history (live + gone tombstone here).
    assert checkpoint.winner_records == 2

    installed = install_checkpoint(
        tmp_path / "checkpoint", target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=7,
        expected_active_roster=("machine-a", "machine-b"),
    )
    assert installed.manifest_sha256 == checkpoint.manifest_sha256
    with FleetSyncAlpha(target_path, "machine-b") as target:
        assert [tuple(row) for row in target.graph.conn.execute(
            "SELECT id,title FROM sources"
        ).fetchall()] == [("live", "carried")]
        assert [tuple(row) for row in target.graph.conn.execute(
            "SELECT id FROM settings WHERE set_id='autonomy.identity.personal'"
        ).fetchall()] == [("target-secret",)]
        assert [tuple(row) for row in target.graph.conn.execute(
            "SELECT slug,type FROM orgs"
        ).fetchall()] == [("personal", "personal")]
        forwarded = list(target.catalog.iter_mutations())
        assert {item.origin_incarnation for item in forwarded} == {"machine-a"}
        assert {item.mutation.address for item in forwarded} == {("live",), ("gone",)}
        with pytest.raises(WatermarkError, match="write refused"):
            with target.author(110, "too-old"):
                pass
        with target.author(111, "after-restart"):
            target.graph.conn.execute(
                "UPDATE sources SET title='new local write' WHERE id='live'"
            )


def test_alpha_refuses_wrong_roster_epoch_without_touching_target(
    tmp_path: Path,
) -> None:
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(1, "tx"):
            _source(origin.graph.conn, "s1", "one")
        origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=3,
            active_roster=("machine-a", "machine-b"),
        )
    before = target_path.exists()
    with pytest.raises(AlphaError, match="roster epoch"):
        install_checkpoint(
            tmp_path / "checkpoint", target_path,
            target_origin_incarnation="machine-b", expected_roster_epoch=4,
            expected_active_roster=("machine-a", "machine-b"),
        )
    assert target_path.exists() is before


def test_full_checkpoint_survives_retired_transaction_journal(
    tmp_path: Path,
) -> None:
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(10, "old-history"):
            _source(origin.graph.conn, "stable", "current winner")
        assert origin.catalog.prune_journal(10) == 1
        checkpoint = origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=2,
            active_roster=("machine-a", "machine-b"), target_chunk_bytes=4096,
        )
        assert checkpoint.winner_records == 1
    install_checkpoint(
        tmp_path / "checkpoint", target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=2,
        expected_active_roster=("machine-a", "machine-b"),
    )
    with FleetSyncAlpha(target_path, "machine-b") as target:
        assert target.graph.conn.execute(
            "SELECT title FROM sources WHERE id='stable'"
        ).fetchone()[0] == "current winner"
        relayed = list(target.catalog.iter_mutations())
        assert len(relayed) == 1
        assert relayed[0].mutation.timestamp_ns == 10


def test_checkpoint_refuses_active_roster_mismatch(tmp_path: Path) -> None:
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(1, "tx"):
            _source(origin.graph.conn, "s1", "one")
        origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=3,
            active_roster=("machine-a", "machine-b"),
        )
    with pytest.raises(AlphaError, match="active roster"):
        install_checkpoint(
            tmp_path / "checkpoint", target_path,
            target_origin_incarnation="machine-c", expected_roster_epoch=3,
            expected_active_roster=("machine-a", "machine-c"),
        )


def test_failed_install_preserves_target_and_removes_staging(
    tmp_path: Path,
) -> None:
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    _identity(target_path, "target-secret")
    before = target_path.read_bytes()
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(1, "tx"):
            _source(origin.graph.conn, "s1", "one")
        origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=3,
            active_roster=("machine-a", "machine-b"),
            target_chunk_bytes=4096,
        )
    chunk = next((tmp_path / "checkpoint" / "base").glob("*.base"))
    damaged = bytearray(chunk.read_bytes())
    damaged[-1] ^= 1
    chunk.write_bytes(damaged)
    with pytest.raises(StreamingCodecError, match="digest"):
        install_checkpoint(
            tmp_path / "checkpoint", target_path,
            target_origin_incarnation="machine-b", expected_roster_epoch=3,
            expected_active_roster=("machine-a", "machine-b"),
        )
    assert target_path.read_bytes() == before
    assert not list(tmp_path.glob(".fleet-sync-install-*"))


def test_transport_refuses_noncanonical_artifact_path_without_partial_target(
    tmp_path: Path,
) -> None:
    origin_path = tmp_path / "origin.db"
    checkpoint_path = tmp_path / "checkpoint"
    target = tmp_path / "received"
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(1, "tx"):
            _source(origin.graph.conn, "s1", "one")
        origin.checkpoint(
            checkpoint_path, roster_epoch=3,
            active_roster=("machine-a", "machine-b"),
            target_chunk_bytes=4096,
        )
    manifest_path = checkpoint_path / "alpha-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["base"]["chunks"][0]["filename"] = "../../outside"
    manifest_path.write_text(json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ))
    with pytest.raises(AlphaError, match="filename"):
        transport_checkpoint_via_raptorq(
            checkpoint_path, target, symbol_size=256
        )
    assert not target.exists()
    assert not (tmp_path / "outside").exists()


def test_failed_checkpoint_build_publishes_no_partial_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin_path = tmp_path / "origin.db"
    checkpoint = tmp_path / "checkpoint"
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(1, "tx"):
            _source(origin.graph.conn, "s1", "one")

        def fail(*_args, **_kwargs):
            raise RuntimeError("injected winner failure")

        monkeypatch.setattr(alpha_module, "stream_winners_to_chunks", fail)
        with pytest.raises(RuntimeError, match="injected winner failure"):
            origin.checkpoint(
                checkpoint, roster_epoch=3,
                active_roster=("machine-a", "machine-b"),
                target_chunk_bytes=4096,
            )
    assert not checkpoint.exists()
    assert not list(tmp_path.glob(".fleet-sync-checkpoint-*"))


def test_checkpoint_install_skips_and_quarantines_foreign_key_orphans(
    tmp_path: Path,
) -> None:
    """A thought whose source was deleted long ago without cascade — legacy
    foreign-key-off debris — must not abort the whole checkpoint. The receiver
    skips it, keeps the catalog exactly consistent with the rows that landed,
    and quarantines it for later repair."""
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    _identity(origin_path, "origin-secret")
    _identity(target_path, "target-secret")
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        conn = origin.graph.conn
        with origin.author(100, "tx-good"):
            _source(conn, "src-live", "kept")
            _thought(conn, "t-good", "src-live")
        # Simulate historical debris: a thought whose parent source is absent,
        # still tracked by the catalog (SQLite shipped with foreign keys off, so
        # such orphans accumulated unnoticed and now ride the checkpoint).
        conn.execute("PRAGMA foreign_keys=OFF")
        with origin.author(105, "tx-orphan"):
            _thought(conn, "t-orphan", "src-missing")
        conn.execute("PRAGMA foreign_keys=ON")
        checkpoint = origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=7,
            active_roster=("machine-a", "machine-b"), target_chunk_bytes=4096,
        )
    # The origin tracked the orphan, so it is part of the checkpoint's base.
    assert checkpoint.winner_records == 3  # src-live, t-good, t-orphan

    installed = install_checkpoint(
        tmp_path / "checkpoint", target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=7,
        expected_active_roster=("machine-a", "machine-b"),
    )
    assert installed.manifest_sha256 == checkpoint.manifest_sha256
    with FleetSyncAlpha(target_path, "machine-b") as target:
        tconn = target.graph.conn
        # The orphan was skipped; the representable rows landed intact.
        assert [r[0] for r in tconn.execute(
            "SELECT id FROM thoughts ORDER BY id"
        )] == ["t-good"]
        assert [r[0] for r in tconn.execute(
            "SELECT id FROM sources ORDER BY id"
        )] == ["src-live"]
        # The catalog is exactly consistent with what landed — the orphan is
        # neither a data row nor a live catalog entry.
        live = tconn.execute(
            "SELECT COUNT(*) FROM fleet_sync_catalog WHERE tombstone=0"
        ).fetchone()[0]
        assert live == 2  # src-live + t-good
        # The retained skip delta is the quarantine row count — a later canary
        # reads COUNT(*) here instead of re-scanning foreign keys.
        quarantined = [tuple(r) for r in tconn.execute(
            "SELECT table_name,logical_address,reason FROM fleet_sync_quarantine"
        )]
        assert quarantined == [
            ("thoughts", json.dumps(["t-orphan"]), "fk_orphan")
        ]


def test_checkpoint_install_quarantines_attachments_without_blob_bytes(
    tmp_path: Path,
) -> None:
    """When blob transfer is not wired (blob_store is None), an attachment's
    external bytes are unavailable and its NOT-NULL file_path cannot be filled.
    The install must not abort: the attachment row is skipped and quarantined
    for a later fetch, while the representable rows land and the catalog stays
    consistent."""
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    _identity(origin_path, "origin-secret")
    _identity(target_path, "target-secret")
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        conn = origin.graph.conn
        with origin.author(100, "tx-1"):
            _source(conn, "src", "kept")
            _thought(conn, "t1", "src")
            _attachment(conn, "att1", "src")
        checkpoint = origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=7,
            active_roster=("machine-a", "machine-b"), target_chunk_bytes=4096,
        )
    assert checkpoint.winner_records == 3  # src, t1, att1

    # No blob_store: attachment bytes are unavailable on the receiver.
    install_checkpoint(
        tmp_path / "checkpoint", target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=7,
        expected_active_roster=("machine-a", "machine-b"),
    )
    with FleetSyncAlpha(target_path, "machine-b") as target:
        tconn = target.graph.conn
        assert [r[0] for r in tconn.execute("SELECT id FROM sources")] == ["src"]
        assert [r[0] for r in tconn.execute("SELECT id FROM thoughts")] == ["t1"]
        # The attachment row was skipped (bytes unavailable), not installed.
        assert tconn.execute(
            "SELECT COUNT(*) FROM attachments"
        ).fetchone()[0] == 0
        # Catalog stays consistent with what landed: src + t1 live, att1 out.
        assert tconn.execute(
            "SELECT COUNT(*) FROM fleet_sync_catalog WHERE tombstone=0"
        ).fetchone()[0] == 2
        quarantined = [tuple(r) for r in tconn.execute(
            "SELECT table_name,logical_address,reason FROM fleet_sync_quarantine"
        )]
        assert quarantined == [
            ("attachments", json.dumps(["att1"]),
             "attachment_bytes_unavailable")
        ]


def test_preexisting_untracked_rows_fail_checkpoint_closed(tmp_path: Path) -> None:
    origin_path = tmp_path / "origin.db"
    graph = GraphDB(origin_path)
    try:
        _source(graph.conn, "legacy", "predates catalog")
        graph.conn.commit()
    finally:
        graph.close()

    checkpoint = tmp_path / "checkpoint"
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with pytest.raises(AlphaError, match="untracked logical rows"):
            origin.checkpoint(
                checkpoint, roster_epoch=3,
                active_roster=("machine-a", "machine-b"),
                target_chunk_bytes=4096,
            )
    assert not checkpoint.exists()
