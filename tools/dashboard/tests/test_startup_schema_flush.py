"""Dashboard startup flush of Setting schema metadata (auto-06ziz, auto-n77vh).

Materializing the ``autonomy.schema#1`` / ``autonomy.schema.synopsis#1``
meta rows that back ``graph set schema / example / find`` is decoupled from
``_SCHEMA_USER_VERSION``: it happens once at dashboard startup via
``flush_schema_meta_machine_store``, invoked from the lifespan
``_on_startup`` hook — and it lands in the MACHINE store, never in an
organization's database. Schema metadata is a projection of the code this
process runs: two machines on different versions have no single true answer
per organization, so flushing into shared org databases made whoever
restarted last win, and wrote the one row family that can never be signed.
These tests drive the real (non-mock) lifespan and assert a schema
registered *after* the DBs were created lands in the machine store with no
version bump, while the organization database ends the boot holding ZERO
schema-meta rows — including rows a pre-n77vh dashboard already wrote there,
which startup sweeps out.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from tools.graph import schemas
from tools.graph.db import GraphDB, _SCHEMA_USER_VERSION, _org_db_path
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


def test_lifespan_startup_materializes_registered_schema_in_machine_store(
    test_app, tmp_path, monkeypatch
):
    """Booting the dashboard lifespan flushes the live registry into the
    machine store — a schema registered after DB creation lands with no
    ``_SCHEMA_USER_VERSION`` bump — and sweeps schema-meta rows a previous
    code version left in an organization database."""
    orgs_dir = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()

    # Org DB exists before startup; ensure_bootstrap_orgs would create it
    # anyway, but creating it here proves the schema was NOT present at
    # creation time (the fresh-init flush is gone) — and lets us plant the
    # stale rows a pre-n77vh dashboard wrote into it.
    autonomy = GraphDB.create_org_db("autonomy")
    org_db_path = autonomy.db_path
    autonomy.conn.execute(
        "INSERT INTO settings (id, set_id, schema_revision, key, payload,"
        " publication_state) VALUES ('stale-n77vh-probe', ?, 1,"
        " 'stale.legacy#1', '{}', 'canonical')",
        (SCHEMA_META_SET_ID,),
    )
    autonomy.conn.commit()
    autonomy.close()
    GraphDB.close_all_pooled()

    # Register a fresh schema only now — mimics a code change picked up on
    # the hot-reload restart.
    _build_demo_schema()

    # The lifespan (__enter__) runs _on_startup, which calls
    # flush_schema_meta_machine_store.
    with TestClient(test_app):
        pass
    GraphDB.close_all_pooled()

    machine = GraphDB(str(_org_db_path("machine")), mode="rw")
    try:
        assert machine.conn.execute(
            "PRAGMA user_version"
        ).fetchone()[0] == _SCHEMA_USER_VERSION
        rows = {
            r[0] for r in machine.conn.execute(
                f"SELECT key FROM settings WHERE set_id = '{SCHEMA_META_SET_ID}'"
            ).fetchall()
        }
        assert "autonomy.test.startupdemo#1" in rows, (
            "startup flush did not materialize the newly registered schema "
            "in the machine store"
        )
    finally:
        machine.close()

    reopened = GraphDB(str(org_db_path), mode="rw")
    try:
        org_rows = reopened.conn.execute(
            "SELECT COUNT(*) FROM settings WHERE set_id IN (?, ?)",
            (SCHEMA_META_SET_ID, SYNOPSIS_META_SET_ID),
        ).fetchone()[0]
        assert org_rows == 0, (
            "startup left schema-meta rows in an organization database — "
            "either the flush wrote there or the sweep missed the stale row"
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

    GraphDB.create_org_db("autonomy").close()
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

    machine = GraphDB(str(_org_db_path("machine")), mode="rw")
    try:
        rows = {
            r[0] for r in machine.conn.execute(
                f"SELECT key FROM settings WHERE set_id = '{SYNOPSIS_META_SET_ID}'"
            ).fetchall()
        }
        assert "autonomy.test.startupdemo#1" in rows
    finally:
        machine.close()
        GraphDB.close_all_pooled()
