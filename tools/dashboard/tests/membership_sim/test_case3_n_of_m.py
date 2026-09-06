"""Case 3 (auto-2gewd): N-of-M admission across multiple dashboards.

A ``member`` role with ``claim_requires: admin-ack`` and a static
``approver_threshold`` of two admits a joiner only when two DISTINCT
eligible approvers countersign. Eligibility is derived from the fold —
``admitting_approvers`` = root ∪ sponsor ∪ holders of ``role:grant:member``
— never configured. The approvers are independent identities that each
authenticate to the registry in their own right; the joiner they admit
becomes one too once the checkpoint advances.
"""

from __future__ import annotations

import time

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger.fold import R_APPROVAL_MISSING
from tools.network.ledger.projections import unassemblable_thresholds

from ._harness import (
    Org, Registry, RoleSpec, assert_member, assert_not_member, assert_verdict,
    identity_admitted,
)


@pytest.fixture
def registry(tmp_path):
    reg = Registry(tmp_path)
    try:
        yield reg
    finally:
        reg.stop()


def _claim_with(org, persona, approvers):
    ik = KeyPair.generate()
    inv = org.invite("member", invite_key=ik)
    return org.claim(inv, ik, persona, approvers=approvers)


def test_two_of_two_admits_only_on_the_second_countersignature(registry):
    org = Org.found(roles={
        "admin": RoleSpec(scope_set=("role:grant:member",)),
        "member": RoleSpec(scope_set=("link:publish",), requires="admin-ack", threshold=2),
    })

    # The founder holds "*", which covers role:grant:member, so it is already
    # one admission-authority holder — but threshold 2 > 1 is still a warning
    # (root itself is never counted as a routine approver).
    warnings = unassemblable_thresholds(org.fold())
    assert [w["role"] for w in warnings] == ["member"]
    assert warnings[0]["admission_authority_holders"] == [org.founder.public_hex]

    a1 = org.admit("a1", "admin")
    a2 = org.admit("a2", "admin")
    assert unassemblable_thresholds(org.fold()) == ()

    # Each approver is an independent authenticated identity on the wire.
    registry.register(org.sim.root)
    registry.commit_checkpoint(org.sim, seq=0)
    assert identity_admitted(registry, org, a1, seq=0) is True
    assert identity_admitted(registry, org, a2, seq=0) is True

    # One countersignature: have 1 < need 2 — pending, not a member.
    joiner = KeyPair.generate()
    one = _claim_with(org, joiner, approvers=[a1])
    state = org.fold()
    assert_verdict(state, one, admitted=False, reason=R_APPROVAL_MISSING)
    assert_not_member(state, joiner)

    # Two distinct eligible countersignatures admit.
    two = _claim_with(org, joiner, approvers=[a1, a2])
    state = org.fold()
    assert_verdict(state, two, admitted=True)
    assert_member(state, joiner, roles=["member"])

    # A well-formed but INELIGIBLE signature is inert: the new member holds
    # no role:grant:member, so (member, a1) is still have 1.
    other = KeyPair.generate()
    ineligible = _claim_with(org, other, approvers=[joiner, a1])
    assert_verdict(org.fold(), ineligible, admitted=False, reason=R_APPROVAL_MISSING)

    # The admitted joiner is a serving identity once the checkpoint advances.
    registry.commit_checkpoint(org.sim, seq=1, ts=int(time.time()) + 1)
    assert identity_admitted(registry, org, joiner, seq=1) is True
