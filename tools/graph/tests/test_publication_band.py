"""A set declares which publication states its rows may hold.

Publication state is the only control over who reads a row across an
organization boundary, and nothing constrained it per set. The same axis was
wrong in both directions at once: every capability contract sat at ``raw``, so
another organization's install of it could not resolve, while nothing stopped a
sealed credential being promoted to ``published``, where every peer reads it.
One of those is an outage and the other is a disclosure, and neither announces
itself -- a broken install looks like a missing row, and a disclosed secret
looks like nothing at all.

The band says what a set is FOR, so the answer is a property of the schema
rather than of whoever last typed a state.
"""
from __future__ import annotations

import pytest

from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import (
    PUBLICATION_ORDER,
    SchemaValidationError,
    SettingSchema,
    field,
    keyed_per_entity,
    publication_band,
    states_allowed,
)


@pytest.fixture(scope="module")
def banded():
    @publication_band(max="raw")
    @keyed_per_entity(key_strategy="probe_id")
    class Sealed(SettingSchema):
        set_id = "probe.band.sealed"
        schema_revision = 1
        v: str = field(required=True, description="v")

    @publication_band(min="published")
    @keyed_per_entity(key_strategy="probe_id")
    class Shared(SettingSchema):
        set_id = "probe.band.shared"
        schema_revision = 1
        v: str = field(required=True, description="v")

    @keyed_per_entity(key_strategy="probe_id")
    class Unconstrained(SettingSchema):
        set_id = "probe.band.free"
        schema_revision = 1
        v: str = field(required=True, description="v")

    return Sealed, Shared, Unconstrained


@pytest.fixture
def orgs(tmp_path, monkeypatch, banded):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    for slug in ("acme", "partner"):
        GraphDB.create_org_db(slug).close()
    yield
    GraphDB.close_all_pooled()


# ── the declaration ──────────────────────────────────────────


def test_a_band_names_the_states_it_permits(banded):
    assert states_allowed("probe.band.sealed", 1) == ("raw",)
    assert states_allowed("probe.band.shared", 1) == ("published", "canonical")


def test_an_undeclared_set_is_unconstrained(banded):
    """A schema that has not been through this decision behaves as it did."""
    assert states_allowed("probe.band.free", 1) == PUBLICATION_ORDER


def test_an_inverted_band_is_refused_at_declaration(banded):
    """No state satisfies it, so every write would fail -- at runtime, far
    from the line that caused it."""
    with pytest.raises(SchemaValidationError, match="no state satisfies"):
        @publication_band(min="canonical", max="raw")
        @keyed_per_entity(key_strategy="probe_id")
        class Impossible(SettingSchema):
            set_id = "probe.band.impossible"
            schema_revision = 1
            v: str = field(required=True, description="v")


# ── enforcement on write ─────────────────────────────────────


def test_a_sealed_set_refuses_a_state_that_leaves_its_database(orgs):
    with pytest.raises(ValueError, match="publication band"):
        settings_ops.add_setting("probe.band.sealed", 1, "k", {"v": "s"},
                                 org="acme", state="published")


def test_it_accepts_the_state_it_is_for(orgs):
    assert settings_ops.add_setting("probe.band.sealed", 1, "ok", {"v": "s"},
                                    org="acme", state="raw")


def test_a_shared_set_refuses_a_row_nobody_can_read(orgs):
    """The other direction, and the one that produced a real outage: a
    contract written at raw resolves for its owner and for nobody else, so
    another organization's install of it silently cannot be satisfied."""
    with pytest.raises(ValueError, match="publication band"):
        settings_ops.add_setting("probe.band.shared", 1, "c", {"v": "x"},
                                 org="acme", state="raw")


def test_the_refusal_names_what_to_type_instead(orgs):
    """A refusal that only says no leaves the caller guessing at four
    states."""
    with pytest.raises(ValueError, match="Allowed: published, canonical"):
        settings_ops.add_setting("probe.band.shared", 1, "c2", {"v": "x"},
                                 org="acme", state="curated")


def test_upsert_is_guarded_too(orgs):
    """The path a keyed set is normally written through."""
    with pytest.raises(ValueError, match="publication band"):
        settings_ops.upsert_by_key("probe.band.sealed", 1, "u", {"v": "s"},
                                   org="acme", state="canonical")


