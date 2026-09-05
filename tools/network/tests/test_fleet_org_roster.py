"""The fleet ORG roster — the unsigned, synced record of the orgs the operator
belongs to (so every fleet machine materialises the same org databases).

It follows the persona-setting precedent, not the machine roster: no signature
(it rides the already-authenticated personal-scope sync). The OR-set merge shape
still holds: concurrent enrolment of different orgs unions, one org renewed
concurrently resolves deterministically, a kick is absorbing unless a re-enrol
cites it, entries round-trip, and the recorded org_id survives.
"""

from __future__ import annotations

import pytest

from tools.network import fleet_org_roster
from tools.network.fleet_org_roster import EntryKind, FleetOrgRosterError


def test_concurrent_enrolment_of_different_orgs_unions():
    a = fleet_org_roster.enroll(org_slug="autonomy", org_id="id-a", seq=0)
    b = fleet_org_roster.enroll(org_slug="anchore", org_id="id-b", seq=0)
    roster = fleet_org_roster.resolve([a, b])
    assert set(roster) == {"autonomy", "anchore"}
    assert roster["autonomy"].org_id == "id-a"
    assert roster["anchore"].org_id == "id-b"


def test_one_org_renewed_concurrently_resolves_deterministically():
    a = fleet_org_roster.enroll(org_slug="autonomy", org_id="x", seq=1, issued_at=100)
    b = fleet_org_roster.enroll(org_slug="autonomy", org_id="x", seq=1, issued_at=200)
    assert a.entry_id != b.entry_id
    expected = a if a.entry_id < b.entry_id else b
    fwd = fleet_org_roster.resolve([a, b])
    rev = fleet_org_roster.resolve([b, a])
    assert fwd["autonomy"].entry_id == rev["autonomy"].entry_id == expected.entry_id


def test_higher_seq_wins():
    old = fleet_org_roster.enroll(org_slug="autonomy", org_id="x", seq=1)
    new = fleet_org_roster.enroll(org_slug="autonomy", org_id="x", seq=2)
    roster = fleet_org_roster.resolve([old, new])
    assert roster["autonomy"].seq == 2


def test_kick_is_absorbing_unless_cited():
    enrolled = fleet_org_roster.enroll(org_slug="autonomy", org_id="x", seq=1)
    kicked = fleet_org_roster.kick(org_slug="autonomy", org_id="x", seq=2)
    assert "autonomy" not in fleet_org_roster.resolve([enrolled, kicked])

    replay = fleet_org_roster.enroll(org_slug="autonomy", org_id="x", seq=3)
    assert "autonomy" not in fleet_org_roster.resolve([enrolled, kicked, replay])

    re = fleet_org_roster.reenroll(
        org_slug="autonomy", org_id="x", supersedes=kicked.entry_id, seq=3
    )
    roster = fleet_org_roster.resolve([enrolled, kicked, re])
    assert roster["autonomy"].kind == EntryKind.ENROLL


def test_a_kick_may_not_cite_a_supersedes():
    with pytest.raises(FleetOrgRosterError):
        fleet_org_roster._make(
            org_slug="autonomy", org_id="x", kind=EntryKind.KICK,
            seq=0, issued_at=0, supersedes="deadbeef",
        )


def test_empty_org_id_is_dropped_on_resolve():
    good = fleet_org_roster.enroll(org_slug="autonomy", org_id="x")
    bad = fleet_org_roster.OrgRosterEntry(
        org_slug="autonomy", org_id="", kind=EntryKind.ENROLL, seq=5, issued_at=0,
    )
    # The malformed higher-seq entry is dropped; the good one still holds.
    roster = fleet_org_roster.resolve([good, bad])
    assert roster["autonomy"].org_id == "x"


def test_entry_round_trips_through_dict():
    e = fleet_org_roster.enroll(org_slug="autonomy", org_id="id-a", seq=2, issued_at=7)
    again = fleet_org_roster.OrgRosterEntry.from_dict(e.to_dict())
    assert again == e
    assert again.entry_id == e.entry_id


@pytest.fixture
def personal_store(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    root = tmp_path / "orgs"
    root.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal").close()
    yield root
    GraphDB.close_all_pooled()


def test_publish_then_materialize_creates_the_stub_with_matching_id(personal_store):
    """The full local chain: server-side publish (no seed) -> synced roster ->
    a fresh member materialises the org DB stub with the SAME org id -> the
    scope is now discoverable for the sync engine. This is the whole bootstrap
    that closes the org chicken-and-egg."""
    from tools.graph.db import GraphDB
    from tools.network.fleet_sync_scheduler import (
        discover_org_sync_scopes,
        materialize_org_scopes_from_roster,
    )

    # Publish is idempotent and needs no seed.
    assert fleet_org_roster.publish_org("autonomy", "org-uuid-123") is True
    assert fleet_org_roster.publish_org("autonomy", "org-uuid-123") is False
    assert "autonomy" in fleet_org_roster.current_orgs()

    # Before materialisation the member has no org scope.
    assert "autonomy" not in discover_org_sync_scopes()

    created = materialize_org_scopes_from_roster()
    assert created == ["autonomy"]

    # The stub is a discoverable scope carrying the RECORDED org identity.
    scopes = discover_org_sync_scopes()
    assert "autonomy" in scopes
    db = GraphDB(scopes["autonomy"])
    try:
        row = db.conn.execute("SELECT id, slug, type FROM orgs").fetchone()
    finally:
        db.close()
    assert row[0] == "org-uuid-123"
    assert row[1] == "autonomy"
    assert row[2] == "shared"

    # Idempotent: a second pass creates nothing.
    assert materialize_org_scopes_from_roster() == []
