"""Read-model projections: the unassemblable-threshold hygiene surface."""

from __future__ import annotations

from tools.network.idkit import KeyPair
from tools.network.ledger.projections import unassemblable_thresholds

from .conftest import Sim


def _grant_admin(sim, count):
    """Grant the role:grant:member-carrying 'admin' role to *count* bare
    personas; returns their public keys."""
    holders = []
    for _ in range(count):
        persona = KeyPair.generate()
        sim.role_grant(sim.root, persona, "admin")
        holders.append(persona.public_hex)
    return holders


def test_unassemblable_threshold_reports_then_clears():
    sim = Sim()
    sim.role_define(sim.root, "admin", scope_set=["role:grant:member"])
    sim.role_define(sim.root, "member", requires="admin-ack", approver_threshold=3)
    holders = _grant_admin(sim, 2)

    warnings = unassemblable_thresholds(sim.fold())
    assert warnings == (
        {
            "role": "member",
            "approver_threshold": 3,
            "admission_authority_holders": sorted(holders),
        },
    )
    # Root holds * but is the cold constitutional key — never counted.
    assert sim.root.public_hex not in warnings[0]["admission_authority_holders"]

    holders.extend(_grant_admin(sim, 1))  # a third holder assembles it
    assert unassemblable_thresholds(sim.fold()) == ()


def test_non_admin_ack_roles_never_warn():
    sim = Sim()
    sim.role_define(sim.root, "self-role", requires="self")
    sim.role_define(sim.root, "sponsor-role", requires="sponsor")
    assert unassemblable_thresholds(sim.fold()) == ()


def test_default_threshold_of_one_warns_only_with_zero_holders():
    sim = Sim()
    sim.role_define(sim.root, "admin", scope_set=["role:grant:member"])
    sim.role_define(sim.root, "member", requires="admin-ack")  # static 1
    warning = unassemblable_thresholds(sim.fold())
    assert warning[0]["role"] == "member"
    assert warning[0]["admission_authority_holders"] == []
    _grant_admin(sim, 1)
    assert unassemblable_thresholds(sim.fold()) == ()
