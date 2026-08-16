"""``read_set_key`` answers with the value, and identifies the base.

The row it returns identifies the BASE — which is what a caller targeting an
override, a promote or an exclude needs, and why it returns a row rather than
a payload. Its payload is the RESOLVED one: base plus every override that
applies.

Those two have to come from different places, and when they did not, the
function returned a well-formed payload with declared defaults applied that
silently omitted every override. Nothing about the shape gave that away — it
looked exactly like a resolved payload, so the first consumer to read a field
from it read a stale value and had no way to notice.
"""
from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB

SET_ID = "autonomy.workspace"


def _legacy_override(base_id: str, patch: dict, *, org: str = "acme") -> str:
    """An override row of the kind that already exists in stored data.

    ``override_setting`` now refuses to amend a row the caller owns when the
    schema says its rows are replaced — the chains in live data predate that
    rule, and merging has to keep working for them. Writing the row directly
    is how the test reproduces one without going through the verb that
    correctly refuses to create another.
    """
    import json as _json
    from uuid import uuid4

    sid = str(uuid4())
    db = GraphDB(org=org)
    try:
        base = db.conn.execute(
            "SELECT set_id, schema_revision, key, publication_state "
            "FROM settings WHERE id = ?", (base_id,)).fetchone()
        db.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
            "publication_state, supersedes, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
            (sid, base["set_id"], base["schema_revision"], base["key"],
             _json.dumps(patch), base["publication_state"], base_id),
        )
        db.conn.commit()
    finally:
        db.close()
    return sid


@pytest.fixture
def acme(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.create_org_db("acme").close()
    yield
    GraphDB.close_all_pooled()


def _base(**payload) -> str:
    return settings_ops.add_setting(
        SET_ID, 1, "w", {"name": "w", "image": "img", **payload}, org="acme")


def _resolved():
    return settings_ops.read_set_key(SET_ID, "w", org="acme", peers=[])


def test_an_override_is_applied(acme):
    base = _base()
    _legacy_override(base, {"image": "img2"})

    assert _resolved()["payload"]["image"] == "img2"


def test_overrides_apply_in_order(acme):
    base = _base()
    _legacy_override(base, {"image": "second"}, org="acme")
    _legacy_override(base, {"image": "third"})

    assert _resolved()["payload"]["image"] == "third"


def test_it_agrees_with_read_set(acme):
    """The two read paths must not answer differently about one key."""
    base = _base(working_dir="/a")
    _legacy_override(base, {"working_dir": "/b"})

    member = next(m for m in settings_ops.read_set(
        SET_ID, org="acme", peers=[]).members if m.key == "w")

    assert _resolved()["payload"] == member.payload


def test_the_row_still_identifies_the_base(acme):
    """Most callers want the id, to target an override or a promote."""
    base = _base()
    _legacy_override(base, {"image": "img2"})

    assert _resolved()["id"] == base


def test_declared_defaults_are_still_applied(acme):
    _base()
    assert _resolved()["payload"]["network_host"] is False


def test_a_key_with_no_row_resolves_to_nothing(acme):
    _base()
    assert settings_ops.read_set_key(
        SET_ID, "absent", org="acme", peers=[]) is None
