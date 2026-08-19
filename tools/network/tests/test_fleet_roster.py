"""The fleet roster CRDT (auto-0vpse).

Design ``graph://0c655045-ee4``. Each test maps to one acceptance bullet:
concurrent enrolment of different machines unions, one machine renewed
concurrently resolves to a deterministic winner, a kick is absorbing against a
higher-seq renewal, a replayed pre-kick enrol does not resurrect while a
tombstone-citing re-enrol does, no roster-level version exists, and the rows
carry only public material verified against the anchor.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network import fleet_roster
from tools.network.fleet_roster import EntryKind, FleetRosterError


def _fleet():
    return KeyPair.generate()  # the personal root


def _machine():
    return KeyPair.generate().public_hex


def test_concurrent_enrolment_of_different_machines_unions():
    root = _fleet()
    anchor = root.public_hex
    m1, m2 = _machine(), _machine()
    # M1 adds M3 on one primary; M2 adds M4 on another — concurrently.
    e1 = fleet_roster.enroll(root, machine_pub=m1, seq=0)
    e2 = fleet_roster.enroll(root, machine_pub=m2, seq=0)
    roster = fleet_roster.resolve([e1, e2], anchor_root_pub=anchor)
    assert set(roster) == {m1, m2}, "both concurrent enrolments must survive"


def test_one_machine_renewed_concurrently_resolves_deterministically():
    """Two concurrent renewals of one machine (same seq) resolve to ONE entry,
    and the SAME one regardless of delivery order — the tie-break is asserted,
    not merely that a winner exists."""
    root = _fleet()
    anchor = root.public_hex
    m = _machine()
    a = fleet_roster.enroll(root, machine_pub=m, seq=1, issued_at=100)
    b = fleet_roster.enroll(root, machine_pub=m, seq=1, issued_at=200)
    assert a.entry_id != b.entry_id
    expected = a if a.entry_id < b.entry_id else b  # ascending id tie-break

    fwd = fleet_roster.resolve([a, b], anchor_root_pub=anchor)
    rev = fleet_roster.resolve([b, a], anchor_root_pub=anchor)
    assert fwd[m].entry_id == rev[m].entry_id == expected.entry_id


def test_a_higher_seq_renewal_wins_when_there_is_no_kick():
    root = _fleet()
    anchor = root.public_hex
    m = _machine()
    old = fleet_roster.enroll(root, machine_pub=m, seq=1)
    new = fleet_roster.enroll(root, machine_pub=m, seq=2)
    roster = fleet_roster.resolve([old, new], anchor_root_pub=anchor)
    assert roster[m].seq == 2


def test_a_kick_beats_a_renewal_carrying_a_higher_sequence():
    """The absorbing rule, and the test that FAILS on plain last-writer-wins:
    a kick at seq 1 beats an ordinary renewal at seq 5. A renewal must never
    silently un-revoke."""
    root = _fleet()
    anchor = root.public_hex
    m = _machine()
    kick = fleet_roster.kick(root, machine_pub=m, seq=1)
    higher_renewal = fleet_roster.enroll(root, machine_pub=m, seq=5)
    roster = fleet_roster.resolve([higher_renewal, kick], anchor_root_pub=anchor)
    assert m not in roster, (
        "a kick must absorb a higher-seq renewal; plain LWW would keep seq 5"
    )


def test_a_replayed_pre_kick_enrol_does_not_resurrect_a_kicked_machine():
    root = _fleet()
    anchor = root.public_hex
    m = _machine()
    enrol = fleet_roster.enroll(root, machine_pub=m, seq=0)
    kick = fleet_roster.kick(root, machine_pub=m, seq=1)
    # The old enrol event replayed later — same bytes, cites no tombstone.
    roster = fleet_roster.resolve([enrol, kick, enrol], anchor_root_pub=anchor)
    assert m not in roster, "a replayed pre-kick enrol must not resurrect"


def test_a_tombstone_citing_reenrolment_resurrects_the_machine():
    root = _fleet()
    anchor = root.public_hex
    m = _machine()
    enrol = fleet_roster.enroll(root, machine_pub=m, seq=0)
    kick = fleet_roster.kick(root, machine_pub=m, seq=1)
    back = fleet_roster.reenroll(
        root, machine_pub=m, supersedes=kick.entry_id, seq=2,
    )
    roster = fleet_roster.resolve([enrol, kick, back], anchor_root_pub=anchor)
    assert m in roster and roster[m].entry_id == back.entry_id


def test_a_reenrolment_citing_an_old_kick_does_not_escape_a_newer_kick():
    """The newest revocation stands: re-enrolling against kick1 does not
    survive a later, uncited kick2."""
    root = _fleet()
    anchor = root.public_hex
    m = _machine()
    kick1 = fleet_roster.kick(root, machine_pub=m, seq=1)
    back = fleet_roster.reenroll(
        root, machine_pub=m, supersedes=kick1.entry_id, seq=2,
    )
    kick2 = fleet_roster.kick(root, machine_pub=m, seq=3)
    roster = fleet_roster.resolve([kick1, back, kick2], anchor_root_pub=anchor)
    assert m not in roster


def test_a_foreign_or_tampered_entry_never_affects_the_roster():
    root = _fleet()
    anchor = root.public_hex
    m = _machine()
    good = fleet_roster.enroll(root, machine_pub=m, seq=0)

    # An entry signed by a DIFFERENT personal root (another operator's fleet).
    foreign_root = _fleet()
    foreign = fleet_roster.enroll(foreign_root, machine_pub=_machine(), seq=0)
    # A tampered good entry: machine swapped after signing.
    from dataclasses import replace
    tampered = replace(good, machine_pub=_machine())

    roster = fleet_roster.resolve([good, foreign, tampered], anchor_root_pub=anchor)
    assert set(roster) == {m}, "only the operator's own verified entry survives"


def test_verify_rejects_a_foreign_anchor_by_name_and_a_bad_signature():
    root = _fleet()
    m = _machine()
    entry = fleet_roster.enroll(root, machine_pub=m, seq=0)
    fleet_roster.verify(entry, anchor_root_pub=root.public_hex)  # ok
    with pytest.raises(FleetRosterError, match="not this fleet's personal root"):
        fleet_roster.verify(entry, anchor_root_pub=_fleet().public_hex)
    from dataclasses import replace
    forged = replace(entry, signature="00" * 64)
    with pytest.raises(FleetRosterError, match="does not verify"):
        fleet_roster.verify(forged, anchor_root_pub=root.public_hex)


def test_a_kick_may_not_cite_a_supersedes():
    """A kick is a tombstone, not an escape — only an enrol cites one. Guard
    the shape so a malformed kick-with-citation is refused."""
    root = _fleet()
    m = _machine()
    # Construct a kick carrying a supersedes by hand (the API never does).
    from tools.network.fleet_roster import _mint
    bad = _mint(root, machine_pub=m, kind=EntryKind.KICK, seq=1, issued_at=0,
                supersedes="ab" * 32)
    with pytest.raises(FleetRosterError, match="kick tombstone does not cite"):
        fleet_roster.verify(bad, anchor_root_pub=root.public_hex)


def test_no_roster_level_version_field_exists_in_the_entry():
    """The stored shape is per-machine entries with a per-machine seq and no
    roster-wide counter — asserted against the signed body."""
    root = _fleet()
    entry = fleet_roster.enroll(root, machine_pub=_machine(), seq=3)
    body = fleet_roster._body(entry)
    assert "seq" in body and body["seq"] == 3
    for forbidden in ("roster_version", "roster_seq", "version_vector", "counter"):
        assert forbidden not in body
    # The only 'v' is the frozen format version, not a mutable roster counter.
    assert body["v"] == fleet_roster.FLEET_ROSTER_VERSION
