from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from tools.network.registry.deploy.registry_snapshot import snapshot, verify


def _wal_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE orgs (id TEXT PRIMARY KEY)")
    connection.execute("CREATE TABLE links (token TEXT PRIMARY KEY, org TEXT)")
    connection.executemany("INSERT INTO orgs VALUES (?)", [("a",), ("b",)])
    connection.executemany(
        "INSERT INTO links VALUES (?, ?)", [("secret-1", "a"), ("secret-2", "b")]
    )
    connection.commit()
    return connection


def test_snapshot_includes_committed_wal_and_is_standalone(tmp_path: Path) -> None:
    live_path = tmp_path / "live.db"
    live = _wal_database(live_path)
    try:
        assert live_path.with_name("live.db-wal").stat().st_size > 0
        destination = tmp_path / "snapshot.db"
        metadata = tmp_path / "manifest.json"
        facts = snapshot(live_path, destination, metadata)
    finally:
        live.close()

    assert not destination.with_name("snapshot.db-wal").exists()
    restored = sqlite3.connect(destination)
    try:
        assert restored.execute("SELECT COUNT(*) FROM orgs").fetchone() == (2,)
        assert restored.execute("SELECT COUNT(*) FROM links").fetchone() == (2,)
    finally:
        restored.close()
    assert facts["tables"] == {"links": 2, "orgs": 2}
    assert json.loads(metadata.read_text())["sha256"] == facts["sha256"]


def test_verify_detects_manifest_mismatch(tmp_path: Path) -> None:
    live_path = tmp_path / "live.db"
    live = _wal_database(live_path)
    destination = tmp_path / "snapshot.db"
    metadata = tmp_path / "manifest.json"
    try:
        snapshot(live_path, destination, metadata)
    finally:
        live.close()

    expected = json.loads(metadata.read_text())
    expected["tables"]["links"] = 999
    metadata.write_text(json.dumps(expected))
    with pytest.raises(RuntimeError, match="metadata mismatch for tables"):
        verify(destination, metadata)


def test_snapshot_refuses_to_overwrite_live_database(tmp_path: Path) -> None:
    live_path = tmp_path / "live.db"
    live = _wal_database(live_path)
    try:
        with pytest.raises(ValueError, match="must differ"):
            snapshot(live_path, live_path, tmp_path / "manifest.json")
    finally:
        live.close()
