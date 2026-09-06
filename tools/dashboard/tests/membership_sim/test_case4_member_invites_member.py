"""Case 4 (auto-aqrn4): members inviting members.

A non-root sponsor holding ``invite:member`` mints an invite; a new persona
claims it; under ``claim_requires: sponsor`` the sponsor's own
countersignature is the one vouch that admits (D16). The new member's
scopes come from the role DEFINITION, not from the inviter — an invite is
not a delegation hop. The negative twin: a member whose role confers no
``invite:*`` scope cannot mint the invite at all.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger.fold import R_APPROVAL_MISSING, R_INVITE_OVERREACH

from ._harness import (
    Org, Registry, RoleSpec, assert_holds, assert_member, assert_not_member,
    assert_verdict, identity_admitted,
)


@pytest.fixture
def registry(tmp_path):
    reg = Registry(tmp_path)
    try:
        yield reg
    finally:
        reg.stop()


def test_member_sponsors_a_member(registry):
    org = Org.found(roles={
        "member": RoleSpec(scope_set=("invite:member",), requires="sponsor"),
        "guest": RoleSpec(scope_set=(), requires="self"),
    })

    # Root sponsors alice; the sponsor's countersignature admits her.
    alice = org.admit("alice", "member", approvers=[org.sim.root])
    state = org.fold()
    assert_member(state, alice, roles=["member"])
    assert_holds(state, alice, "invite:member")

    # alice sponsors bob: her single vouch is have 1 / need 1.
    bob, ik = KeyPair.generate(), KeyPair.generate()
    inv = org.invite("member", sponsor=alice, invite_key=ik)
    claim = org.claim(inv, ik, bob, approvers=[alice])
    state = org.fold()
    assert_verdict(state, inv, admitted=True)
    assert_verdict(state, claim, admitted=True)
    assert_member(state, bob, roles=["member"], )
    assert state.members[bob.public_hex].sponsor == alice.public_hex

    # bob's authority is the role's, not alice's: he holds what "member"
    # confers and nothing alice might separately hold.
    assert_holds(state, bob, "invite:member")
    assert_holds(state, bob, "link:publish", expected=False)

    # Without the sponsor's vouch the claim stays pending.
    carol, ik2 = KeyPair.generate(), KeyPair.generate()
    inv2 = org.invite("member", sponsor=alice, invite_key=ik2)
    unvouched = org.claim(inv2, ik2, carol, approvers=[])
    state = org.fold()
    assert_verdict(state, unvouched, admitted=False, reason=R_APPROVAL_MISSING)
    assert_not_member(state, carol)

    # NEGATIVE: a guest (empty scope_set) cannot mint an invite for member.
    guest = org.admit("guest", "guest")
    overreach = org.invite("member", sponsor=guest, invite_key=KeyPair.generate())
    assert_verdict(org.fold(), overreach, admitted=False, reason=R_INVITE_OVERREACH)

    # Wire: bob, admitted on a member's sponsorship alone, authenticates to
    # the registry by committed membership like anyone else.
    registry.register(org.sim.root)
    registry.commit_checkpoint(org.sim, seq=0)
    assert identity_admitted(registry, org, bob, seq=0) is True
    assert identity_admitted(registry, org, carol, seq=0) is False
