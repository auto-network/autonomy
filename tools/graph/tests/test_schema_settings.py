"""Schema migration tests for the settings primitive.

Verifies the table + indices land on fresh DBs, are idempotent on reopen,
and have no impact on adjacent tables (Settings is a NEW primitive, not a
Notes refactor). Spec: graph://0d3f750f-f9c.
"""

from __future__ import annotations

import sqlite3

import pytest

from tools.graph.db import GraphDB
from tools.graph.models import Source


@pytest.fixture
def fresh_db(tmp_path):
    db = GraphDB(tmp_path / "graph.db")
    yield db
    db.close()


def test_fresh_db_has_settings_table(fresh_db):
    row = fresh_db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='settings'"
    ).fetchone()
    assert row is not None


def test_settings_columns_match_spec(fresh_db):
    cols = {r[1] for r in fresh_db.conn.execute("PRAGMA table_info(settings)").fetchall()}
    expected = {
        "id", "set_id", "schema_revision", "key", "payload",
        "publication_state", "supersedes", "excludes",
        "deprecated", "successor_id", "created_at", "updated_at",
    }
    assert expected <= cols


def test_settings_indices_present(fresh_db):
    rows = fresh_db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='settings'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_settings_set" in names
    assert "idx_settings_state" in names
    assert "idx_settings_schema" in names


def test_publication_state_check_constraint(fresh_db):
    """Invalid publication_state must be rejected by the CHECK."""
    with pytest.raises(sqlite3.IntegrityError):
        fresh_db.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, publication_state, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("s1", "x.y", 1, "k", "{}", "garbage", "2026-01-01", "2026-01-01"),
        )


def test_migration_is_idempotent(tmp_path):
    """Reopening the DB should not error or duplicate anything."""
    p = tmp_path / "graph.db"
    db = GraphDB(p)
    db.close()
    db = GraphDB(p)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(settings)").fetchall()}
    db.close()
    assert "set_id" in cols


def test_existing_data_unaffected(tmp_path):
    """Inserting a Source into a DB with the new table works exactly as before."""
    p = tmp_path / "graph.db"
    db = GraphDB(p)
    src = Source(
        type="note", platform="local", title="probe", file_path="note:probe",
        metadata={"tags": []},
    )
    db.insert_source(src)
    got = db.get_source(src.id)
    db.close()
    assert got is not None
    assert got["title"] == "probe"


def test_settings_round_trip(fresh_db):
    """A bare-metal INSERT/SELECT exercises the storage layer end-to-end."""
    fresh_db.conn.execute(
        "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
        "publication_state, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("abc", "autonomy.test", 1, "foo", '{"x": 1}', "raw",
         "2026-04-21T00:00:00Z", "2026-04-21T00:00:00Z"),
    )
    fresh_db.conn.commit()
    row = fresh_db.conn.execute(
        "SELECT * FROM settings WHERE id = 'abc'"
    ).fetchone()
    assert row["set_id"] == "autonomy.test"
    assert row["schema_revision"] == 1
    assert row["key"] == "foo"
    assert row["payload"] == '{"x": 1}'


# ── Workspace repo base_source field validation (auto-4sfe9) ────────


def _ws_payload(**repo_overrides):
    """Build an autonomy.workspace#1 payload with a single repo entry."""
    repo = {
        "host": "github.com",
        "repo": "autonomy/autonomy",
        "mount": "/workspace/repo",
    }
    repo.update(repo_overrides)
    return {
        "name": "autonomy",
        "image": "autonomy-agent:latest",
        "repos": [repo],
    }


def test_workspace_repo_accepts_omitted_base_source():
    from tools.graph.schemas.registry import SchemaValidationError  # noqa: F401
    from tools.graph.schemas.workspace import WorkspaceV1
    WorkspaceV1.validate(_ws_payload())


def test_workspace_repo_accepts_absolute_base_source():
    from tools.graph.schemas.workspace import WorkspaceV1
    WorkspaceV1.validate(_ws_payload(base_source="/home/user/autonomy"))


def test_workspace_repo_rejects_relative_base_source():
    from tools.graph.schemas.registry import SchemaValidationError
    from tools.graph.schemas.workspace import WorkspaceV1
    with pytest.raises(SchemaValidationError, match="base_source"):
        WorkspaceV1.validate(_ws_payload(base_source="relative/path"))


def test_workspace_repo_rejects_empty_base_source():
    from tools.graph.schemas.registry import SchemaValidationError
    from tools.graph.schemas.workspace import WorkspaceV1
    with pytest.raises(SchemaValidationError, match="base_source"):
        WorkspaceV1.validate(_ws_payload(base_source=""))


def test_workspace_repo_rejects_non_string_base_source():
    from tools.graph.schemas.registry import SchemaValidationError
    from tools.graph.schemas.workspace import WorkspaceV1
    with pytest.raises(SchemaValidationError, match="base_source"):
        WorkspaceV1.validate(_ws_payload(base_source=123))


def test_workspace_accepts_independent_nested_docker_runtime():
    from tools.graph.schemas.workspace import WorkspaceV1
    payload = _ws_payload()
    payload.update({
        "needs_nested_docker": True,
        "session_runtime": "sysbox",
    })
    WorkspaceV1.validate(payload)


def test_workspace_rejects_conflicting_legacy_dind_alias():
    from tools.graph.schemas.registry import SchemaValidationError
    from tools.graph.schemas.workspace import WorkspaceV1
    payload = _ws_payload()
    payload.update({"dind": True, "needs_nested_docker": False})
    with pytest.raises(SchemaValidationError, match="conflict"):
        WorkspaceV1.validate(payload)


@pytest.mark.parametrize("runtime", ["", "sysbox;touch-pwned", "has space"])
def test_workspace_rejects_unsafe_runtime_selector(runtime):
    from tools.graph.schemas.registry import SchemaValidationError
    from tools.graph.schemas.workspace import WorkspaceV1
    payload = _ws_payload()
    payload["session_runtime"] = runtime
    with pytest.raises(SchemaValidationError, match="session_runtime"):
        WorkspaceV1.validate(payload)
