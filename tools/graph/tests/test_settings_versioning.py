"""Schema versioning tests for the Settings primitive.

Covers the four read modes from the spec (default, target_revision,
min_revision, combined), upconvert chain composition, missing-link drop,
and ``--dry-run`` migrate. Spec: graph://0d3f750f-f9c § Schema versioning.
"""

from __future__ import annotations

import json

import pytest

from tools.graph import ops, schemas
from tools.graph.schemas.registry import SCHEMAS, UPCONVERTERS


@pytest.fixture
def graph_db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    yield db_path


@pytest.fixture(autouse=True)
def _isolate_schema_registry():
    schemas_snap = dict(SCHEMAS)
    upcon_snap = dict(UPCONVERTERS)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(schemas_snap)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upcon_snap)


# ── Versioning fixture: 3 revisions with one upconvert chain gap ────


@pytest.fixture
def chain_with_gap():
    """Register revisions 1, 2, 3 of autonomy.test.v with an upconverter
    only between 2 → 3. The 1 → 2 hop is missing — that's a breaking change.
    """

    class V1(schemas.SettingSchema):
        set_id = "autonomy.test.v"
        schema_revision = 1

    class V2(schemas.SettingSchema):
        set_id = "autonomy.test.v"
        schema_revision = 2

    def up_2_to_3(payload):
        out = dict(payload)
        out["v3_field"] = "added-by-upconvert"
        return out

    class V3(schemas.SettingSchema):
        set_id = "autonomy.test.v"
        schema_revision = 3

    schemas.register_schema("autonomy.test.v", 1, V1)
    schemas.register_schema("autonomy.test.v", 2, V2)
    schemas.register_schema("autonomy.test.v", 3, V3,
                            upconvert_from_prev=up_2_to_3)
    return (V1, V2, V3, up_2_to_3)


@pytest.fixture
def linear_chain():
    """1 → 2 → 3 with all hops present — clean linear upgrade."""

    class V1(schemas.SettingSchema):
        set_id = "autonomy.test.linear"
        schema_revision = 1

    def up_1_to_2(p):
        return {**p, "added_in_v2": True}

    class V2(schemas.SettingSchema):
        set_id = "autonomy.test.linear"
        schema_revision = 2

    def up_2_to_3(p):
        return {**p, "added_in_v3": True}

    class V3(schemas.SettingSchema):
        set_id = "autonomy.test.linear"
        schema_revision = 3

    schemas.register_schema("autonomy.test.linear", 1, V1)
    schemas.register_schema("autonomy.test.linear", 2, V2,
                            upconvert_from_prev=up_1_to_2)
    schemas.register_schema("autonomy.test.linear", 3, V3,
                            upconvert_from_prev=up_2_to_3)
    return (V1, V2, V3)


# ── Registry behaviour ─────────────────────────────────────


def test_registry_lookup(linear_chain):
    cls = schemas.get_schema("autonomy.test.linear", 2)
    assert cls is not None
    assert cls.schema_revision == 2


def test_registry_unknown_returns_none():
    assert schemas.get_schema("nope", 1) is None


def test_upconvert_chain_identity(linear_chain):
    chain = schemas.upconvert_chain("autonomy.test.linear", 2, 2)
    assert chain == []


def test_upconvert_chain_full(linear_chain):
    chain = schemas.upconvert_chain("autonomy.test.linear", 1, 3)
    assert chain is not None and len(chain) == 2


def test_upconvert_chain_missing_returns_none(chain_with_gap):
    chain = schemas.upconvert_chain("autonomy.test.v", 1, 3)
    assert chain is None  # 1 → 2 hop missing


def test_upconvert_chain_downgrade_returns_none(linear_chain):
    chain = schemas.upconvert_chain("autonomy.test.linear", 3, 1)
    assert chain is None


def test_upconvert_payload_applies_chain(linear_chain):
    out = schemas.upconvert_payload("autonomy.test.linear", 1, 3, {"x": 1})
    assert out == {"x": 1, "added_in_v2": True, "added_in_v3": True}


# ── Read modes ─────────────────────────────────────────────


def _seed_three(set_id):
    """Seed one row at each of revs 1, 2, 3 (different keys to keep all)."""
    a = ops.add_setting(set_id, 1, "k1", {"x": 1})
    b = ops.add_setting(set_id, 2, "k2", {"x": 2})
    c = ops.add_setting(set_id, 3, "k3", {"x": 3})
    return a, b, c


def test_default_returns_stored_revisions(graph_db_env, linear_chain):
    _seed_three("autonomy.test.linear")
    members = ops.read_set("autonomy.test.linear")
    by_key = {m.key: m for m in members.members}
    assert by_key["k1"].stored_revision == 1
    assert by_key["k2"].stored_revision == 2
    assert by_key["k3"].stored_revision == 3
    # No transform requested — target_revision stays None.
    assert all(m.target_revision is None for m in members.members)


