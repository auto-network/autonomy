from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_checkpoint_handoff import (
    install_quiesced_checkpoint,
    recover_checkpoint_handoff,
)
import tools.network.fleet_checkpoint_handoff as handoff_module
from tools.network.fleet_sync_connection import (
    FleetSyncQuiescenceError,
    acquire_database_quiescence,
)
from tools.network.fleet_sync_sim.alpha import (
    AlphaError,
    FleetSyncAlpha,
    install_checkpoint,
    transport_checkpoint_via_raptorq,
)


def _source(conn, identity: str, title: str) -> None:
    conn.execute(
        "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
        "VALUES(?,?,?,?,?,?)",
        (
            identity,
            "note",
            title,
            "{}",
            "2026-08-21T00:00:00Z",
            "2026-08-21T00:00:00Z",
        ),
    )


def test_quiescence_refuses_live_writer_and_new_connections(tmp_path: Path) -> None:
    path = tmp_path / "personal.db"
    db = GraphDB(path)
    with pytest.raises(FleetSyncQuiescenceError, match="live production"):
        acquire_database_quiescence(path)
    db.close()

    with acquire_database_quiescence(path):
        with pytest.raises(FleetSyncQuiescenceError, match="quiesced"):
            GraphDB(path)
    reopened = GraphDB(path)
    reopened.close()


def test_quiesced_multisegment_checkpoint_merges_local_winners(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.db"
    target_path = tmp_path / "target.db"
    checkpoint = tmp_path / "checkpoint"
    received = tmp_path / "received"
    epoch = "a" * 64
    roster = ("machine-a", "machine-b")

    with FleetSyncAlpha(source_path, "machine-a") as source:
        with source.author(100, "source-seed"):
            for index in range(90):
                _source(source.graph.conn, f"remote-{index:03d}", f"remote {index}")
            _source(source.graph.conn, "shared", "older remote")
            _source(source.graph.conn, "gone", "temporary")
        with source.author(200, "source-delete"):
            source.graph.conn.execute("DELETE FROM sources WHERE id='gone'")
        source.checkpoint(
            checkpoint,
            roster_epoch=epoch,
            active_roster=roster,
            target_chunk_bytes=4096,
        )

    manifest = json.loads((checkpoint / "alpha-manifest.json").read_bytes())
    assert len(manifest["base"]["chunks"]) > 1
    transport_checkpoint_via_raptorq(checkpoint, received, symbol_size=256)

    with FleetSyncAlpha(target_path, "machine-b") as target:
        with target.author(300, "local-divergence"):
            _source(target.graph.conn, "shared", "newer local")
            _source(target.graph.conn, "local-only", "preserved")

    with acquire_database_quiescence(target_path) as token:
        installed = install_quiesced_checkpoint(
            received,
            target_path,
            quiescence=token,
            target_origin_incarnation="machine-b",
            expected_roster_epoch=epoch,
            expected_active_roster=roster,
        )
    assert installed.watermark == 200
    with FleetSyncAlpha(target_path, "machine-b") as target:
        rows = dict(target.graph.conn.execute(
            "SELECT id,title FROM sources ORDER BY id"
        ).fetchall())
        assert rows["shared"] == "newer local"
        assert rows["local-only"] == "preserved"
        assert rows["remote-089"] == "remote 89"
        assert "gone" not in rows


def test_handoff_recovers_before_and_after_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source.db"
    checkpoint = tmp_path / "checkpoint"
    epoch = "b" * 64
    roster = ("machine-a", "machine-b")
    with FleetSyncAlpha(source_path, "machine-a") as source:
        with source.author(100, "seed"):
            _source(source.graph.conn, "remote", "arrived")
        source.checkpoint(
            checkpoint, roster_epoch=epoch, active_roster=roster
        )

    before_target = tmp_path / "before.db"
    with FleetSyncAlpha(before_target, "machine-b") as target:
        with target.author(200, "local"):
            _source(target.graph.conn, "local", "stays")

    def crash_before(*_args, **_kwargs):
        raise SystemExit("crash before swap")

    monkeypatch.setattr(handoff_module, "install_checkpoint", crash_before)
    with acquire_database_quiescence(before_target) as token:
        with pytest.raises(SystemExit, match="before swap"):
            install_quiesced_checkpoint(
                checkpoint,
                before_target,
                quiescence=token,
                target_origin_incarnation="machine-b",
                expected_roster_epoch=epoch,
                expected_active_roster=roster,
            )
        assert recover_checkpoint_handoff(
            before_target, quiescence=token
        ) == "clean"
    with FleetSyncAlpha(before_target, "machine-b") as target:
        assert target.graph.conn.execute(
            "SELECT title FROM sources WHERE id='local'"
        ).fetchone()[0] == "stays"

    after_target = tmp_path / "after.db"
    real_install = install_checkpoint

    def crash_after(*args, **kwargs):
        real_install(*args, **kwargs)
        raise SystemExit("crash after swap")

    monkeypatch.setattr(handoff_module, "install_checkpoint", crash_after)
    with acquire_database_quiescence(after_target) as token:
        with pytest.raises(SystemExit, match="after swap"):
            install_quiesced_checkpoint(
                checkpoint,
                after_target,
                quiescence=token,
                target_origin_incarnation="machine-b",
                expected_roster_epoch=epoch,
                expected_active_roster=roster,
            )
        assert recover_checkpoint_handoff(
            after_target, quiescence=token
        ) == "published"
    with FleetSyncAlpha(after_target, "machine-b") as target:
        assert target.graph.conn.execute(
            "SELECT title FROM sources WHERE id='remote'"
        ).fetchone()[0] == "arrived"

    manifest_path = checkpoint / "alpha-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["alpha_version"] = "incompatible"
    manifest_path.write_text(json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ))
    monkeypatch.setattr(handoff_module, "install_checkpoint", real_install)
    with acquire_database_quiescence(before_target) as token:
        with pytest.raises(AlphaError, match="unsupported alpha checkpoint"):
            install_quiesced_checkpoint(
                checkpoint,
                before_target,
                quiescence=token,
                target_origin_incarnation="machine-b",
                expected_roster_epoch=epoch,
                expected_active_roster=roster,
            )
    with FleetSyncAlpha(before_target, "machine-b") as target:
        assert target.graph.conn.execute(
            "SELECT title FROM sources WHERE id='local'"
        ).fetchone()[0] == "stays"
