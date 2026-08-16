"""An override on a set that forbids amendment is a row that cannot exist.

``singleton`` and ``keyed_per_entity`` declare that rows are replaced, not
amended. The write path enforces it. Resolution does not consult it, so such a
row is still merged into every resolved value -- the store serves a state its
own schema declares illegal, and nothing reports it.

The legitimate case has to survive: an override whose base belongs to another
organization is the reason overrides exist, since you cannot rewrite a row you
do not own.
"""
from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import (
    SettingSchema,
    append_only_log,
    field,
    keyed_per_entity,
)


@pytest.fixture(scope="module")
def schemas():
    @keyed_per_entity(key_strategy="probe_id")
    class Replaced(SettingSchema):
        set_id = "probe.amend.replaced"
        schema_revision = 1
        v: str = field(required=True, description="v")

    @append_only_log()
    class Appended(SettingSchema):
        set_id = "probe.amend.appended"
        schema_revision = 1
        v: str = field(required=True, description="v")

    return Replaced, Appended


@pytest.fixture
def orgs(tmp_path, monkeypatch, schemas):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    for slug in ("acme", "partner"):
        GraphDB.create_org_db(slug).close()
    yield
    GraphDB.close_all_pooled()


def test_a_clean_org_reports_nothing(orgs):
    settings_ops.add_setting("probe.amend.replaced", 1, "k", {"v": "x"},
                             org="acme")

    assert settings_ops.illegal_amendments(org="acme") == []


def test_an_override_on_a_replaced_set_is_reported(orgs, monkeypatch):
    """Written past the guard, as a row predating the rule would be."""
    base = settings_ops.add_setting("probe.amend.replaced", 1, "k", {"v": "x"},
                                    org="acme")
    monkeypatch.setattr(settings_ops, "_collapse_amendment",
                        lambda *a, **k: None)
    settings_ops.override_setting(base, {"v": "amended"}, org="acme")

    found = settings_ops.illegal_amendments(org="acme")

    assert [f["set_id"] for f in found] == ["probe.amend.replaced"]
    assert found[0]["key"] == "k"
    assert found[0]["access_pattern"] == "keyed_per_entity"


def test_a_set_that_permits_amendment_is_left_alone(orgs, monkeypatch):
    """The check must discriminate by declaration, not by shape."""
    import uuid
    base = settings_ops.add_setting(
        "probe.amend.appended", 1, str(uuid.uuid4()), {"v": "x"}, org="acme")
    settings_ops.override_setting(base, {"v": "amended"}, org="acme")

    assert settings_ops.illegal_amendments(org="acme") == []


def test_the_row_is_addressable_by_id(orgs, monkeypatch):
    """The only way to reach an override whose base was deleted: every other
    address goes through resolution, which returns nothing for it."""
    base = settings_ops.add_setting("probe.amend.replaced", 1, "gone", {"v": "x"},
                                    org="acme")
    monkeypatch.setattr(settings_ops, "_collapse_amendment",
                        lambda *a, **k: None)
    settings_ops.override_setting(base, {"v": "amended"}, org="acme")

    found = settings_ops.illegal_amendments(org="acme")

    assert found and found[0]["id"]
    assert found[0]["supersedes"] == base


def test_a_stranded_override_is_not_mistaken_for_a_peers(orgs, monkeypatch):
    """Deleted-base and peer-owned look identical from the owning database.

    Both have a ``supersedes`` that is not a local id. Treating that as
    "peer, therefore legitimate" skips the one row a sweep exists to find:
    stranded, resolving to nothing, unreachable by key, invisible to every
    read. Asking whether the target exists anywhere separates them.
    """
    from tools.graph.db import GraphDB, resolve_caller_db_path

    base = settings_ops.add_setting("probe.amend.replaced", 1, "strand",
                                    {"v": "x"}, org="acme")
    monkeypatch.setattr(settings_ops, "_collapse_amendment",
                        lambda *a, **k: None)
    settings_ops.override_setting(base, {"v": "amended"}, org="acme")

    db = GraphDB(resolve_caller_db_path("acme"))
    try:
        db.conn.execute("DELETE FROM settings WHERE id = ?", (base,))
        db.conn.commit()
    finally:
        db.close()

    found = settings_ops.illegal_amendments(org="acme")

    stranded = [f for f in found if f["key"] == "strand"]
    assert stranded, "a stranded override was counted as a legitimate peer override"
    assert stranded[0]["base_present"] is False
