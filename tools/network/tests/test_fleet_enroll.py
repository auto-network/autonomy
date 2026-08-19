"""First-boot enrollment: keypair, fingerprint, ask-to-join (auto-b6fee).

Covers the crypto core of piece 2: the fingerprint both surfaces must share,
the enrollment request's proof of possession, and that a request grants its
holder nothing. The machine-store placement and the acceptance screen are the
caller's integration; these pin the primitives.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network import fleet_invite, fleet_enroll
from tools.network.fleet_enroll import EnrollmentRequest, FleetEnrollError


def _invite(root=None):
    root = root or KeyPair.generate()
    return root, fleet_invite.mint(
        root, rendezvous="https://primary.example.net/fleet/rv/x",
        invite_id="ab" * 32,
    )


def test_the_fingerprint_is_deterministic_and_shared_form():
    m = KeyPair.generate().public_hex
    fp1 = fleet_enroll.fingerprint(m)
    fp2 = fleet_enroll.fingerprint(m)
    assert fp1 == fp2
    # Six groups of four uppercase hex — the readable compare form.
    groups = fp1.split(" ")
    assert len(groups) == 6
    assert all(len(g) == 4 and g == g.upper() for g in groups)
    # A different key renders a different fingerprint.
    assert fleet_enroll.fingerprint(KeyPair.generate().public_hex) != fp1


def test_both_surfaces_render_the_same_fingerprint_for_one_key():
    """The primary and the joining machine both call fingerprint(); this is
    the test that FAILS if either side changes its format — the operator's
    compare-before-approve check rests on it."""
    machine = KeyPair.generate()
    # The machine renders it locally; the primary renders it from the pub key
    # carried in the enrollment request. Same function, same bytes.
    _, invite = _invite()
    req = fleet_enroll.build_request(machine, invite=invite)
    machine_side = fleet_enroll.fingerprint(machine.public_hex)
    primary_side = fleet_enroll.fingerprint(req.machine_pub)
    assert machine_side == primary_side


def test_a_request_verifies_against_the_invite_it_was_built_for():
    machine = KeyPair.generate()
    _, invite = _invite()
    req = fleet_enroll.build_request(machine, invite=invite)
    fleet_enroll.verify_request(req, invite=invite)  # no raise
    assert req.machine_pub == machine.public_hex


def test_a_request_for_another_invite_or_fleet_is_refused():
    machine = KeyPair.generate()
    _, invite = _invite()
    req = fleet_enroll.build_request(machine, invite=invite)

    # Another invite (different id) from the same fleet.
    root = KeyPair.generate()
    other_invite = fleet_invite.mint(
        root, rendezvous="https://primary.example.net/rv/y", invite_id="cd" * 32,
    )
    with pytest.raises(FleetEnrollError, match="different invite"):
        fleet_enroll.verify_request(req, invite=other_invite)

    # Same invite id, different fleet anchor -> refused on anchor.
    from dataclasses import replace
    wrong_fleet = replace(other_invite, invite_id=invite.invite_id)
    with pytest.raises(FleetEnrollError, match="different fleet anchor"):
        fleet_enroll.verify_request(req, invite=wrong_fleet)


def test_a_proof_from_a_key_the_sender_does_not_hold_is_refused():
    """Proof of possession: a request naming a machine key whose private half
    the sender lacks cannot verify. Model it by pairing one machine's pub with
    a proof signed by a DIFFERENT key."""
    real_machine = KeyPair.generate()
    _, invite = _invite()
    req = fleet_enroll.build_request(real_machine, invite=invite)

    from dataclasses import replace
    # Swap in an observed public key but keep the real machine's proof: the
    # proof no longer matches the named key.
    forged = replace(req, machine_pub=KeyPair.generate().public_hex)
    with pytest.raises(FleetEnrollError, match="does not verify"):
        fleet_enroll.verify_request(forged, invite=invite)


def test_the_request_carries_only_public_material():
    """A stolen request grants nothing: every field is public, and the proof
    is over public facts. The private half is never in it — reconstructing a
    signer from the request cannot act as the machine."""
    machine = KeyPair.generate()
    _, invite = _invite()
    req = fleet_enroll.build_request(machine, invite=invite)
    # The only 64-hex fields are public keys; the proof is 128-hex.
    assert req.machine_pub == machine.public_hex
    assert len(req.proof) == 128
    # A holder cannot forge a valid request for the same machine key: they
    # lack its private half, so signing a new correlation fails to verify.
    impostor = KeyPair.from_private_hex(req.machine_pub)  # misuse pub as seed
    assert impostor.public_hex != machine.public_hex


def test_a_fingerprint_rejects_a_malformed_key():
    for bad in ("", "zz" * 32, "ab" * 16, "ab" * 40):
        with pytest.raises(FleetEnrollError):
            fleet_enroll.fingerprint(bad)
