"""No set that declares one row per key may hold an amendment.

`singleton` and `keyed_per_entity` declare that a key resolves to exactly one
stored row. An override on such a set contradicts the declaration: the value
becomes a merge of a chain, and every reader pays for a history nobody asked to
keep. Writing one is no longer possible -- overriding your own row on such a set
rewrites it -- so this is a gate against the condition returning by some path
nobody anticipated, which is how it arrived the first time.

Seventy-eight such rows existed. They were invisible to every read, because
reads return the merged result; unaddressable by key once a base was deleted;
and reported as fine by a sweep that asked the calling process instead of the
store. Finding them took a day. This asserts the count stays zero so nobody
spends that day again.

Cross-organization overrides are untouched and always legal: a row in another
organization's database cannot be rewritten from here, which is the case
overrides exist for.
"""
from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import (
    SettingSchema,
    field,
    keyed_per_entity,
)


@pytest.fixture(scope="module")
def gated():
    @keyed_per_entity(key_strategy="probe_id")
    class Gated(SettingSchema):
        set_id = "probe.gate.keyed"
        schema_revision = 1
        v: str = field(required=True, description="v")

    return Gated


@pytest.fixture
def orgs(tmp_path, monkeypatch, gated):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    for slug in ("acme", "partner"):
        GraphDB.create_org_db(slug).close()
    yield
    GraphDB.close_all_pooled()


def test_ordinary_use_creates_none(orgs):
    """The gate has to hold under the workflow that produced them.

    Repeated partial edits are how the platform is actually operated -- one
    key had been amended seven times. Each now lands in the row itself.
    """
    base = settings_ops.add_setting("probe.gate.keyed", 1, "k", {"v": "0"},
                                    org="acme")
    for n in range(1, 8):
        settings_ops.override_setting(base, {"v": str(n)}, org="acme")

    assert settings_ops.illegal_amendments(org="acme") == []
    assert settings_ops.read_set_key(
        "probe.gate.keyed", "k", org="acme", peers=[])["payload"]["v"] == "7"


def test_a_peers_row_is_still_adaptable(orgs):
    """The legal case must survive the gate, or the gate is a ban."""
    theirs = settings_ops.upsert_by_key(
        "probe.gate.keyed", 1, "shared", {"v": "theirs"},
        org="partner", state="published")

    settings_ops.override_setting(theirs, {"v": "ours"}, org="acme")

    assert settings_ops.illegal_amendments(org="acme") == []
    assert settings_ops.read_set_key(
        "probe.gate.keyed", "shared", org="acme")["payload"]["v"] == "ours"


def test_the_gate_detects_one_that_slipped_through(orgs, monkeypatch):
    """A gate that cannot fail proves nothing.

    The condition is forced past the collapse here exactly as a row written
    before the rule existed would appear.
    """
    base = settings_ops.add_setting("probe.gate.keyed", 1, "slip", {"v": "0"},
                                    org="acme")
    monkeypatch.setattr(settings_ops, "_collapse_amendment",
                        lambda *a, **k: None)
    settings_ops.override_setting(base, {"v": "1"}, org="acme")

    found = settings_ops.illegal_amendments(org="acme")

    assert [f["key"] for f in found] == ["slip"]
