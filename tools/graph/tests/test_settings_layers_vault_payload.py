"""layers_for reports a vaulted row's locator-string payload opaquely."""

from __future__ import annotations

import json

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import (
    SettingSchema,
    field,
    keyed_per_entity,
)


@pytest.fixture(scope="module")
def probe_schema():
    @keyed_per_entity(key_strategy="probe_id")
    class Probe(SettingSchema):
        set_id = "probe.layers.vaultish"
        schema_revision = 1
        v: str = field(required=True, description="v")

    return Probe


@pytest.fixture
def orgs(tmp_path, monkeypatch, probe_schema):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.create_org_db("acme").close()
    yield
    GraphDB.close_all_pooled()


def test_locator_string_base_is_reported_not_crashed(orgs):
    """A vaulted row stores a JSON-encoded locator STRING in the payload
    column. layers_for used to run ``dict()`` over it and raise
    ``dictionary update sequence element #0 has length 1`` — every caller
    that touched a vault row crashed."""
    sid = settings_ops.add_setting(
        "probe.layers.vaultish", 1, "k", {"v": "x"}, org="acme", state="raw",
    )
    db = settings_ops._open("acme")
    try:
        db.conn.execute(
            "UPDATE settings SET payload = ? WHERE id = ?",
            (json.dumps("vault:abc123"), sid),
        )
        db.conn.commit()
    finally:
        db.close()

    layers = settings_ops.layers_for("probe.layers.vaultish", "k", org="acme")

    assert layers["base"]["id"] == sid
    assert layers["base"]["payload"] == "vault:abc123"
    assert layers["resolved"] == "vault:abc123"
    assert layers["overrides"] == []
