"""Smoke for the shared harness (auto-whf4h acceptance): the real stack comes
up, a founded two-member org commits to the registry, one ledger event is
admitted and one refused through ``drive_actions``/``assert_verdict``, and
the registry admits a member's identity while refusing an outsider's.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger.fold import R_INVITE_OVERREACH

from ._harness import (
    Org, Registry, RoleSpec, Step, assert_holds, assert_member,
    assert_not_member, assert_verdict, drive_actions, identity_admitted,
)


@pytest.fixture
def registry(tmp_path):
    reg = Registry(tmp_path)
    try:
        yield reg
    finally:
        reg.stop()


def test_harness_end_to_end(registry):
    org = Org.found(roles={"member": RoleSpec(scope_set=("link:publish",))})
    alice = org.admit("alice", "member")
    bob = org.admit("bob", "member")

    # The fold: both admitted, and the founder (holding "*") is a checkpointer
    # while a plain member is not — the two roots the registry commits.
    state = org.fold()
    assert_member(state, org.founder, roles=["owner"])
    assert_member(state, alice, roles=["member"])
    assert_holds(state, org.founder, "membership:checkpoint")
    assert_holds(state, alice, "membership:checkpoint", expected=False)

    # Registry: nothing adopted before the seed; seq 0 after it.
    registry.register(org.sim.root)
    assert registry.membership_state() is None
    registry.commit_checkpoint(org.sim, seq=0)
    assert registry.membership_state()["seq"] == 0

    # drive_actions / assert_verdict: alice (link:publish only) minting an
    # invite for "member" overreaches — she lacks invite:member — and the
    # root's identical invite is admitted.
    ik_bad, ik_ok = KeyPair.generate(), KeyPair.generate()
    ids = drive_actions(org, [
        Step("alice_invites", lambda o: o.invite("member", sponsor=alice, invite_key=ik_bad)),
        Step("root_invites", lambda o: o.invite("member", invite_key=ik_ok)),
    ])
    state = org.fold()
    assert_verdict(state, ids["alice_invites"], admitted=False, reason=R_INVITE_OVERREACH)
    assert_verdict(state, ids["root_invites"], admitted=True)

    # The wire: a committed member authenticates to the registry by proof;
    # an outsider presenting a fabricated roster does not.
    outsider = KeyPair.generate()
    assert_not_member(state, outsider)
    assert identity_admitted(registry, org, bob, seq=0) is True
    assert identity_admitted(registry, org, outsider, seq=0,
                             member_pubs_override=[outsider.public_hex]) is False


def test_drive_actions_rejects_duplicate_step_names():
    org = Org.found()
    with pytest.raises(ValueError, match="duplicate"):
        drive_actions(org, [
            Step("x", lambda o: o.invite("owner", invite_key=KeyPair.generate())),
            Step("x", lambda o: o.invite("owner", invite_key=KeyPair.generate())),
        ])
