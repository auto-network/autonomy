"""Overriding your own row rewrites it; overriding a peer's layers a patch.

"Override" states an intent -- make this value win. A patch row was never part
of that promise, only of how it happened to be stored, and on a set declaring
one row per key the two contradict: the declaration says one row and a chain is
the opposite of that. So the intent is satisfied by rewriting the row.

What forces a patch row is not intent but physics. A row in another
organization's database cannot be rewritten from here, which is the case
overrides exist for and the one place a chain remains.

The failure this removes: a write that reports success, stores a row, and
changes nothing anyone reads, because a patch already sitting on the same key
overwrites the field again on every read.
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
    singleton,
)


@pytest.fixture(scope="module")
def schemas():
    @keyed_per_entity(key_strategy="probe_id")
    class Keyed(SettingSchema):
        set_id = "probe.collapse.keyed"
        schema_revision = 1
        a: str = field(required=True, description="a")
        b: str = field(required=False, description="b")

    @singleton()
    class Only(SettingSchema):
        set_id = "probe.collapse.singleton"
        schema_revision = 1
        a: str = field(required=True, description="a")

    @append_only_log()
    class Logged(SettingSchema):
        set_id = "probe.collapse.logged"
        schema_revision = 1
        a: str = field(required=True, description="a")

    return Keyed, Only, Logged


@pytest.fixture
def orgs(tmp_path, monkeypatch, schemas):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    for slug in ("acme", "partner"):
        GraphDB.create_org_db(slug).close()
    yield
    GraphDB.close_all_pooled()


def _rows(set_id, key, org):
    return settings_ops.layers_for(set_id, key, org=org)


# ── own rows are rewritten ───────────────────────────────────


def test_overriding_own_row_leaves_one_row(orgs):
    base = settings_ops.add_setting("probe.collapse.keyed", 1, "k",
                                    {"a": "1", "b": "2"}, org="acme")

    returned = settings_ops.override_setting(base, {"b": "changed"}, org="acme")

    layers = _rows("probe.collapse.keyed", "k", "acme")
    assert layers["overrides"] == [], "a chain formed where one row was declared"
    assert layers["base"]["payload"] == {"a": "1", "b": "changed"}
    assert returned == base, "the row that now holds the value is the one named"


def test_repeated_edits_do_not_accumulate(orgs):
    """The shape that produced a key resolving through seven rows."""
    base = settings_ops.add_setting("probe.collapse.keyed", 1, "many",
                                    {"a": "0"}, org="acme")
    for n in range(1, 6):
        settings_ops.override_setting(base, {"a": str(n)}, org="acme")

    layers = _rows("probe.collapse.keyed", "many", "acme")

    assert layers["overrides"] == []
    assert layers["base"]["payload"]["a"] == "5"


def test_a_second_write_is_not_masked_by_the_first(orgs):
    """The defect itself.

    Amend, then rewrite the whole row: the rewrite must be what readers see.
    While the amendment was a patch row it won on every read, so the rewrite
    stored a value nothing returned and reported success anyway.
    """
    base = settings_ops.add_setting("probe.collapse.keyed", 1, "m",
                                    {"a": "original"}, org="acme")
    settings_ops.override_setting(base, {"a": "amended"}, org="acme")

    settings_ops.upsert_by_key("probe.collapse.keyed", 1, "m",
                               {"a": "rewritten"}, org="acme")

    resolved = settings_ops.read_set_key("probe.collapse.keyed", "m",
                                         org="acme", peers=[])
    assert resolved["payload"]["a"] == "rewritten"


def test_a_singleton_is_rewritten_too(orgs):
    base = settings_ops.add_setting("probe.collapse.singleton", 1, "default",
                                    {"a": "1"}, org="acme")

    settings_ops.override_setting(base, {"a": "2"}, org="acme")

    layers = _rows("probe.collapse.singleton", "default", "acme")
    assert layers["overrides"] == []
    assert layers["base"]["payload"] == {"a": "2"}
    assert layers["base"]["id"] == base


def test_the_rows_own_publication_state_survives(orgs):
    """The caller's ``state`` described a patch row that is no longer
    created. Applying it here would let an unstated default demote a
    published row on an ordinary edit."""
    base = settings_ops.upsert_by_key(
        "probe.collapse.keyed", 1, "pub", {"a": "1"},
        org="acme", state="canonical")

    settings_ops.override_setting(base, {"a": "2"}, org="acme")  # state='raw'

    layers = _rows("probe.collapse.keyed", "pub", "acme")
    assert layers["base"]["state"] == "canonical"


def test_the_merged_result_is_validated(orgs):
    """Rewriting must not become a way past the schema."""
    from tools.graph.schemas.registry import SchemaValidationError

    base = settings_ops.add_setting("probe.collapse.keyed", 1, "v",
                                    {"a": "1"}, org="acme")

    with pytest.raises(SchemaValidationError):
        settings_ops.override_setting(base, {"a": None}, org="acme")


# ── a peer's row still layers ────────────────────────────────


def test_overriding_a_peer_row_still_writes_a_patch(orgs):
    """The one case a chain is not a choice: the row is in another
    organization's database and cannot be rewritten from here."""
    theirs = settings_ops.upsert_by_key(
        "probe.collapse.keyed", 1, "shared", {"a": "theirs"},
        org="partner", state="published")

    returned = settings_ops.override_setting(theirs, {"a": "ours"}, org="acme")

    assert returned != theirs, "a peer's row cannot be rewritten in place"
    resolved = settings_ops.read_set_key(
        "probe.collapse.keyed", "shared", org="acme")
    assert resolved["payload"]["a"] == "ours"


def test_the_peers_own_value_is_untouched(orgs):
    settings_ops.upsert_by_key(
        "probe.collapse.keyed", 1, "shared2", {"a": "theirs"},
        org="partner", state="published")
    theirs = settings_ops.read_set_key(
        "probe.collapse.keyed", "shared2", org="partner", peers=[])

    settings_ops.override_setting(theirs["id"], {"a": "ours"}, org="acme")

    still = settings_ops.read_set_key(
        "probe.collapse.keyed", "shared2", org="partner", peers=[])
    assert still["payload"]["a"] == "theirs"


# ── sets that permit amendment are untouched ─────────────────


def test_a_set_that_does_not_declare_replacement_still_layers(orgs):
    """Collapse follows the declaration, not a guess about the data. A log's
    history is its rows, and nothing here decides whether amending one is
    sensible -- only that this change does not alter it."""
    import uuid

    base = settings_ops.add_setting(
        "probe.collapse.logged", 1, str(uuid.uuid4()), {"a": "1"}, org="acme")

    returned = settings_ops.override_setting(base, {"a": "2"}, org="acme")

    assert returned != base
