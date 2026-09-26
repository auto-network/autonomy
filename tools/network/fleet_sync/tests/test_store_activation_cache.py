"""The scheduler's per-scope activation cache is by path, and a path can be
a new file: `graph follow remove` then `graph follow add` deletes and
recreates the followed mirror under the same name. The cache must not stand
in for triggers the new file does not have (Windows signed-release node,
2026-09-26: every follow round failed with "fleet writers are not active")."""

from __future__ import annotations

from pathlib import Path

from tools.graph.db import GraphDB
from tools.network import fleet_sync_scheduler as fss
from tools.network.fleet_sync.compaction import WatermarkError
from tools.network.idkit import KeyPair

ORG = "e7d4c5a6-0000-4000-8000-000000000001"


def _mirror(path: Path) -> None:
    GraphDB.create_org_db("autonomy", type_="followed", org_id=ORG, path=path).close()


def _scheduler(tmp_path: Path, mirror: Path) -> fss.FleetSyncScheduler:
    return fss.FleetSyncScheduler(fss.FleetSyncRuntimeConfig(
        machine_key=KeyPair.generate(), personal_root_pub=KeyPair.generate().public_hex,
        roster_entries=lambda: (), peer_addresses=lambda: {},
        personal_db_path=tmp_path / "personal.db", poll_interval=60.0,
        sync_scopes=lambda: {"autonomy": mirror},
    ))


def test_a_mirror_recreated_under_the_same_path_is_activated_again(tmp_path):
    mirror = tmp_path / "orgs" / "autonomy.db"
    mirror.parent.mkdir()
    _mirror(mirror)
    scheduler = _scheduler(tmp_path, mirror)
    assert scheduler._store_for("autonomy").follow_cursor() is None
    assert mirror in scheduler._activated_scopes
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(mirror) + suffix)
        if p.exists():
            p.unlink()
    _mirror(mirror)                                    # graph follow remove + add
    assert not fss._store_carries_capture(mirror)
    store = scheduler._store_for("autonomy")           # re-activates instead of trusting the cache
    assert store.follow_cursor() is None
    assert fss._store_carries_capture(mirror)


def test_the_inactive_store_error_names_the_file(tmp_path):
    path = tmp_path / "orgs" / "autonomy.db"
    path.parent.mkdir()
    _mirror(path)
    store = fss.SQLiteFleetSyncStore(path)
    try:
        store.follow_cursor()
    except WatermarkError as exc:
        assert str(path) in str(exc)
    else:
        raise AssertionError("an inactive store opened without the catalog")
