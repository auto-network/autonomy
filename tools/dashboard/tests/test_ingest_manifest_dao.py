"""Tests for the W4 ingest_manifest DAO layer (auto-gah4g).

Covers the L1 matrix the bead's AC calls for: stat-skip round-trip,
sealing, and the active/sealed path-set queries the sweep uses to detect
files that vanished.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def ddb(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_DB", str(tmp_path / "dashboard.db"))
    from tools.dashboard.dao import dashboard_db as mod
    importlib.reload(mod)
    return mod


class TestManifestRoundTrip:
    def test_missing_entry_returns_none(self, ddb):
        assert ddb.get_manifest_entry("/tmp/nope.jsonl") is None

    def test_upsert_then_get_round_trips(self, ddb):
        ddb.upsert_manifest_entry(
            "/tmp/a.jsonl", size=100, mtime=1000.5, inode=42,
            org="autonomy", source_id="src-1", ingest_offset=100,
        )
        entry = ddb.get_manifest_entry("/tmp/a.jsonl")
        assert entry["size"] == 100
        assert entry["mtime"] == 1000.5
        assert entry["inode"] == 42
        assert entry["org"] == "autonomy"
        assert entry["source_id"] == "src-1"
        assert entry["ingest_offset"] == 100
        assert entry["state"] == "active"

    def test_upsert_overwrites_existing_row(self, ddb):
        ddb.upsert_manifest_entry(
            "/tmp/a.jsonl", size=100, mtime=1000.0, inode=1,
            org="autonomy", source_id="src-1", ingest_offset=100,
        )
        ddb.upsert_manifest_entry(
            "/tmp/a.jsonl", size=200, mtime=2000.0, inode=1,
            org="autonomy", source_id="src-1", ingest_offset=200,
        )
        entry = ddb.get_manifest_entry("/tmp/a.jsonl")
        assert entry["size"] == 200
        assert entry["mtime"] == 2000.0
        assert entry["ingest_offset"] == 200


class TestSealing:
    def test_seal_sets_state_sealed(self, ddb):
        ddb.upsert_manifest_entry(
            "/tmp/a.jsonl", size=100, mtime=1000.0, inode=1,
            org="autonomy", source_id="src-1", ingest_offset=100,
        )
        ddb.seal_manifest_entry("/tmp/a.jsonl")
        entry = ddb.get_manifest_entry("/tmp/a.jsonl")
        assert entry["state"] == "sealed"

    def test_seal_preserves_other_columns(self, ddb):
        ddb.upsert_manifest_entry(
            "/tmp/a.jsonl", size=100, mtime=1000.0, inode=1,
            org="autonomy", source_id="src-1", ingest_offset=100,
        )
        ddb.seal_manifest_entry("/tmp/a.jsonl")
        entry = ddb.get_manifest_entry("/tmp/a.jsonl")
        assert entry["size"] == 100
        assert entry["source_id"] == "src-1"

    def test_unseal_via_active_upsert(self, ddb):
        """A force re-ingest that upserts state='active' unseals a row —
        this is how catch_up_sweep(force=True) un-skips a sealed file."""
        ddb.upsert_manifest_entry(
            "/tmp/a.jsonl", size=100, mtime=1000.0, inode=1,
            org="autonomy", source_id="src-1", ingest_offset=100,
        )
        ddb.seal_manifest_entry("/tmp/a.jsonl")
        ddb.upsert_manifest_entry(
            "/tmp/a.jsonl", size=150, mtime=1500.0, inode=1,
            org="autonomy", source_id="src-1", ingest_offset=150, state="active",
        )
        entry = ddb.get_manifest_entry("/tmp/a.jsonl")
        assert entry["state"] == "active"


class TestPathSetQueries:
    def test_sealed_paths_empty_by_default(self, ddb):
        ddb.upsert_manifest_entry(
            "/tmp/a.jsonl", size=1, mtime=1.0, inode=1,
            org="autonomy", source_id="s", ingest_offset=1,
        )
        assert ddb.get_sealed_manifest_paths() == set()

    def test_sealed_paths_reflects_sealed_rows_only(self, ddb):
        ddb.upsert_manifest_entry(
            "/tmp/a.jsonl", size=1, mtime=1.0, inode=1,
            org="autonomy", source_id="s", ingest_offset=1,
        )
        ddb.upsert_manifest_entry(
            "/tmp/b.jsonl", size=1, mtime=1.0, inode=2,
            org="autonomy", source_id="s2", ingest_offset=1,
        )
        ddb.seal_manifest_entry("/tmp/a.jsonl")
        assert ddb.get_sealed_manifest_paths() == {"/tmp/a.jsonl"}

    def test_active_paths_excludes_sealed(self, ddb):
        ddb.upsert_manifest_entry(
            "/tmp/a.jsonl", size=1, mtime=1.0, inode=1,
            org="autonomy", source_id="s", ingest_offset=1,
        )
        ddb.upsert_manifest_entry(
            "/tmp/b.jsonl", size=1, mtime=1.0, inode=2,
            org="autonomy", source_id="s2", ingest_offset=1,
        )
        ddb.seal_manifest_entry("/tmp/a.jsonl")
        assert ddb.get_active_manifest_paths() == {"/tmp/b.jsonl"}