def test_target_revision_upconverts_lower(graph_db_env, linear_chain):
    _seed_three("autonomy.test.linear")
    members = ops.read_set("autonomy.test.linear", target_revision=3)
    by_key = {m.key: m for m in members.members}
    # rev-1 row got upconverted through both hops.
    assert by_key["k1"].payload == {"x": 1, "added_in_v2": True, "added_in_v3": True}
    assert by_key["k1"].target_revision == 3
    assert by_key["k2"].payload == {"x": 2, "added_in_v3": True}
    assert by_key["k3"].payload == {"x": 3}


def test_target_revision_drops_higher(graph_db_env, linear_chain):
    """Asking for rev=2 drops rev-3 rows (no downgrade)."""
    _seed_three("autonomy.test.linear")
    members = ops.read_set("autonomy.test.linear", target_revision=2)
    keys = {m.key for m in members.members}
    assert "k3" not in keys
    assert members.dropped["above_target_no_downgrade"] == 1


def test_target_revision_drops_when_no_chain(graph_db_env, chain_with_gap):
    """Rev-1 row cannot upconvert to rev-3 — gap at 1 → 2."""
    ops.add_setting("autonomy.test.v", 1, "k1", {"x": 1})
    ops.add_setting("autonomy.test.v", 3, "k3", {"x": 3})
    members = ops.read_set("autonomy.test.v", target_revision=3)
    keys = {m.key for m in members.members}
    assert "k1" not in keys
    assert "k3" in keys
    assert members.dropped["no_upconvert_path"] == 1


def test_min_revision_filters_floor(graph_db_env, linear_chain):
    _seed_three("autonomy.test.linear")
    members = ops.read_set("autonomy.test.linear", min_revision=2)
    keys = {m.key for m in members.members}
    assert "k1" not in keys
    assert "k2" in keys and "k3" in keys
    assert members.dropped["below_min_revision"] == 1
    # Returned at stored revisions (no target_revision set).
    assert all(m.target_revision is None for m in members.members)


def test_combined_min_and_target(graph_db_env, linear_chain):
    """Tightest contract: only rev-2+ rows, all shaped as rev 2."""
    _seed_three("autonomy.test.linear")
    members = ops.read_set(
        "autonomy.test.linear", min_revision=2, target_revision=2,
    )
    keys = {m.key for m in members.members}
    assert keys == {"k2"}  # k1 below floor; k3 above target
    assert members.dropped["below_min_revision"] == 1
    assert members.dropped["above_target_no_downgrade"] == 1


def test_get_setting_with_target_revision(graph_db_env, linear_chain):
    sid = ops.add_setting("autonomy.test.linear", 1, "k", {"x": 1})
    got = ops.get_setting(sid, target_revision=3)
    assert got is not None
    assert got.payload == {"x": 1, "added_in_v2": True, "added_in_v3": True}
    assert got.target_revision == 3


def test_get_setting_above_target_returns_none(graph_db_env, linear_chain):
    sid = ops.add_setting("autonomy.test.linear", 3, "k", {"x": 3})
    assert ops.get_setting(sid, target_revision=2) is None


# ── migrate ────────────────────────────────────────────────


def test_migrate_dry_run_does_not_write(graph_db_env, linear_chain):
    a, b, c = _seed_three("autonomy.test.linear")
    report = ops.migrate_setting_revisions(
        "autonomy.test.linear", 3, dry_run=True,
    )
    assert report.rewrote == 2  # k1 (rev 1→3) and k2 (rev 2→3)
    assert report.already_at_target == 1
    assert set(report.affected_ids) == {a, b}
    # Storage unchanged.
    members = ops.read_set("autonomy.test.linear")
    by_key = {m.key: m.stored_revision for m in members.members}
    assert by_key == {"k1": 1, "k2": 2, "k3": 3}


def test_migrate_writes_when_committed(graph_db_env, linear_chain):
    _seed_three("autonomy.test.linear")
    report = ops.migrate_setting_revisions("autonomy.test.linear", 3)
    assert report.rewrote == 2
    members = ops.read_set("autonomy.test.linear")
    by_key = {m.key: m for m in members.members}
    assert by_key["k1"].stored_revision == 3
    assert by_key["k1"].payload == {"x": 1, "added_in_v2": True, "added_in_v3": True}


def test_migrate_reports_no_chain(graph_db_env, chain_with_gap):
    ops.add_setting("autonomy.test.v", 1, "k1", {"x": 1})
    ops.add_setting("autonomy.test.v", 3, "k3", {"x": 3})
    report = ops.migrate_setting_revisions("autonomy.test.v", 3)
    assert report.no_upconvert_path == 1
    assert report.already_at_target == 1
    assert report.rewrote == 0


def test_migrate_leaves_above_target_alone(graph_db_env, linear_chain):
    sid = ops.add_setting("autonomy.test.linear", 3, "k", {"x": 3})
    report = ops.migrate_setting_revisions("autonomy.test.linear", 2)
    assert report.above_target == 1
    # Storage stayed at 3.
    got = ops.get_setting(sid)
    assert got.stored_revision == 3
