"""A schema's access pattern decides how its rows may be written.

Every schema already states this. ``@singleton`` says latest-write-wins,
``@keyed_per_entity`` says upsert, ``@append_only_log`` says rows are never
overridden. Until it was checked, all three were advice: a set declared
latest-write-wins could be amended with an override, and a log row declared
never-rewritten could be rewritten in place.

The cost of the first is not abstract. Overriding appends a row and every
later read merges the whole chain, so a value changed that way grows a layer
per edit — which is how a workspace comes to resolve through ten rows.

Overriding a row ANOTHER organization owns stays allowed, and is what
overrides are for: you cannot rewrite a row you do not own, and adapting a
shared primitive locally is the intended use.
"""
from __future__ import annotations

import uuid

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import (
    SettingSchema,
    append_only_log,
    field,
    keyed_per_entity,
    singleton,
)


@pytest.fixture(scope="module")
def schemas():
    @singleton(key="default")
    class Replaced(SettingSchema):
        set_id = "probe.pattern.singleton"
        schema_revision = 1
        v: str = field(required=True, description="value")

    @keyed_per_entity(key_strategy="org_slug")
    class PerEntity(SettingSchema):
        set_id = "probe.pattern.per-entity"
        schema_revision = 1
        v: str = field(required=True, description="value")

    @append_only_log(key="uuid_v4")
    class Log(SettingSchema):
        set_id = "probe.pattern.log"
        schema_revision = 1
        v: str = field(required=True, description="value")

    @singleton(key="default")
    class Undeclared(SettingSchema):
        # Declares a pattern but stands in for the general case below.
        set_id = "probe.pattern.other"
        schema_revision = 1
        v: str = field(required=True, description="value")

    return Replaced, PerEntity, Log, Undeclared


@pytest.fixture
def orgs(tmp_path, monkeypatch, schemas):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    for slug in ("acme", "other"):
        GraphDB.create_org_db(slug).close()
    yield
    GraphDB.close_all_pooled()


# ── a replaced row is rewritten, not amended ─────────────────


@pytest.mark.parametrize(
    "set_id", ["probe.pattern.singleton", "probe.pattern.per-entity"])
def test_amending_your_own_replaced_row_is_refused(orgs, set_id):
    key = "default" if set_id.endswith("singleton") else "acme"
    base = settings_ops.add_setting(set_id, 1, key, {"v": "a"}, org="acme")

    with pytest.raises(ValueError, match="replaced, not amended"):
        settings_ops.override_setting(base, {"v": "b"}, org="acme")


def test_the_refusal_names_the_verb_to_use_instead(orgs):
    base = settings_ops.add_setting(
        "probe.pattern.singleton", 1, "default", {"v": "a"}, org="acme")

    with pytest.raises(ValueError, match=r"upsert_by_key\(key='default'\)"):
        settings_ops.override_setting(base, {"v": "b"}, org="acme")


def test_rewriting_it_is_what_works(orgs):
    first = settings_ops.upsert_by_key(
        "probe.pattern.singleton", 1, "default", {"v": "a"}, org="acme")
    again = settings_ops.upsert_by_key(
        "probe.pattern.singleton", 1, "default", {"v": "b"}, org="acme")

    assert first == again, "one row, rewritten in place"
    row = settings_ops.read_set_key(
        "probe.pattern.singleton", "default", org="acme", peers=[])
    assert row["payload"]["v"] == "b"


# ── another organization's row is a different matter ─────────


def test_overriding_a_peer_organizations_row_is_allowed(orgs):
    """You cannot rewrite what you do not own; this is why overrides exist."""
    base = settings_ops.add_setting(
        "probe.pattern.singleton", 1, "default", {"v": "a"},
        org="acme", state="canonical")

    sid = settings_ops.override_setting(
        base, {"v": "b"}, org="other", state="canonical")

    assert sid


# ── an append-only row is never rewritten ────────────────────


def test_rewriting_an_append_only_row_is_refused(orgs):
    key = str(uuid.uuid4())
    settings_ops.add_setting("probe.pattern.log", 1, key, {"v": "a"}, org="acme")

    with pytest.raises(ValueError, match="never rewritten"):
        settings_ops.upsert_by_key(
            "probe.pattern.log", 1, key, {"v": "b"}, org="acme")


def test_appending_to_it_is_what_works(orgs):
    for _ in range(3):
        settings_ops.add_setting(
            "probe.pattern.log", 1, str(uuid.uuid4()), {"v": "a"}, org="acme")

    members = settings_ops.read_set("probe.pattern.log", org="acme", peers=[])
    assert len(members.members) == 3
