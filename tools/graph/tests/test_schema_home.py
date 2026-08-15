"""``@home`` — which database a Setting lives in, declared and enforced.

``personal`` is the operator's own store: their identity, their credentials,
their machine. ``organization`` is a store an org owns and that its members
read. An organization's database is what federates, so a value in the wrong
one is either invisible to everyone who needs it or visible to everyone who
should not have it. Neither failure announces itself.

It is a decorator of its own rather than an argument to the access-pattern
ones because the two answer different questions — how many rows there are,
and whose database they are in — and neither implies the other.
"""
from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    declared_home,
    field,
    home,
    keyed_per_entity,
    singleton,
)


@pytest.fixture(scope="module")
def homed_schemas():
    """Module-scoped: the registry is process-global and refuses to
    re-register a set_id."""
    @home("personal")
    @keyed_per_entity(key_strategy="org_slug")
    class Mine(SettingSchema):
        set_id = "probe.home.mine"
        schema_revision = 1
        v: str = field(required=True, description="value")

    @home("organization")
    @keyed_per_entity(key_strategy="workspace_id")
    class Ours(SettingSchema):
        set_id = "probe.home.ours"
        schema_revision = 1
        v: str = field(required=True, description="value")

    @singleton(key="default")
    class Undeclared(SettingSchema):
        set_id = "probe.home.undeclared"
        schema_revision = 1
        v: str = field(required=True, description="value")

    return Mine, Ours, Undeclared


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    root = tmp_path / "orgs"
    root.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.create_org_db("acme", path=root / "acme.db").close()
    return root


# ── The declaration ──────────────────────────────────────────


def test_a_home_must_name_a_real_one(homed_schemas):
    with pytest.raises(SchemaValidationError, match="home must be one of"):
        home("global")


def test_a_schema_cannot_live_in_two_databases(homed_schemas):
    with pytest.raises(SchemaValidationError, match="declares two homes"):
        @home("organization")
        @home("personal")
        class Confused(SettingSchema):
            set_id = "probe.home.confused"
            schema_revision = 1
            v: str = field(required=True, description="value")


def test_home_and_cardinality_are_independent(homed_schemas):
    """Declaring one must not consume or overwrite the other."""
    Mine, Ours, _ = homed_schemas
    assert (Mine._home, Mine._access_pattern) == ("personal", "keyed_per_entity")
    assert (Ours._home, Ours._access_pattern) == ("organization", "keyed_per_entity")


def test_an_undeclared_home_is_undeclared(homed_schemas):
    """Not a default. A schema that has not been through this decision
    asserts nothing and behaves exactly as it did."""
    assert declared_home("probe.home.undeclared") is None


# ── The enforcement ──────────────────────────────────────────


def test_a_personal_setting_refuses_an_organization(homed_schemas, orgs_root):
    with pytest.raises(SchemaValidationError, match="operator's own database"):
        settings_ops.add_setting(
            "probe.home.mine", 1, "acme", {"v": "x"}, org="acme",
        )


def test_an_organization_setting_refuses_the_personal_store(homed_schemas, orgs_root):
    with pytest.raises(SchemaValidationError, match="organization's database"):
        settings_ops.add_setting(
            "probe.home.ours", 1, "ws-a", {"v": "x"}, org="personal",
        )


def test_a_read_looking_in_the_wrong_place_fails_the_same_way(homed_schemas, orgs_root):
    """Checked on the way to the database, so the direction of travel does
    not change the answer. A read that silently finds nothing in the wrong
    store is the harder bug of the two."""
    with pytest.raises(SchemaValidationError, match="operator's own database"):
        settings_ops.read_set("probe.home.mine", org="acme", peers=[])


def test_each_setting_is_accepted_in_the_database_it_declares(homed_schemas, orgs_root):
    settings_ops.add_setting("probe.home.mine", 1, "acme", {"v": "p"}, org="personal")
    settings_ops.add_setting("probe.home.ours", 1, "ws-a", {"v": "o"}, org="acme")

    mine = settings_ops.read_set("probe.home.mine", org="personal", peers=[])
    ours = settings_ops.read_set("probe.home.ours", org="acme", peers=[])

    assert [m.payload["v"] for m in mine.members] == ["p"]
    assert [m.payload["v"] for m in ours.members] == ["o"]


def test_an_undeclared_setting_is_routed_anywhere(homed_schemas, orgs_root):
    settings_ops.add_setting("probe.home.undeclared", 1, "default", {"v": "a"}, org="acme")
    settings_ops.add_setting("probe.home.undeclared", 1, "default", {"v": "b"}, org="personal")
