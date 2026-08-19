"""``@home("machine")`` — this computer, and nowhere else.

The operator's own store follows them across every machine they own. Some
values cannot: where a binary landed on this box, what version is installed
here, what was verified here. Carried to a second machine those are not merely
useless, they are believed — a recorded path to a program that host does not
have, trusted instead of probed, so the setup step that would have said
"install it" is skipped and the failure surfaces somewhere else entirely.

A machine store is a separate database, which is what lets a fleet sync ignore
it wholesale rather than inspecting rows and coupling replication to the schema
registry. Publication state is untouched by any of this: it governs who may
READ across a boundary, and it keeps meaning exactly that.
"""
from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
)


@pytest.fixture(scope="module")
def schemas():
    @home("machine")
    @keyed_per_entity(key_strategy="harness_name")
    class OnThisBox(SettingSchema):
        set_id = "probe.home.machine"
        schema_revision = 1
        path: str = field(required=True, description="where it is on this box")

    @home("personal")
    @keyed_per_entity(key_strategy="org_slug")
    class Mine(SettingSchema):
        set_id = "probe.home.mine-not-machine"
        schema_revision = 1
        v: str = field(required=True, description="value")

    return OnThisBox, Mine


@pytest.fixture
def orgs(tmp_path, monkeypatch, schemas):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.create_org_db("acme").close()
    yield tmp_path / "orgs"
    GraphDB.close_all_pooled()


def test_a_machine_setting_lives_in_its_own_database(orgs):
    settings_ops.add_setting(
        "probe.home.machine", 1, "claude",
        {"path": "/home/j/.local/bin/claude"}, org="machine")

    assert (orgs.parent / "machine.db").exists(), (
        "a separate file is what lets a fleet sync skip it wholesale")
    row = settings_ops.read_set_key(
        "probe.home.machine", "claude", org="machine", peers=[])
    assert row["payload"]["path"] == "/home/j/.local/bin/claude"


def test_nobody_provisions_the_machine_store_first(orgs):
    """Like the operator's own store, it comes into being where it is used."""
    assert not (orgs.parent / "machine.db").exists()
    settings_ops.add_setting(
        "probe.home.machine", 1, "codex", {"path": "/usr/bin/codex"},
        org="machine")
    assert (orgs.parent / "machine.db").exists()


@pytest.mark.parametrize("org", ["acme", "personal", None])
def test_a_machine_setting_is_refused_anywhere_that_travels(orgs, org):
    """Every other store either follows the operator or reaches an org."""
    with pytest.raises(SchemaValidationError, match="never leaves it"):
        settings_ops.add_setting(
            "probe.home.machine", 1, "claude", {"path": "/p"}, org=org)


def test_the_machine_store_takes_nothing_that_should_travel(orgs):
    """The other direction: a value that must reach other machines cannot be
    parked here, where it would silently stop existing for them."""
    with pytest.raises(SchemaValidationError, match="does not live in this machine"):
        settings_ops.add_setting(
            "probe.home.mine-not-machine", 1, "acme", {"v": "x"}, org="machine")


def test_it_needs_no_machine_in_its_key(orgs):
    """The database IS the machine, so repeating it would be the duplication
    every other home already refuses."""
    settings_ops.add_setting(
        "probe.home.machine", 1, "claude", {"path": "/a"}, org="machine")

    members = settings_ops.read_set("probe.home.machine", org="machine", peers=[])
    assert [m.key for m in members.members] == ["claude"]


def test_publication_state_is_untouched_by_any_of_this(orgs):
    """Machine scope is a fact about WHERE a row lives, not who may read it.

    The two axes stay separate: a machine row can carry any state, and the
    state means what it always meant.
    """
    for state in ("raw", "curated", "published", "canonical"):
        settings_ops.upsert_by_key(
            "probe.home.machine", 1, f"h-{state}", {"path": f"/{state}"},
            org="machine", state=state)

    members = settings_ops.read_set("probe.home.machine", org="machine", peers=[])
    assert {m.state for m in members.members} == {
        "raw", "curated", "published", "canonical"}
