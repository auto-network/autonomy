"""Dashboard startup flush of Setting schema metadata (bead auto-06ziz).

Materializing the ``autonomy.schema#1`` / ``autonomy.schema.synopsis#1``
meta rows that back ``graph set schema / example / find`` is decoupled from
``_SCHEMA_USER_VERSION``: it happens once at dashboard startup via
``flush_schema_meta_all_orgs``, invoked from the lifespan ``_on_startup``
hook. These tests drive the real (non-mock) lifespan and assert a schema
registered *after* the org DB was created lands with no version bump and no
full table re-init.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from tools.graph import schemas
from tools.graph.db import GraphDB, _SCHEMA_USER_VERSION
from tools.graph.schemas.registry import (
    SCHEMAS,
    UPCONVERTERS,
    SCHEMA_META_SET_ID,
    SYNOPSIS_META_SET_ID,
)


@pytest.fixture(autouse=True)
def _isolate_schema_registry():
    """Keep a per-test demo schema from leaking into other tests."""
    schemas_snap = dict(SCHEMAS)
    upcon_snap = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(schemas_snap)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upcon_snap)


def _build_demo_schema() -> type:
    class _DemoStartupSchema(schemas.SettingSchema):
        set_id = "autonomy.test.startupdemo"
        schema_revision = 1

        _field_metadata = {
            "label": {"type": "string", "required": True, "description": "d"},
        }
    return _DemoStartupSchema


def test_lifespan_startup_materializes_registered_schema(
    test_app, tmp_path, monkeypatch
):
    """Booting the dashboard lifespan flushes the live registry into every
    org DB — a schema registered after DB creation lands with no
    ``_SCHEMA_USER_VERSION`` bump."""
    orgs_dir = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()

    # Org DB exists before startup; ensure_bootstrap_orgs would create it
    # anyway, but creating it here proves the schema was NOT present at
    # creation time (the fresh-init flush is gone).
    autonomy = GraphDB.create_org_db("autonomy")
    db_path = autonomy.db_path
    autonomy.close()
    GraphDB.close_all_pooled()

    # Register a fresh schema only now — mimics a code change picked up on
    # the hot-reload restart.
    _build_demo_schema()

    # The lifespan (__enter__) runs _on_startup, which calls
    # flush_schema_meta_all_orgs.
    with TestClient(test_app):
        pass
    GraphDB.close_all_pooled()

    reopened = GraphDB(str(db_path), mode="rw")
    try:
        assert reopened.conn.execute(
            "PRAGMA user_version"
        ).fetchone()[0] == _SCHEMA_USER_VERSION
        rows = {
            r[0] for r in reopened.conn.execute(
                f"SELECT key FROM settings WHERE set_id = '{SCHEMA_META_SET_ID}'"
            ).fetchall()
        }
        assert "autonomy.test.startupdemo#1" in rows, (
            "startup flush did not materialize the newly registered schema"
        )
    finally:
        reopened.close()
        GraphDB.close_all_pooled()


def test_lifespan_startup_materializes_synopsis(test_app, tmp_path, monkeypatch):
    """Editing only a module SYNOPSIS is a third route that must land at
    startup too — it is what ``graph set find`` ranks on."""
    orgs_dir = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()

    autonomy = GraphDB.create_org_db("autonomy")
    db_path = autonomy.db_path
    autonomy.close()
    GraphDB.close_all_pooled()

    demo_cls = _build_demo_schema()
    import sys as _sys
    mod = _sys.modules[demo_cls.__module__]
    monkeypatch.setattr(
        mod,
        "SYNOPSIS",
        {"summary": "startup syn", "nouns": ["startupsyn"], "related_set_ids": []},
        raising=False,
    )

    with TestClient(test_app):
        pass
    GraphDB.close_all_pooled()

    reopened = GraphDB(str(db_path), mode="rw")
    try:
        rows = {
            r[0] for r in reopened.conn.execute(
                f"SELECT key FROM settings WHERE set_id = '{SYNOPSIS_META_SET_ID}'"
            ).fetchall()
        }
        assert "autonomy.test.startupdemo#1" in rows
    finally:
        reopened.close()
        GraphDB.close_all_pooled()
