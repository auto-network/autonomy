from __future__ import annotations

import sqlite3

import pytest

from tools.dashboard.dao import trusted_git_object_store as store


def test_trusted_git_object_store_schema_created(tmp_path):
    path = tmp_path / "trusted_git_object_store.db"
    store.init_db(path)

    conn = store._get_conn(path)
    try:
        snapshot_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(trusted_git_object_snapshots)").fetchall()
        }
        assert snapshot_cols == {
            "snapshot_ref",
            "workflow_id",
            "repo_slug",
            "commit_sha",
            "tree_sha",
            "parent_shas_json",
            "manifest_sha256",
            "canonical_preview_sha256",
            "snapshot_type",
            "status",
            "captured_at",
            "captured_by",
            "store_root",
            "latest_integrity_at",
            "latest_integrity_status",
            "retention_class",
            "retention_expires_at",
        }

        snapshot_schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='trusted_git_object_snapshots'"
        ).fetchone()["sql"]
        assert "snapshot_type IN ('commit_create', 'rewrite_source', 'rewrite_result')" in snapshot_schema
        assert "status IN ('captured', 'verified', 'gc_pending', 'released', 'gc_deleted')" in snapshot_schema
        assert "captured_by IN ('dashboard', 'reconciler', 'broker')" in snapshot_schema
        assert "retention_class IN ('active', 'signing_pending', 'published', 'terminal')" in snapshot_schema

        entry_cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(trusted_git_object_entries)").fetchall()
        }
        assert entry_cols == {
            "snapshot_ref",
            "object_oid",
            "object_type",
            "object_size",
            "object_path",
            "object_sha256",
            "position",
        }

        entry_schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='trusted_git_object_entries'"
        ).fetchone()["sql"]
        assert "object_type IN ('blob', 'tree', 'commit', 'tag')" in entry_schema
        assert "FOREIGN KEY(snapshot_ref) REFERENCES trusted_git_object_snapshots(snapshot_ref)" in entry_schema

        conn.execute(
            """
            INSERT INTO trusted_git_object_snapshots (
                snapshot_ref, workflow_id, repo_slug, commit_sha, tree_sha,
                parent_shas_json, manifest_sha256, canonical_preview_sha256,
                snapshot_type, status, captured_at, captured_by, store_root,
                latest_integrity_at, latest_integrity_status, retention_class,
                retention_expires_at
            ) VALUES (
                'snapshot-1', 'wf-1', 'autonomy', 'commit-1', 'tree-1',
                '[]', 'manifest-1', 'preview-1', 'commit_create', 'captured',
                1.0, 'dashboard', 'store-root', NULL, NULL, 'active', 10.0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO trusted_git_object_entries (
                snapshot_ref, object_oid, object_type, object_size,
                object_path, object_sha256, position
            ) VALUES (
                'snapshot-1', 'oid-1', 'tree', 123, 'objects/tree', 'sha-1', 0
            )
            """
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO trusted_git_object_entries (
                    snapshot_ref, object_oid, object_type, object_size,
                    object_path, object_sha256, position
                ) VALUES (
                    'snapshot-1', 'oid-1', 'tree', 124, 'objects/tree2', 'sha-2', 1
                )
                """
            )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO trusted_git_object_snapshots (
                    snapshot_ref, workflow_id, repo_slug, commit_sha, tree_sha,
                    parent_shas_json, manifest_sha256, canonical_preview_sha256,
                    snapshot_type, status, captured_at, captured_by, store_root,
                    latest_integrity_at, latest_integrity_status, retention_class,
                    retention_expires_at
                ) VALUES (
                    'snapshot-2', 'wf-1', 'autonomy', 'commit-2', 'tree-2',
                    '[]', 'manifest-2', 'preview-2', 'unsupported', 'captured',
                    2.0, 'dashboard', 'store-root', NULL, NULL, 'active', 20.0
                )
                """
            )

    finally:
        conn.close()
