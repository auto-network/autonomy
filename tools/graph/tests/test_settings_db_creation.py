"""Opening a Setting store never brings a database into existence.

An organization's database is created when the organization is. Letting an
open create one instead is silent and wrong: inside a container, a path that
is not mounted where the caller believes it is yields a fresh empty database,
the write reports success, and nothing ever reads it. The row is not lost to a
bug anyone can see -- it is in a file that will be discarded when the container
exits.

Two destinations are exempt, because nobody provisions them first: saying
nothing, and naming the operator's own store. Those are the same database, and
``_db_path`` provisions it deliberately, under a lock.
"""
from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB, GraphDBMissing


SET_ID = "autonomy.workspace.turn_correction"
REVISION = 1


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    root = tmp_path / "orgs"
    root.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    return root


def test_writing_to_an_unprovisioned_org_fails_and_names_the_path(orgs_root):
    """The hazard, stated directly: the write must not invent its target."""
    with pytest.raises(GraphDBMissing) as caught:
        settings_ops.add_setting(
            SET_ID, REVISION, "ws-a", {"enabled": True}, org="never-provisioned",
        )

    assert "never-provisioned" in str(caught.value), (
        "the error has to name the database it looked for -- that path is the "
        "whole diagnosis when a mount is missing"
    )
    assert not list(orgs_root.glob("never-provisioned*")), (
        "refusing the write must also leave no file behind"
    )


def test_reading_an_unprovisioned_org_creates_nothing(orgs_root):
    with pytest.raises(GraphDBMissing):
        settings_ops.read_set(SET_ID, org="never-provisioned")
    assert not list(orgs_root.glob("never-provisioned*"))


def test_a_provisioned_org_writes_and_reads_normally(orgs_root):
    """The guard is about existence, not about making writes harder."""
    GraphDB.create_org_db("provisioned", path=orgs_root / "provisioned.db").close()

    settings_ops.add_setting(
        SET_ID, REVISION, "ws-a", {"enabled": False}, org="provisioned",
    )
    members = settings_ops.read_set(SET_ID, org="provisioned", peers=[])

    assert [m.key for m in members.members] == ["ws-a"]
    assert members.members[0].payload["enabled"] is False


def test_the_operators_own_store_is_created_on_demand(orgs_root):
    """Named or defaulted, ``personal`` is the same database and nobody
    provisions it first -- so it is exempt, by both routes."""
    settings_ops.add_setting(
        SET_ID, REVISION, "ws-a", {"enabled": True}, org="personal",
    )
    assert (orgs_root.parent / "personal.db").exists()

    members = settings_ops.read_set(SET_ID, org="personal", peers=[])
    assert [m.key for m in members.members] == ["ws-a"]
