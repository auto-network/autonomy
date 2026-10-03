"""The roster persists as raw personal.db rows (auto-0vpse acceptance).

Bullet 6: the rows are `raw`; a test asserts they are never written at
`published` or `canonical`. Plus a personal.db roundtrip: store a set of
entries, read them back, and resolve to the same roster the in-memory merge
gives. Storage is settings members keyed per entry so relay's sync unions
them (confirmed with auto-0811-093753).
"""

from __future__ import annotations

import pytest

from tools.graph.db import GraphDB
from tools.graph import settings_ops
from tools.network.idkit import KeyPair
from tools.network import fleet_roster
from tools.graph.schemas.fleet_roster import FLEET_ROSTER_SET_ID, FLEET_ROSTER_REVISION


@pytest.fixture
def personal_store(tmp_path, monkeypatch):
    root = tmp_path / "orgs"
    root.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal").close()
    yield root
    GraphDB.close_all_pooled()


def test_entries_roundtrip_through_personal_db_and_resolve(personal_store):
    root = KeyPair.generate()
    anchor = root.public_hex
    m1, m2 = KeyPair.generate().public_hex, KeyPair.generate().public_hex
    e1 = fleet_roster.enroll(root, machine_pub=m1, seq=0)
    e2 = fleet_roster.enroll(root, machine_pub=m2, seq=0)
    kick = fleet_roster.kick(root, machine_pub=m2, seq=1)
    for e in (e1, e2, kick):
        fleet_roster.store_entry(e, org=None)

    # Read back the full entry set and resolve — m2 was kicked.
    loaded = fleet_roster.load_entries(org=None)
    assert len(loaded) == 3
    roster = fleet_roster.current_roster(anchor, org=None)
    assert set(roster) == {m1}
    assert fleet_roster.active_machines(loaded, anchor_root_pub=anchor) == {m1}


def test_stored_rows_are_raw_never_published_or_canonical(personal_store):
    """Bullet 6, enforced two ways: store_entry writes `raw`, and the schema's
    publication_band(max='raw') REFUSES any attempt to write higher — so a
    bug or a hostile caller cannot promote fleet topology into org-visible
    bands."""
    root = KeyPair.generate()
    entry = fleet_roster.enroll(root, machine_pub=KeyPair.generate().public_hex)
    fleet_roster.store_entry(entry, org=None)

    members = settings_ops.read_owned_set(
        FLEET_ROSTER_SET_ID, org=None, target_revision=FLEET_ROSTER_REVISION,
    )
    assert len(members) == 1
    assert members.members[0].state == "raw"

    # The band ceiling is enforced by the schema, not just convention.
    for promoted in ("published", "canonical"):
        with pytest.raises(Exception):
            settings_ops.add_setting(
                FLEET_ROSTER_SET_ID, FLEET_ROSTER_REVISION,
                "promoted-attempt", fleet_roster._entry_payload(entry),
                org=None, state=promoted,
            )


def test_a_reenrol_stored_beside_its_kick_resolves_alive(personal_store):
    root = KeyPair.generate()
    anchor = root.public_hex
    m = KeyPair.generate().public_hex
    enrol = fleet_roster.enroll(root, machine_pub=m, seq=0)
    kick = fleet_roster.kick(root, machine_pub=m, seq=1)
    back = fleet_roster.reenroll(root, machine_pub=m, supersedes=kick.entry_id, seq=2)
    for e in (enrol, kick, back):
        fleet_roster.store_entry(e, org=None)
    roster = fleet_roster.current_roster(anchor, org=None)
    assert m in roster and roster[m].entry_id == back.entry_id


def _anchor(monkeypatch, root_pub):
    from tools.network import fleet_tunnel_server
    monkeypatch.setattr(fleet_tunnel_server, "_personal_root_pub", lambda: root_pub)


def test_a_forged_roster_row_is_refused_at_write(personal_store, monkeypatch):
    """auto-s4uv8: readers do not verify roster signatures (b0c36e4b8), so a
    session writing the set through the generic Settings API must be refused
    at the write: a row naming the operator's root with a bad signature."""
    from tools.graph.schemas.registry import SchemaValidationError
    root = KeyPair.generate()
    _anchor(monkeypatch, root.public_hex)
    good = fleet_roster.enroll(root, machine_pub=KeyPair.generate().public_hex)
    forged = fleet_roster._entry_payload(good)
    forged["machine_pub"] = KeyPair.generate().public_hex
    with pytest.raises(SchemaValidationError, match="does not verify"):
        settings_ops.upsert_by_key(FLEET_ROSTER_SET_ID, FLEET_ROSTER_REVISION,
                                   "f" * 64, forged, org=None, state="raw")
    assert fleet_roster.load_entries(org=None) == []


def test_an_override_that_swaps_the_machine_is_refused(personal_store, monkeypatch):
    from tools.graph.schemas.registry import SchemaValidationError
    root = KeyPair.generate()
    _anchor(monkeypatch, root.public_hex)
    sid = fleet_roster.store_entry(
        fleet_roster.enroll(root, machine_pub=KeyPair.generate().public_hex), org=None)
    with pytest.raises(SchemaValidationError, match="does not verify"):
        settings_ops.override_setting(
            sid, {"machine_pub": KeyPair.generate().public_hex}, org=None, state="raw")


def test_an_entry_signed_by_another_root_is_refused_once_a_root_is_enrolled(
    personal_store, monkeypatch,
):
    from tools.graph.schemas.registry import SchemaValidationError
    _anchor(monkeypatch, KeyPair.generate().public_hex)
    other = KeyPair.generate()
    with pytest.raises(SchemaValidationError, match="not this fleet's personal root"):
        fleet_roster.store_entry(
            fleet_roster.enroll(other, machine_pub=KeyPair.generate().public_hex), org=None)
