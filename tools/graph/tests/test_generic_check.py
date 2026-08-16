"""``check_setting`` — is this row satisfied, and everything it depends on?

Metadata-driven end to end. It follows fields declaring ``references`` and
asks fields declaring ``exists``, and it contains no name of any particular
set, so adding a setting requires no code here. The proof is that these tests
invent their own schemas: the checker has never heard of them and walks them
anyway.

Two things it deliberately does not do. It does not run readiness checks at
write — whether a file is present is a fact about the world, not the value,
and enforcing it at write would make an organization's row refusable on one
machine and acceptable on another. And it does not follow relationships
carried only by key convention: it reaches exactly as far as the declarations
go, and says so, rather than inventing an edge nobody stated.
"""
from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import (
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    validate_payload,
)


@pytest.fixture(scope="module")
def invented():
    """Schemas the checker has never heard of."""
    @home("machine")
    @keyed_per_entity(key_strategy="probe_id")
    class Local(SettingSchema):
        set_id = "probe.check.local"
        schema_revision = 1
        path: str = field(required=True, description="a file here", exists="file")

    @keyed_per_entity(key_strategy="probe_id")
    class Needs(SettingSchema):
        set_id = "probe.check.needs"
        schema_revision = 1
        uses: str = field(required=True, description="what it needs",
                          references="probe.check.local")

    @keyed_per_entity(key_strategy="probe_id")
    class Plain(SettingSchema):
        set_id = "probe.check.plain"
        schema_revision = 1
        v: str = field(required=True, description="declares no dependency")

    return Local, Needs, Plain


@pytest.fixture
def acme(tmp_path, monkeypatch, invented):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.create_org_db("acme").close()
    present = tmp_path / "present.txt"
    present.write_text("x")
    yield present
    GraphDB.close_all_pooled()


def _check(key):
    return settings_ops.check_setting("probe.check.needs", key, org="acme")


def test_a_satisfied_chain_reports_nothing(acme):
    settings_ops.add_setting("probe.check.local", 1, "here",
                             {"path": str(acme)}, org="machine")
    settings_ops.add_setting("probe.check.needs", 1, "a",
                             {"uses": "here"}, org="acme")

    assert _check("a") == []


def test_a_reference_to_a_row_that_does_not_exist_is_reported(acme):
    settings_ops.add_setting("probe.check.needs", 1, "b",
                             {"uses": "never-provisioned"}, org="acme")

    findings = _check("b")

    assert [f.kind for f in findings] == ["missing_reference"]
    assert "probe.check.local" in findings[0].address


def test_a_declared_file_that_is_absent_is_reported(acme):
    settings_ops.add_setting("probe.check.local", 1, "gone",
                             {"path": "/nope/not-there.txt"}, org="machine")
    settings_ops.add_setting("probe.check.needs", 1, "c",
                             {"uses": "gone"}, org="acme")

    findings = _check("c")

    assert [f.kind for f in findings] == ["missing_path"]
    assert "not-there.txt" in findings[0].detail


def test_the_walk_crosses_into_the_home_the_target_declares(acme):
    """The referring row is an org's; its target lives on this machine.

    The checker reads each row where its schema says it lives, which is the
    only way a walk survives sets with different homes.
    """
    settings_ops.add_setting("probe.check.local", 1, "here",
                             {"path": str(acme)}, org="machine")
    settings_ops.add_setting("probe.check.needs", 1, "d",
                             {"uses": "here"}, org="acme")

    assert _check("d") == []


def test_a_row_declaring_no_dependency_is_satisfied_by_existing(acme):
    settings_ops.add_setting("probe.check.plain", 1, "p", {"v": "x"}, org="acme")

    assert settings_ops.check_setting("probe.check.plain", "p", org="acme") == []


def test_a_readiness_check_never_runs_at_write(acme):
    """A missing file must not make a row unwritable.

    Otherwise provisioning order becomes mandatory and an organization's row
    is refusable on one machine and acceptable on another.
    """
    validate_payload("probe.check.local", 1, {"path": "/definitely/absent"})
    assert settings_ops.add_setting(
        "probe.check.local", 1, "written-anyway",
        {"path": "/definitely/absent"}, org="machine")


def test_every_finding_says_where_it_looked(acme):
    """"Not found" from a process that cannot see a filesystem is a different
    fact from "not found" on the machine that owns it."""
    settings_ops.add_setting("probe.check.needs", 1, "e",
                             {"uses": "absent"}, org="acme")

    for finding in _check("e"):
        assert finding.looked_in, "a finding with no frame cannot be judged"


def test_a_cycle_terminates(acme):
    """Declared edges may point back; the walk must not."""
    @keyed_per_entity(key_strategy="probe_id")
    class Loop(SettingSchema):
        set_id = "probe.check.loop"
        schema_revision = 1
        uses: str = field(required=True, description="itself",
                          references="probe.check.loop")

    settings_ops.add_setting("probe.check.loop", 1, "x", {"uses": "x"}, org="acme")

    assert settings_ops.check_setting("probe.check.loop", "x", org="acme") == []


# ── an edge that is itself wrong ─────────────────────────────


