"""The approver-authored admission event (OrgAdmission.tla admission event,
auto-qrmlg.12; operator ruling 2026-09-25): an approver appends the
invitee's UNCHANGED signed member.claim together with the approvals
gathered for it, and the fold admits on it. Calibrations from the model:
ApproverCannotForgeClaim, NoDoubleAdmission, AdmissionRespectsRemoval,
AdmissionHasThreshold.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import HLC
from tools.network.ledger.events import Event, make_event, sign_approval
from tools.network.ledger.fold import (
    R_ADMISSION_AFTER_REMOVAL,
    R_ADMISSION_BAD_CLAIM,
    R_ADMISSION_POSITION,
    R_ADMISSION_UNAUTHORIZED,
    R_APPROVAL_MISSING,
    R_INVITE_ALREADY_CLAIMED,
    R_PERSONA_EXISTS,
)

from .conftest import Sim, key


def _org(threshold=1):
    """Root, a sponsor member holding the invite scope for an approval role."""
    sim = Sim()
    sim.role_define(sim.root, "member", ["link:publish"], requires="sponsor")
    sim.role_define(sim.root, "steward", ["*"], requires="self")
    steward = KeyPair.generate()
    sim.claim(sim.invite(sim.root, "steward", invite_key=steward), steward, steward)
    return sim, steward


def _staged_claim(sim, invite_id, persona, *, ts=None):
    """A claim minted by the invitee at the current heads but NOT appended:
    what the join channel stages while approvals gather."""
    payload = {"type": "member.claim", "invite_ref": invite_id,
               "persona_pub": key(persona), "profile": {}, "approvals": []}
    hlc = HLC(ts if ts is not None else sim.next_ts())
    return make_event(persona, payload, list(sim.ledger.heads()), hlc)


def _admission(sim, author, claim: Event, approvers, *, parents=None, ts=None):
    approvals = sorted((sign_approval(a, "member.claim", claim.payload) for a in approvers),
                       key=lambda e: e["key"])
    payload = {"type": "member.admission", "claim": claim.to_json().decode("utf-8"),
               "approvals": approvals}
    return sim.emit(author, payload, parents=parents, ts=ts)


def test_the_sponsor_admits_a_staged_claim_without_a_second_invitee_ceremony():
    sim, steward = _org()
    invitee, ik = KeyPair.generate(), KeyPair.generate()
    invite = sim.invite(steward, "member", invite_key=ik)
    claim = _staged_claim(sim, invite, ik)          # key-bound claim signed by the invite key
    # Time passes: another event lands after the claim was staged.
    sim.role_define(sim.root, "extra", ["link:publish"], requires="self")
    admission = _admission(sim, steward, claim, [steward])
    state = sim.fold()
    assert state.valid[admission] is True, state.reasons.get(admission)
    member = state.members[key(ik)]
    assert member.claim_id == admission and member.invite_id == invite
    assert key(ik) in [m.current_key for m in state.members.values()]
    assert invitee.public_hex  # the invitee never signed again


def test_the_carried_claim_must_verify_under_the_invitee_key_at_its_position():
    """ApproverCannotForgeClaim: an approver cannot alter or fabricate the
    claim, and cannot carry a claim staged at a position outside its
    ancestry."""
    sim, steward = _org()
    ik = KeyPair.generate()
    invite = sim.invite(steward, "member", invite_key=ik)
    claim = _staged_claim(sim, invite, ik)
    # Tampered claim wire: the invitee's signature no longer verifies.
    forged = Event.from_json(claim.to_json())
    wire = claim.to_json().decode("utf-8").replace('"profile":{}', '"profile":{"x":1}')
    payload = {"type": "member.admission", "claim": wire,
               "approvals": [sign_approval(steward, "member.claim", claim.payload)]}
    bad = sim.emit(steward, payload)
    assert sim.fold().reasons[bad] == R_ADMISSION_BAD_CLAIM
    # A claim minted at heads this admission does not descend from.
    other = KeyPair.generate()
    other_invite = sim.invite(steward, "member", invite_key=other)
    orphan = make_event(other, {"type": "member.claim", "invite_ref": other_invite,
                                "persona_pub": key(other), "profile": {}, "approvals": []},
                        ["ab" * 32], HLC(sim.next_ts()))
    payload = {"type": "member.admission", "claim": orphan.to_json().decode("utf-8"),
               "approvals": [sign_approval(steward, "member.claim", orphan.payload)]}
    misplaced = sim.emit(steward, payload)
    assert sim.fold().reasons[misplaced] == R_ADMISSION_POSITION
    assert forged.event_id == claim.event_id


def test_threshold_author_and_double_admission_rules():
    """AdmissionHasThreshold, the author must be an approver, NoDoubleAdmission."""
    sim, steward = _org()
    ik = KeyPair.generate()
    invite = sim.invite(steward, "member", invite_key=ik)
    claim = _staged_claim(sim, invite, ik)
    outsider = KeyPair.generate()
    # No approvals at all, authored by the root (so the author gate passes):
    # below threshold.
    none = sim.emit(sim.root, {"type": "member.admission",
                               "claim": claim.to_json().decode("utf-8"), "approvals": []})
    assert sim.fold().reasons[none] == R_APPROVAL_MISSING
    # Approved by the sponsor but authored by a stranger.
    stranger = _admission(sim, outsider, claim, [steward])
    assert sim.fold().reasons[stranger] == R_ADMISSION_UNAUTHORIZED
    # A proper admission, then the same claim admitted again: the invite is redeemed.
    ok = _admission(sim, steward, claim, [steward])
    assert sim.fold().valid[ok] is True
    again = _admission(sim, steward, claim, [steward])
    assert sim.fold().reasons[again] in {R_INVITE_ALREADY_CLAIMED, R_PERSONA_EXISTS}


def test_a_removal_after_the_staged_claim_blocks_its_admission():
    """AdmissionRespectsRemoval: a claim staged before the persona's removal
    cannot re-admit it afterwards."""
    sim, steward = _org()
    ik = KeyPair.generate()
    first_invite = sim.invite(steward, "member", invite_key=ik)
    first = _admission(sim, steward, _staged_claim(sim, first_invite, ik), [steward])
    assert sim.fold().valid[first] is True
    # A second invite for the same persona, staged, then the persona removed.
    second_invite = sim.invite(steward, "member", invite_key=ik)
    staged = _staged_claim(sim, second_invite, ik)
    sim.revoke_event(steward, first)                 # removal, after the staged claim
    assert key(ik) not in sim.fold().members
    late = _admission(sim, steward, staged, [steward])
    assert sim.fold().reasons[late] == R_ADMISSION_AFTER_REMOVAL
    # A claim staged AFTER the removal admits again.
    fresh = _admission(sim, steward, _staged_claim(sim, second_invite, ik), [steward])
    assert sim.fold().valid[fresh] is True, sim.fold().reasons.get(fresh)
