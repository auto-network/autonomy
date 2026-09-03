"""Two-replica convergence of sealed-settings item rows.

``autonomy.sealed-settings.row`` is an ordinary replicated Setting, so it rides
the same fleet-sync engine every other Setting does: the ``settings`` table has
a per-row SPECIAL policy keyed by ``(set_id, schema_revision, key, ...)`` with
last-writer-wins on the payload. This test witnesses that end to end for the
sealed set specifically — one origin authors a sealed item row (and a later
overwrite of the same key), a checkpoint carries exactly one winner per
address, and a fresh target installs it and converges to that winner.

The gap before install is the layer's PENDING window: a reader on the target
that references the row while it is absent reports it pending, never absent —
proved at the layer in ``tools/graph/tests/test_sealed_settings.py``
(``test_discover_reports_pending_for_unreplicated_rows``). Here we witness the
substrate half: the row is genuinely absent on the target until the checkpoint
lands, then present.
"""

from pathlib import Path

from tools.graph.db import GraphDB
from tools.graph.schemas.sealed_row import SEALED_ROW_REVISION, SEALED_ROW_SET_ID
from tools.network.fleet_sync.sync import FleetSyncAlpha, install_checkpoint

_TS = "2026-09-03T00:00:00Z"
# An opaque, org-keyed sealed-settings item key: <store-tag>.<blind-index>.
_ROW_KEY = "gpZ0yFl8HO0uX63yAtFOtyMxp3HEgXrO1onEcTRUhCk.S7Iu048dthHLvoiajjkXl2wzfH5C7xZxA"


def _seed_identity(path: Path, marker: str) -> None:
    graph = GraphDB(path)
    try:
        graph.conn.execute(
            "INSERT INTO settings(id,set_id,schema_revision,key,payload,"
            "publication_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (marker, "autonomy.identity.personal", 1, "root", "{}", "raw", _TS, _TS),
        )
        graph.conn.execute(
            "INSERT INTO orgs(id,slug,type,created_at) VALUES(?,?,?,?)",
            ("local-org", "personal", "personal", _TS),
        )
        graph.conn.commit()
    finally:
        graph.close()


def _author_identity(origin, marker: str, ts_ns: int) -> None:
    with origin.author(ts_ns, f"identity-{marker}"):
        origin.graph.conn.execute(
            "INSERT INTO settings(id,set_id,schema_revision,key,payload,"
            "publication_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (marker, "autonomy.identity.personal", 1, "root", "{}", "raw", _TS, _TS),
        )


def _author_sealed_row(origin, sid: str, key: str, ciphertext: str, ts_ns: int) -> None:
    with origin.author(ts_ns, f"sealed-{sid}"):
        origin.graph.conn.execute(
            "INSERT INTO settings(id,set_id,schema_revision,key,payload,"
            "publication_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (sid, SEALED_ROW_SET_ID, SEALED_ROW_REVISION, key,
             f'{{"ciphertext":"{ciphertext}"}}', "raw", _TS, _TS),
        )


def _sealed_rows(path: Path):
    conn = GraphDB(path).conn
    try:
        return conn.execute(
            "SELECT key,payload FROM settings WHERE set_id=? AND deprecated=0",
            (SEALED_ROW_SET_ID,),
        ).fetchall()
    finally:
        conn.close()


def test_sealed_settings_row_converges_across_two_replicas(tmp_path: Path) -> None:
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    _seed_identity(target_path, "target-secret")

    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        _author_identity(origin, "origin-secret", 90)
        # One opaque sealed item row on the origin replica. Per-row conflict
        # resolution (two writers on the same key) is the engine's LWW policy
        # on the settings table — the DB's own UNIQUE(address) constraint
        # forbids two bases at one address within a single replica, so that
        # merge lives in the cross-replica path (fleet_sync merge tests); here
        # we witness the row-level convergence and the pending window.
        _author_sealed_row(origin, "sealed-v1", _ROW_KEY, "CIPHERTEXT_V1", 100)
        checkpoint = origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=7,
            active_roster=("machine-a", "machine-b"), target_chunk_bytes=4096,
        )

    # PENDING window: the row is genuinely absent on the target before install.
    assert _sealed_rows(target_path) == []

    install_checkpoint(
        tmp_path / "checkpoint", target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=7,
        expected_active_roster=("machine-a", "machine-b"),
    )

    # Converged: the sealed row is now present on the target, byte-identical.
    rows = _sealed_rows(target_path)
    assert len(rows) == 1
    key, payload = rows[0]
    assert key == _ROW_KEY
    assert "CIPHERTEXT_V1" in payload