def test_an_edge_naming_a_set_that_does_not_exist_says_so(acme):
    """Not the same as a row nobody has written.

    Reported as "no row under this key", a reader goes to provision it — and
    cannot, because a write to an unregistered schema is refused. A loop with
    no exit, from one typo. It has to be named for what it is.
    """
    @keyed_per_entity(key_strategy="probe_id")
    class Typo(SettingSchema):
        set_id = "probe.check.typo"
        schema_revision = 1
        uses: str = field(required=True, description="u",
                          references="probe.check.no-such-set")

    settings_ops.add_setting("probe.check.typo", 1, "a", {"uses": "x"}, org="acme")

    findings = settings_ops.check_setting("probe.check.typo", "a", org="acme")

    assert [f.kind for f in findings] == ["unknown_target"]
    assert "probe.check.no-such-set" in findings[0].detail
    assert "registry" in findings[0].looked_in, (
        "the answer came from this process's registry, and a set can be "
        "registered elsewhere and not here — the frame has to be stated")


def test_a_bad_edge_reads_differently_from_an_unwritten_row(acme):
    """The two must not be confusable; they call for opposite actions."""
    @keyed_per_entity(key_strategy="probe_id")
    class Bad(SettingSchema):
        set_id = "probe.check.bad-edge"
        schema_revision = 1
        uses: str = field(required=True, description="u",
                          references="probe.check.absent-set")

    settings_ops.add_setting("probe.check.bad-edge", 1, "a", {"uses": "x"},
                             org="acme")
    settings_ops.add_setting("probe.check.needs", 1, "unwritten",
                             {"uses": "nothing-here"}, org="acme")

    bad = settings_ops.check_setting("probe.check.bad-edge", "a", org="acme")
    unwritten = settings_ops.check_setting("probe.check.needs", "unwritten",
                                           org="acme")

    assert bad[0].kind != unwritten[0].kind
    assert {bad[0].kind, unwritten[0].kind} == {"unknown_target",
                                               "missing_reference"}


# ── edges that live in the key ───────────────────────────────


@pytest.fixture(scope="module")
def keyed_edges():
    """An entity, and rows keyed BY it — the edge a field cannot express."""
    @keyed_per_entity(key_strategy="probe_id")
    class Thing(SettingSchema):
        set_id = "probe.keyed.thing"
        schema_revision = 1
        v: str = field(required=True, description="v")

    @keyed_per_entity(
        key_strategy="thing_id:aspect",
        key_references={"thing_id": "probe.keyed.thing"},
    )
    class Aspect(SettingSchema):
        set_id = "probe.keyed.aspect"
        schema_revision = 1
        v: str = field(required=True, description="v")

    return Thing, Aspect


def test_rows_keyed_by_an_entity_are_found(acme, keyed_edges):
    settings_ops.add_setting("probe.keyed.thing", 1, "t1", {"v": "x"}, org="acme")
    for aspect in ("one", "two"):
        settings_ops.add_setting("probe.keyed.aspect", 1, f"t1:{aspect}",
                                 {"v": "x"}, org="acme")
    settings_ops.add_setting("probe.keyed.aspect", 1, "t2:one", {"v": "x"},
                             org="acme")

    found = settings_ops.rows_keyed_by("probe.keyed.thing", "t1", org="acme")

    assert sorted(k for _s, k, _seg in found) == ["t1:one", "t1:two"]
    assert {seg for _s, _k, seg in found} == {"thing_id"}


def test_an_entity_nothing_is_keyed_by_finds_nothing(acme, keyed_edges):
    assert settings_ops.rows_keyed_by(
        "probe.keyed.thing", "unreferenced", org="acme") == []


def test_a_check_reaches_rows_keyed_by_the_thing_checked(acme, keyed_edges):
    """The direction a field reference cannot go.

    A capability enable is not reachable from any field of a workspace — only
    from the key segment that names it.
    """
    settings_ops.add_setting("probe.keyed.thing", 1, "t3", {"v": "x"}, org="acme")
    settings_ops.add_setting("probe.keyed.aspect", 1, "t3:only", {"v": "x"},
                             org="acme")

    # Satisfied today; the point is that the walk visits the keyed row at all.
    seen: set = set()
    settings_ops.check_setting("probe.keyed.thing", "t3", org="acme", _seen=seen)

    assert ("probe.keyed.aspect", "t3:only", "acme") in seen


def test_a_row_keyed_by_something_deleted_is_an_orphan(acme, keyed_edges):
    """Nothing validates this at write, because provisioning order is
    legitimate — so the row outlives its entity and stays valid in every
    other respect."""
    settings_ops.add_setting("probe.keyed.aspect", 1, "never-existed:x",
                             {"v": "x"}, org="acme")

    findings = settings_ops.orphans_of("probe.keyed.aspect", org="acme")

    orphaned = [f for f in findings if "never-existed" in f.detail]
    assert orphaned and orphaned[0].kind == "orphaned_key"


def test_a_segment_the_key_strategy_does_not_have_is_refused():
    """A declaration naming a segment that is not in the key is a typo that
    would otherwise silently never match anything."""
    from tools.graph.schemas.registry import SchemaValidationError

    with pytest.raises(SchemaValidationError, match="does not have"):
        @keyed_per_entity(key_strategy="thing_id:aspect",
                          key_references={"thingId": "probe.keyed.thing"})
        class Typo(SettingSchema):
            set_id = "probe.keyed.typo"
            schema_revision = 1
            v: str = field(required=True, description="v")