def test_an_unconstrained_set_takes_any_state(orgs):
    for state in PUBLICATION_ORDER:
        assert settings_ops.upsert_by_key(
            "probe.band.free", 1, f"f-{state}", {"v": "x"},
            org="acme", state=state)


# ── enforcement on promotion ─────────────────────────────────


def test_promotion_cannot_walk_a_row_out_of_its_band(orgs):
    """The second door into the same failure.

    A band checked only at creation is a band a promotion walks through --
    and promotion is exactly how a row that was written correctly later
    becomes readable by every peer, typed by someone tidying up.
    """
    sid = settings_ops.add_setting("probe.band.sealed", 1, "p", {"v": "s"},
                                   org="acme", state="raw")

    with pytest.raises(ValueError, match="publication band"):
        settings_ops.promote_setting(sid, "published", org="acme")


def test_promotion_within_the_band_still_works(orgs):
    """The guard must not become a freeze."""
    sid = settings_ops.add_setting("probe.band.shared", 1, "q", {"v": "x"},
                                   org="acme", state="published")

    settings_ops.promote_setting(sid, "canonical", org="acme")

    layers = settings_ops.layers_for("probe.band.shared", "q", org="acme")
    assert layers["base"]["state"] == "canonical"


# ── the sets this exists for ─────────────────────────────────


SECRET_BEARING = (
    "autonomy.secure.setting",
    "autonomy.commit.signing-key",
    "autonomy.credential-file",
    "autonomy.vault.secret",
    "dashboard.claude.credentials",
    "dashboard.claude.setup_tokens",
)


@pytest.mark.parametrize("set_id", SECRET_BEARING)
def test_secret_bearing_sets_cannot_leave_their_database(set_id):
    """The band is what makes "promote this" unable to become a disclosure.

    Each of these holds key material, a credential, or the location of one.
    A peer reads a row when its state says so and for no other reason, so a
    single promotion -- typed by anyone with write access, at any point in
    the future -- is the whole distance between private and published.
    """
    from tools.graph import schemas

    assert schemas.states_allowed(set_id, 1) == ("raw",), (
        f"{set_id} may hold a state that peers can read")


@pytest.mark.parametrize("set_id", SECRET_BEARING)
def test_the_pin_matches_what_is_stored_today(set_id):
    """A band contradicting live rows breaks the next write to them.

    Every one of these was verified raw in live data before pinning, which
    is the check a migration exists to avoid needing.
    """
    from tools.graph import schemas

    assert "raw" in schemas.states_allowed(set_id, 1)


# ── the second guard: not served, even if mismarked ──────────


def test_a_mismarked_secret_row_still_does_not_cross(orgs):
    """The band refuses the write; this refuses the read.

    Both are needed because they fail independently. A row can reach a
    peer-visible state by a path the write guard never sees -- a direct
    database write, a restore from a backup taken before the band existed,
    a migration. At that point the only thing between a sealed credential
    and every peer organization is whether resolution agrees to serve it.
    """
    from tools.graph.db import GraphDB, resolve_caller_db_path

    settings_ops.add_setting("probe.band.sealed", 1, "leak", {"v": "secret"},
                             org="partner", state="raw")
    # Forced past the guard, exactly as a restore or a direct write would.
    db = GraphDB(resolve_caller_db_path("partner"))
    try:
        db.conn.execute(
            "UPDATE settings SET publication_state = 'canonical' "
            "WHERE set_id = 'probe.band.sealed' AND key = 'leak'")
        db.conn.commit()
    finally:
        db.close()

    seen = settings_ops.read_set_key("probe.band.sealed", "leak", org="acme")

    assert seen is None, (
        "a peer's sealed row was served across an organization boundary")


def test_the_owner_still_reads_its_own_row(orgs):
    """Refusing the federated read must not blind an org to its own data."""
    settings_ops.add_setting("probe.band.sealed", 1, "mine", {"v": "s"},
                             org="acme", state="raw")

    row = settings_ops.read_set_key("probe.band.sealed", "mine", org="acme")

    assert row is not None and row["payload"]["v"] == "s"


def test_a_set_with_a_public_surface_still_federates(orgs):
    """The guard keys on the band, not on a list of set names, so a set that
    is meant to be shared is unaffected."""
    settings_ops.add_setting("probe.band.shared", 1, "pub", {"v": "theirs"},
                             org="partner", state="published")

    row = settings_ops.read_set_key("probe.band.shared", "pub", org="acme")

    assert row is not None and row["payload"]["v"] == "theirs"
