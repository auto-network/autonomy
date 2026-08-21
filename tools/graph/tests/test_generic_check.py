"""``check_setting`` — is this row satisfied, and everything it depends on?

Metadata-driven end to end. It follows fields declaring ``references`` and
asks fields declaring ``exists``, and it contains no name of any particular
set, so adding a setting requires no code here. The proof is that these tests
invent their own schemas: the checker has never heard of them and walks them
anyway.

Two things it deliberately does not do. It does not run readiness checks at
write — whether a file is present is a fact about the world, not the value,
and enforcing it at write would make an organization's row refusable on one
machine and acceptable on another. And it does not invent relationships:
field references, key references, environment/path predicates and specialized
schema hooks all have to be declared before the walk follows them.
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


# ── what counts as existing ──────────────────────────────────


@pytest.fixture
def two_orgs(tmp_path, monkeypatch, invented):
    """``acme`` refers to something ``partner`` owns and publishes."""
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    for slug in ("acme", "partner"):
        GraphDB.create_org_db(slug).close()
    yield
    GraphDB.close_all_pooled()


@pytest.fixture(scope="module")
def shared():
    @keyed_per_entity(key_strategy="thing_id")
    class Thing(SettingSchema):
        set_id = "probe.shared.thing"
        schema_revision = 1
        v: str = field(required=True, description="v")

    @keyed_per_entity(key_strategy="thing_id",
                      key_references={"thing_id": "probe.shared.thing"})
    class Uses(SettingSchema):
        set_id = "probe.shared.uses"
        schema_revision = 1
        v: str = field(required=True, description="v")

    return Thing, Uses


def test_a_row_another_org_publishes_is_present(two_orgs, shared):
    """Existence is asked with the visibility its consumer has.

    An organization reads its subscribed peers, so a row one of them
    publishes is as available to the software as one of its own. Answering
    from the owning database alone reports a working configuration as
    dangling, and the report is the more convincing for being specific.
    """
    settings_ops.upsert_by_key("probe.shared.thing", 1, "shared-thing",
                               {"v": "x"}, org="partner", state="published")
    settings_ops.add_setting("probe.shared.uses", 1, "shared-thing",
                             {"v": "x"}, org="acme")

    assert settings_ops.orphans_of("probe.shared.uses", org="acme") == []


def test_a_row_another_org_keeps_private_is_absent(two_orgs, shared):
    """The other half of the same rule.

    A row a peer does not publish is not readable, so nothing in this
    organization can resolve it — which is what makes the row keyed by it
    genuinely orphaned rather than merely owned elsewhere.
    """
    settings_ops.upsert_by_key("probe.shared.thing", 1, "private-thing",
                               {"v": "x"}, org="partner", state="raw")
    settings_ops.add_setting("probe.shared.uses", 1, "private-thing",
                             {"v": "x"}, org="acme")

    findings = settings_ops.orphans_of("probe.shared.uses", org="acme")

    assert [f.kind for f in findings] == ["orphaned_key"]


def test_the_frame_names_the_peers_it_consulted(two_orgs, shared):
    """"Not found in acme" and "not found in acme or anything it reads" are
    different claims, and only the second justifies deleting the row."""
    settings_ops.add_setting("probe.shared.uses", 1, "nowhere", {"v": "x"},
                             org="acme")

    findings = settings_ops.orphans_of("probe.shared.uses", org="acme")

    assert "peers" in findings[0].looked_in


# ── one answer to "where does this row live" ─────────────────


@pytest.fixture(scope="module")
def org_scoped():
    """A target with no declared home, referred to by an org-scoped field.

    The key carries the organization, which means the store holds several of
    them — the operator's own. An organization's own database would have no
    reason to repeat which organization it is.
    """
    @keyed_per_entity(key_strategy="secret_name")
    class Sealed(SettingSchema):
        set_id = "probe.scoped.sealed"
        schema_revision = 1
        v: str = field(required=True, description="v")

    @keyed_per_entity(key_strategy="probe_id")
    class Wants(SettingSchema):
        set_id = "probe.scoped.wants"
        schema_revision = 1
        unlocks: str = field(required=True, description="a sealed value",
                             references="probe.scoped.sealed",
                             reference_scope="org")

    return Sealed, Wants


def test_both_paths_look_in_the_same_place(acme, org_scoped):
    """The defect this exists to prevent.

    ``unresolved_references`` and ``check_setting`` each decided where an
    org-scoped target lived, and decided differently — one read the
    operator's store, the other the organization's. Both reported their
    answer as though it settled the question, so the same row was
    simultaneously provisioned and missing depending on which verb asked.
    """
    settings_ops.add_setting("probe.scoped.sealed", 1, "acme:thing",
                             {"v": "x"}, org="personal")
    payload = {"unlocks": "thing"}
    settings_ops.add_setting("probe.scoped.wants", 1, "w", payload, org="acme")

    assert settings_ops.unresolved_references(
        "probe.scoped.wants", 1, payload, org="acme") == []
    assert settings_ops.check_setting(
        "probe.scoped.wants", "w", org="acme") == []


def test_both_paths_agree_when_it_is_absent(acme, org_scoped):
    """Agreeing only when satisfied would leave the disagreement in place."""
    payload = {"unlocks": "never-sealed"}
    settings_ops.add_setting("probe.scoped.wants", 1, "u", payload, org="acme")

    by_field = settings_ops.unresolved_references(
        "probe.scoped.wants", 1, payload, org="acme")
    by_walk = settings_ops.check_setting("probe.scoped.wants", "u", org="acme")

    assert [key for _t, key in by_field] == ["acme:never-sealed"]
    assert [f.kind for f in by_walk] == ["missing_reference"]
    assert "operator" in by_walk[0].looked_in, (
        "the frame has to name the store that actually answered")


# ── written, but not readable ────────────────────────────────


def test_a_row_a_peer_keeps_private_says_so_rather_than_missing(two_orgs, shared):
    """"Nobody wrote it" and "you may not read it" need opposite repairs.

    A plain read reports both as nothing. A reader told the row is absent
    writes one, and now two rows exist under the same key in different
    organizations, disagreeing, with nothing recording which one anything
    resolved. So the report has to name the owner and the state.
    """
    settings_ops.upsert_by_key("probe.shared.thing", 1, "held-back",
                               {"v": "x"}, org="partner", state="raw")
    settings_ops.add_setting("probe.shared.uses", 1, "u",
                             {"v": "x"}, org="acme")

    findings = settings_ops.check_setting("probe.shared.uses", "u", org="acme")
    keyed = settings_ops.check_setting("probe.shared.thing", "held-back",
                                       org="acme")

    assert [f.kind for f in keyed] == ["unreadable_reference"]
    assert "partner" in keyed[0].detail and "raw" in keyed[0].detail


def test_a_row_nobody_wrote_is_still_reported_missing(two_orgs, shared):
    """The distinction is only worth anything if it discriminates."""
    findings = settings_ops.check_setting("probe.shared.thing", "nowhere-at-all",
                                          org="acme")

    assert [f.kind for f in findings] == ["missing_reference"]


def test_a_published_row_is_neither(two_orgs, shared):
    settings_ops.upsert_by_key("probe.shared.thing", 1, "open",
                               {"v": "x"}, org="partner", state="published")

    assert settings_ops.check_setting("probe.shared.thing", "open",
                                      org="acme") == []


# ── what a layers view must not merge ────────────────────────


def test_layers_does_not_merge_a_deprecated_override(acme, monkeypatch):
    """A view that merges rows resolution skips describes a value nothing
    returns.

    This was not theoretical. A deprecated override carrying a model pin was
    merged into a workspace's base by a migration built on this view, which
    would have silently re-pinned that workspace from a row the platform had
    already retired.
    """
    from tools.graph.db import GraphDB, resolve_caller_db_path

    base = settings_ops.add_setting("probe.check.plain", 1, "dep", {"v": "live"},
                                    org="acme")
    # A pre-existing chain, as the live data holds: written before amendment
    # collapsed into the row it amends.
    monkeypatch.setattr(settings_ops, "_collapse_amendment",
                        lambda *a, **k: None)
    settings_ops.override_setting(base, {"v": "retired"}, org="acme")

    db = GraphDB(resolve_caller_db_path("acme"))
    try:
        db.conn.execute(
            "UPDATE settings SET deprecated = 1 WHERE supersedes = ?", (base,))
        db.conn.commit()
    finally:
        db.close()

    layers = settings_ops.layers_for("probe.check.plain", "dep", org="acme")
    resolved = settings_ops.read_set_key("probe.check.plain", "dep",
                                         org="acme", peers=[])

    assert layers["resolved"] == resolved["payload"], (
        "the view and the resolver disagree about the same key")
    assert layers["resolved"]["v"] == "live"
    assert layers["overrides"] == []
    assert len(layers["deprecated"]) == 1, (
        "a retired row should still be visible, just not applied")


def test_layers_reports_the_rows_own_revision(acme):
    """A consumer that rewrites the row needs to know which schema it is.

    The migration built on this view assumed revision 1 for everything and
    wrote revision-2 rows against revision 1's schema. That rejected
    payloads which were valid where they came from, and would silently have
    downgraded any it happened to accept -- so the view has to carry the
    revision rather than let a caller guess it.
    """
    settings_ops.add_setting("probe.check.plain", 1, "rev", {"v": "x"},
                             org="acme")

    layers = settings_ops.layers_for("probe.check.plain", "rev", org="acme")

    assert layers["base"]["schema_revision"] == 1, (
        "a caller cannot rewrite a row correctly without its revision")


def test_a_composed_read_says_it_was_composed(acme, monkeypatch, capsys):
    """The fix for the wrong mental model, at its source.

    Every read surface returns a merged payload, so the store presents as a
    dictionary of key to value while it is really rows and layers. When a
    write then appears to do nothing there is nowhere to look. One line on
    a composed read is what makes the layering visible in the surface people
    use daily, rather than in a command they would have to already suspect.
    """
    from tools.graph import set_cmd

    base = settings_ops.add_setting("probe.check.plain", 1, "comp",
                                    {"v": "1"}, org="acme")
    monkeypatch.setattr(settings_ops, "_collapse_amendment",
                        lambda *a, **k: None)
    settings_ops.override_setting(base, {"v": "2"}, org="acme")

    set_cmd._print_composition("probe.check.plain", "comp", "acme")

    assert "composed from a base plus 1 override" in capsys.readouterr().err


def test_a_single_row_read_stays_quiet(acme, capsys):
    """The common case must not become noise, or the line stops being read."""
    from tools.graph import set_cmd

    settings_ops.add_setting("probe.check.plain", 1, "single", {"v": "1"},
                             org="acme")

    set_cmd._print_composition("probe.check.plain", "single", "acme")

    assert capsys.readouterr().err == ""


def test_the_command_a_message_tells_you_to_run_exists(acme, monkeypatch, capsys):
    """A report that names a command must name one that parses.

    The composition line shipped telling readers to run
    `graph set layers <set_id> <key>` while the command takes --key. A
    diagnostic that hands out an invocation which errors is worse than one
    that hands out none, because the reader concludes the tool is broken
    rather than that the instruction was.
    """
    import argparse
    import shlex

    from tools.graph import set_cmd

    base = settings_ops.add_setting("probe.check.plain", 1, "cmd", {"v": "1"},
                                    org="acme")
    monkeypatch.setattr(settings_ops, "_collapse_amendment",
                        lambda *a, **k: None)
    settings_ops.override_setting(base, {"v": "2"}, org="acme")

    set_cmd._print_composition("probe.check.plain", "cmd", "acme")
    suggested = capsys.readouterr().err.split("`")[1]

    parser = argparse.ArgumentParser(prog="graph")
    set_cmd.attach_set_subparser(parser.add_subparsers(dest="command"))

    # shlex minus the leading "graph" -- the real argv the reader would type.
    parsed = parser.parse_args(shlex.split(suggested)[1:])

    assert parsed.key == "cmd"
    assert parsed.set_at_rev == "probe.check.plain"
    assert parsed.func is set_cmd.cmd_set_layers


# ── a value that names a host environment variable ───────────
#
# The launcher forwards the variables that are set and skips the rest
# without a word (three call sites, all ``if value is not None``). So a
# workspace declaring one that is unset starts without it, and whatever
# needed the value fails much later saying nothing about a forward that
# never happened. Declaring what the field HOLDS is what turns that into
# something a check can say in advance.


@pytest.fixture(scope="module")
def env_naming():
    @keyed_per_entity(key_strategy="probe_id")
    class Forwards(SettingSchema):
        set_id = "probe.check.env"
        schema_revision = 1
        wants: list = field(
            required=True,
            description="host env var names",
            element=str,
            names_host_env=True,
        )

    return Forwards


@pytest.fixture
def env_org(tmp_path, monkeypatch, env_naming):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.create_org_db("acme").close()
    yield
    GraphDB.close_all_pooled()


def test_a_named_variable_that_is_not_set_is_reported(env_org, monkeypatch):
    monkeypatch.delenv("PROBE_ABSENT", raising=False)
    settings_ops.add_setting("probe.check.env", 1, "k", {"wants": ["PROBE_ABSENT"]},
                             org="acme", state="raw")

    findings = settings_ops.check_setting("probe.check.env", "k", org="acme")

    assert [f.kind for f in findings] == ["missing_env"]
    assert "PROBE_ABSENT" in findings[0].detail


def test_a_named_variable_that_is_set_is_not(env_org, monkeypatch):
    monkeypatch.setenv("PROBE_PRESENT", "x")
    settings_ops.add_setting("probe.check.env", 1, "s", {"wants": ["PROBE_PRESENT"]},
                             org="acme", state="raw")

    assert settings_ops.check_setting("probe.check.env", "s", org="acme") == []


def test_an_empty_value_counts_as_set(env_org, monkeypatch):
    """Forwarding is by presence, not by truthiness.

    The launcher tests ``is not None``, so an exported-but-empty variable
    IS forwarded. A check calling that missing would report a problem the
    launcher does not have, and send someone to fix a variable that is
    already doing what it will do.
    """
    monkeypatch.setenv("PROBE_EMPTY", "")
    settings_ops.add_setting("probe.check.env", 1, "e", {"wants": ["PROBE_EMPTY"]},
                             org="acme", state="raw")

    assert settings_ops.check_setting("probe.check.env", "e", org="acme") == []


def test_each_unset_variable_is_named_separately(env_org, monkeypatch):
    """One finding per variable. A single "some of these are missing" is a
    finding somebody has to go and expand by hand."""
    for name in ("PROBE_A", "PROBE_B"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PROBE_C", "here")
    settings_ops.add_setting("probe.check.env", 1, "m",
                             {"wants": ["PROBE_A", "PROBE_C", "PROBE_B"]},
                             org="acme", state="raw")

    findings = settings_ops.check_setting("probe.check.env", "m", org="acme")

    assert len(findings) == 2
    assert {"PROBE_A", "PROBE_B"} == {
        n for n in ("PROBE_A", "PROBE_B")
        for f in findings if n in f.detail
    }


def test_the_finding_says_whose_environment_it_read(env_org, monkeypatch):
    """The frame is load-bearing here in a way it is not for a file.

    A variable exported in the operator's shell is not set in the dashboard
    process, and neither is the container's. So this check is only decisive
    when run in the process that will do the forwarding, and the finding has
    to say so rather than let a clean result be read as a guarantee.
    """
    monkeypatch.delenv("PROBE_FRAME", raising=False)
    settings_ops.add_setting("probe.check.env", 1, "f", {"wants": ["PROBE_FRAME"]},
                             org="acme", state="raw")

    finding = settings_ops.check_setting("probe.check.env", "f", org="acme")[0]

    assert "environment of the process running this check" in finding.looked_in
    assert "launcher" in finding.looked_in


def test_a_readiness_check_on_env_never_runs_at_write(env_org, monkeypatch):
    """Same rule as a path: whether a variable is exported is a fact about
    the world, and an organization's row must not be refusable on one host
    and acceptable on the next."""
    monkeypatch.delenv("PROBE_AT_WRITE", raising=False)

    assert settings_ops.add_setting(
        "probe.check.env", 1, "w", {"wants": ["PROBE_AT_WRITE"]},
        org="acme", state="raw")


def test_a_declared_fixed_environment_fallback_satisfies_the_name(
    env_org, monkeypatch,
):
    @keyed_per_entity(key_strategy="probe_id")
    class EffectiveEnv(SettingSchema):
        set_id = "probe.check.effective-env"
        schema_revision = 1
        fixed: dict = field(required=True, description="fixed environment")
        forwarded: list = field(
            required=True,
            description="host environment names",
            element=str,
            names_host_env=True,
            env_fallback_field="fixed",
        )

    monkeypatch.delenv("PROBE_FIXED", raising=False)
    settings_ops.add_setting(
        "probe.check.effective-env", 1, "k",
        {"fixed": {"PROBE_FIXED": "configured"}, "forwarded": ["PROBE_FIXED"]},
        org="acme", state="raw",
    )

    findings, satisfied = settings_ops.inspect_setting(
        "probe.check.effective-env", "k", org="acme",
    )

    assert findings == []
    available = [item for item in satisfied if item.kind == "available_env"]
    assert [item.subject for item in available] == ["PROBE_FIXED"]
    assert "configured" not in available[0].detail
    assert "fixed 'fixed' workspace environment" in available[0].detail


def test_an_env_fallback_without_an_env_check_is_refused():
    with pytest.raises(SchemaValidationError, match="does not declare names_host_env"):
        @keyed_per_entity(key_strategy="probe_id")
        class BadFallback(SettingSchema):
            set_id = "probe.check.bad-env-fallback"
            schema_revision = 1
            value: str = field(
                required=True,
                description="not an environment variable name",
                env_fallback_field="fixed",
            )


# ── a path whose filesystem is not this process's ────────────
#
# A bind-mount source belongs to the machine running the platform. Asked
# from inside a container the question has two wrong answers and no right
# one, and the dangerous one is not the false alarm.


@pytest.fixture(scope="module")
def host_framed():
    @keyed_per_entity(key_strategy="probe_id")
    class Mounts(SettingSchema):
        set_id = "probe.check.hostframe"
        schema_revision = 1
        host_path: str = field(
            required=True,
            description="a directory on the platform host",
            exists="dir",
            exists_frame="platform-host",
        )

    return Mounts


@pytest.fixture
def frame_org(tmp_path, monkeypatch, host_framed):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    GraphDB.create_org_db("acme").close()
    yield tmp_path
    GraphDB.close_all_pooled()


def test_a_container_refuses_to_answer_for_the_host(frame_org, monkeypatch):
    monkeypatch.setenv("AUTONOMY_CONTAINER", "1")
    settings_ops.add_setting("probe.check.hostframe", 1, "k",
                             {"host_path": "/data/thing"}, org="acme", state="raw")

    findings = settings_ops.check_setting("probe.check.hostframe", "k", org="acme")

    assert [f.kind for f in findings] == ["unanswerable_here"]
    assert "run this check on the host" in findings[0].detail


def test_a_directory_present_only_in_the_container_does_not_read_as_ready(
    frame_org, monkeypatch,
):
    """The reason this refuses instead of answering with a caveat.

    A same-named directory inside the container satisfies the filesystem
    question and the check turns GREEN for a bind-mount source the host does
    not have. Nobody re-reads a clean result, so the caveat would never be
    seen — the workspace would simply fail at launch, having been declared
    ready.
    """
    monkeypatch.setenv("AUTONOMY_CONTAINER", "1")
    local = frame_org / "looks-right"
    local.mkdir()
    settings_ops.add_setting("probe.check.hostframe", 1, "d",
                             {"host_path": str(local)}, org="acme", state="raw")

    findings = settings_ops.check_setting("probe.check.hostframe", "d", org="acme")

    assert [f.kind for f in findings] == ["unanswerable_here"], (
        "a directory that exists only in this container reported the host's "
        "bind-mount source as satisfied")


def test_on_the_host_the_question_is_answered_normally(frame_org, monkeypatch):
    monkeypatch.setenv("AUTONOMY_CONTAINER", "0")
    settings_ops.add_setting("probe.check.hostframe", 1, "h",
                             {"host_path": "/definitely/not/here"},
                             org="acme", state="raw")

    findings = settings_ops.check_setting("probe.check.hostframe", "h", org="acme")

    assert [f.kind for f in findings] == ["missing_path"]


def test_on_the_host_a_present_directory_is_satisfied(frame_org, monkeypatch):
    monkeypatch.setenv("AUTONOMY_CONTAINER", "0")
    present = frame_org / "real"
    present.mkdir()
    settings_ops.add_setting("probe.check.hostframe", 1, "p",
                             {"host_path": str(present)}, org="acme", state="raw")

    assert settings_ops.check_setting("probe.check.hostframe", "p", org="acme") == []


def test_an_undeclared_frame_is_answered_wherever_it_is_asked(acme):
    """Only a declared frame changes behaviour, so every existing check and
    every schema that has not considered the question is untouched."""
    settings_ops.add_setting("probe.check.local", 1, "u",
                             {"path": "/nowhere/at/all"}, org="machine")

    findings = settings_ops.check_setting("probe.check.local", "u", org="machine")

    assert [f.kind for f in findings] == ["missing_path"]


def test_a_frame_without_an_exists_check_is_refused_at_declaration():
    """It says whose filesystem an exists check reads. With no such check it
    reads as a constraint and is nothing at all."""
    from tools.graph.schemas.registry import SchemaValidationError as SVE

    with pytest.raises(SVE, match="declares no exists check"):
        @keyed_per_entity(key_strategy="probe_id")
        class Bad(SettingSchema):
            set_id = "probe.check.badframe"
            schema_revision = 1
            v: str = field(required=True, description="v",
                           exists_frame="platform-host")


# ── what stops it, and what only degrades it ─────────────────
#
# "Everything declared is present" and "this can run" are different
# questions. Printed as one list, a missing local clone source reads
# exactly like a missing credential — so the reader either treats every
# finding as fatal or learns to treat none of them as fatal.


@pytest.fixture(scope="module")
def graded():
    from tools.graph.schemas.registry import readiness_gated_by

    @keyed_per_entity(key_strategy="probe_id")
    class Mixed(SettingSchema):
        set_id = "probe.sev.mixed"
        schema_revision = 1
        needed: str = field(required=True, description="required file",
                            exists="file")
        shortcut: str = field(required=False, description="a faster source",
                              exists="file", severity="advisory")

    @readiness_gated_by("wanted")
    @keyed_per_entity(key_strategy="probe_id")
    class Optional(SettingSchema):
        set_id = "probe.sev.gated"
        schema_revision = 1
        wanted: bool = field(required=True, description="is this row needed")
        thing: str = field(required=True, description="a file", exists="file")

    return Mixed, Optional


@pytest.fixture
def sev_org(tmp_path, monkeypatch, graded):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.setenv("AUTONOMY_CONTAINER", "0")
    GraphDB.create_org_db("acme").close()
    yield
    GraphDB.close_all_pooled()


def test_an_undeclared_requirement_blocks(sev_org):
    """Blocking is the default and is never declared. Whoever wrote a
    requirement was saying it is needed."""
    settings_ops.add_setting("probe.sev.mixed", 1, "a",
                             {"needed": "/nope/a"}, org="acme")

    findings = settings_ops.check_setting("probe.sev.mixed", "a", org="acme")

    assert [f.severity for f in findings] == ["blocking"]


def test_a_declared_advisory_does_not(sev_org):
    settings_ops.add_setting("probe.sev.mixed", 1, "b",
                             {"needed": "/nope/b", "shortcut": "/nope/fast"},
                             org="acme")

    findings = settings_ops.check_setting("probe.sev.mixed", "b", org="acme")

    by_field = {("shortcut" if "shortcut" in f.detail else "needed"): f.severity
                for f in findings}
    assert by_field == {"needed": "blocking", "shortcut": "advisory"}


def test_a_row_that_says_it_is_optional_downgrades_everything_it_asks_for(sev_org):
    """The fact was already in the payload; only the way to read it was
    missing. An optional mount's absent directory is worth reporting and is
    not a broken launch."""
    settings_ops.add_setting("probe.sev.gated", 1, "off",
                             {"wanted": False, "thing": "/nope/opt"}, org="acme")

    findings = settings_ops.check_setting("probe.sev.gated", "off", org="acme")

    assert [f.severity for f in findings] == ["advisory"]


def test_the_same_row_marked_needed_blocks(sev_org):
    """The control. Without it, a gate that downgraded every row would pass
    the assertion above."""
    settings_ops.add_setting("probe.sev.gated", 1, "on",
                             {"wanted": True, "thing": "/nope/req"}, org="acme")

    findings = settings_ops.check_setting("probe.sev.gated", "on", org="acme")

    assert [f.severity for f in findings] == ["blocking"]


def test_a_gate_naming_a_field_the_schema_lacks_is_refused():
    """Silently, it would gate on a field that is never set — so every row
    reads as optional and nothing ever blocks."""
    from tools.graph.schemas.registry import (
        SchemaValidationError as SVE, readiness_gated_by,
    )

    with pytest.raises(SVE, match="not a field it declares"):
        @readiness_gated_by("nonexistent")
        @keyed_per_entity(key_strategy="probe_id")
        class Bad(SettingSchema):
            set_id = "probe.sev.badgate"
            schema_revision = 1
            _field_metadata = {"v": {"type": "string", "description": "v"}}


def test_an_unknown_severity_is_refused_at_declaration():
    from tools.graph.schemas.registry import SchemaValidationError as SVE

    with pytest.raises(SVE, match="severity must be one of"):
        @keyed_per_entity(key_strategy="probe_id")
        class Bad(SettingSchema):
            set_id = "probe.sev.badsev"
            schema_revision = 1
            v: str = field(required=True, description="v", severity="maybe")
