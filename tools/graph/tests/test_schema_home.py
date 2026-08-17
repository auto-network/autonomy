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


def test_an_organization_setting_accepts_the_operators_own_store(
        homed_schemas, orgs_root):
    """`organization` is a decision marker, not a prohibition.

    It records that someone asked "must this be forced into the operator's
    store or this machine's?" and answered no. It does NOT mean "anywhere
    except the operator's" -- the operator owns workspaces, and their
    database is the organizational home of their own things, mounts and
    commit policies included.

    Read the other way it refused writes that were correct, which is what
    this test used to assert.
    """
    sid = settings_ops.add_setting(
        "probe.home.ours", 1, "ws-a", {"v": "x"}, org="personal")

    assert sid
    row = settings_ops.read_set_key("probe.home.ours", "ws-a",
                                    org="personal", peers=[])
    assert row["payload"]["v"] == "x"


def test_an_organization_setting_still_refuses_the_machine_store(
        homed_schemas, orgs_root):
    """The one rule it keeps, and it is not its own: every declared home
    that is not `machine` already refuses the machine store, because a store
    that never leaves this computer reaches nobody who needs the value."""
    with pytest.raises(SchemaValidationError, match="never leaves the machine"):
        settings_ops.add_setting(
            "probe.home.ours", 1, "ws-b", {"v": "x"}, org="machine")


def test_a_read_resolves_to_the_home_instead_of_refusing(homed_schemas, orgs_root):
    """Reads route, writes refuse -- and the asymmetry is the point.

    This test previously asserted that a read from the wrong organization
    raises, on the reasoning that a read silently finding nothing in the
    wrong store is the harder bug of the two. That reasoning was right about
    the danger and wrong about the options: it assumed the alternative to
    refusing was looking in the wrong place. Resolving to the declared home
    is a third answer, and it removes the danger by construction -- there is
    no wrong store left to look in.

    Refusing broke every consumer that legitimately scopes by organization.
    `/api/sign-key?org=anchore` asks which KEY, not which database, and the
    guard turned it into a 500 that the operator's overlay reported as "User
    declined signing request".
    """
    settings_ops.add_setting("probe.home.mine", 1, "acme", {"v": "p"},
                             org="personal")

    seen = settings_ops.read_set("probe.home.mine", org="acme", peers=[])

    assert [m.key for m in seen.members] == ["acme"]


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


# ── a read resolves to the home; it does not refuse the caller ──


def test_a_read_scoped_to_an_org_finds_a_personal_homed_set(homed_schemas, orgs_root):
    """The org names WHICH KEY, not which database.

    `/api/sign-key?org=anchore` asks for the signing key anchore's commits
    are signed with. The key is personal-homed -- one operator, one store --
    so the organization is a key component, and reading it at the caller's
    org made the guard refuse a request that was entirely correct.

    It surfaced as HTTP 500, and the operator's overlay reported it as "User
    declined signing request". A refusal that misreports itself as a human
    decision is worse than the misrouting it was added to prevent.
    """
    # Every organization has a database in production; the fixture only makes
    # one, and a read as an organization also opens that organization's own
    # store.
    root = orgs_root
    for slug in ("anchore", "autonomy"):
        GraphDB.create_org_db(slug, path=root / f"{slug}.db").close()
    settings_ops.add_setting("probe.home.mine", 1, "anchore",
                             {"v": "anchore-key"}, org="personal")

    for caller in ("anchore", "autonomy", "personal", None):
        row = settings_ops.read_set_key(
            "probe.home.mine", "anchore", org=caller, peers=[])
        assert row is not None, (
            f"a read as {caller!r} could not reach a personal-homed set")
        assert row["payload"]["v"] == "anchore-key"


def test_a_write_to_the_wrong_home_is_still_refused(homed_schemas, orgs_root):
    """Reads route; writes must not.

    A write landing in the wrong database is a value nobody can find or
    everybody can read, and neither failure announces itself. That is what
    the declaration exists to prevent, so routing a write would remove the
    only protection while looking like a convenience.
    """
    with pytest.raises(SchemaValidationError, match="operator's own database"):
        settings_ops.add_setting("probe.home.mine", 1, "acme",
                                 {"v": "x"}, org="acme")


def test_an_organization_homed_set_still_reads_at_the_callers_org(homed_schemas, orgs_root):
    """Routing applies where a home names ONE database. `organization` names
    a class of them, so the caller's org is the right answer and refusing a
    personal caller stays correct."""
    settings_ops.add_setting("probe.home.ours", 1, "ws-a", {"v": "o"},
                             org="acme")

    seen = settings_ops.read_set("probe.home.ours", org="acme", peers=[])

    assert [m.key for m in seen.members] == ["ws-a"]


# ── credentials belong to the operator, and it is declared ──


@pytest.mark.parametrize("set_id", [
    "dashboard.claude.setup_tokens",
    "dashboard.claude.credentials",
    "dashboard.codex.credentials",
])
def test_harness_credentials_declare_the_operators_store(set_id):
    """Every writer already named `personal` by a module constant, and the
    launcher read it back the same way -- so the home was agreed and not
    enforced. An undeclared home cannot refuse a write into an
    organization's database, and it left a bare `graph set members` looking
    in the caller's own store and answering "(no Settings)" for rows that
    plainly existed one database over.
    """
    from tools.graph import schemas

    assert schemas.declared_home(set_id) == "personal"


@pytest.mark.parametrize("set_id", [
    "dashboard.claude.setup_tokens",
    "dashboard.claude.credentials",
])
def test_they_also_cannot_be_published(set_id):
    """Home says which database; the band says who may read across one. A
    credential needs both answers, and they are different questions."""
    from tools.graph import schemas

    assert schemas.states_allowed(set_id, 1) == ("raw",)


def test_a_sweep_across_every_store_never_raises(homed_schemas, orgs_root):
    """The outage this cost twelve minutes to learn.

    Readers exist that walk every database -- the session monitor does, on a
    timer. Reaching the machine store, an organization-homed set raised
    instead of returning no rows. The exception was thrown inside a timer
    task and never retrieved, so the dashboard did not crash: it kept the
    port and stopped answering, and every graph call across the fleet hung
    rather than failing fast.

    A guard meant to stop a value being WRITTEN where nobody could find it
    took down the platform on a READ. Reads resolve or come back empty; they
    do not refuse.
    """
    settings_ops.add_setting("probe.home.ours", 1, "ws-a", {"v": "x"},
                             org="acme")

    for store in ("acme", "personal", "machine", None):
        members = settings_ops.read_set("probe.home.ours", org=store, peers=[])
        assert isinstance(getattr(members, "members", None), list)


def test_the_machine_store_simply_holds_none_of_it(homed_schemas, orgs_root):
    """Empty is the honest answer, and it is a different answer from an
    error. The value is not there; nothing has gone wrong."""
    settings_ops.add_setting("probe.home.ours", 1, "ws-b", {"v": "x"},
                             org="acme")

    seen = settings_ops.read_set("probe.home.ours", org="machine", peers=[])

    assert [m.key for m in seen.members] == []
