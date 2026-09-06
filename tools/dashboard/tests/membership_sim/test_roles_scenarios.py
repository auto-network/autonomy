"""Role semantics as harness scenarios (auto-f9wwa; checkpoint
test:crit-membership-sim). Every semantic the roles design of record
(graph://d1b3db8f-879) decides is asserted here on the fold, in the form the
design's test plan names: (a) member admission under admin-ack, (b) narrowing
as a loss head with widening and covering as non-contractions, plus the
operator's root-widening ruling, (c) the attenuation rules c1..c5, (d) a
member inviting a member, (e) N-of-M admission with eligible approvers
derived from scopes.

Layer: the ``Sim`` ledger through ``_harness`` only. The over-the-wire
``role.define`` path is case C2 of the simulation epic; the browser ceremony
has its own vector test. This module defines no fixtures of its own.
"""

from __future__ import annotations

import hashlib

from tools.network.idkit import KeyPair
from tools.network.ledger import membership_commitment as mc
from tools.network.ledger.fold import (
    R_APPROVAL_MISSING,
    R_INVITE_OVERREACH,
    R_ROLE_DEFINE_OVERREACH,
    R_ROLE_DEFINE_UNAUTHORIZED,
    R_ROLE_GRANT_UNAUTHORIZED,
)
from tools.network.ledger.projections import unassemblable_thresholds

from ._harness import (
    Org, RoleSpec, Step, assert_holds, assert_member, assert_not_member,
    assert_verdict, drive_actions,
)

GOVERNANCE_SCOPES = (
    "invite:member", "role:grant:member", "role:define",
    "link:publish", "membership:checkpoint",
)


def _bearer():
    token = "cd" * 32
    return token, hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── (a) a Member joins under admin-ack ────────────────────────


def test_a_member_joins_after_one_approval_and_holds_no_governance():
    """QA-R1. Member carries nothing; a bearer claim never self-completes
    (B6), so one eligible approval is the whole admission."""
    org = Org.found(roles={
        "member": RoleSpec(scope_set=(), requires="admin-ack", threshold=1),
    })
    token, token_hash = _bearer()
    dean = KeyPair.generate()
    # Later steps cite the invite id, so the rounds are driven separately.
    first = drive_actions(org, [
        Step("invite", lambda o: o.invite("member", sponsor=o.founder, token_hash=token_hash)),
    ])
    second = drive_actions(org, [
        Step("claim_unapproved", lambda o: o.claim(first["invite"], dean, dean, token=token)),
    ])
    state = org.fold()
    assert_verdict(state, second["claim_unapproved"], admitted=False, reason=R_APPROVAL_MISSING)
    assert_not_member(state, dean)

    third = drive_actions(org, [
        Step("claim_approved", lambda o: o.claim(
            first["invite"], dean, dean, token=token, approvers=[o.founder])),
    ])
    state = org.fold()
    assert_verdict(state, third["claim_approved"], admitted=True)
    assert_member(state, dean, roles=["member"])
    for scope in GOVERNANCE_SCOPES:
        assert_holds(state, dean, scope, expected=False)
    # In the storage domain: the persona's current key is a domain member.
    assert dean.public_hex in mc.member_pubs(state)


# ── (b) narrowing is a loss head; widening and covering are not ───


def test_b_narrowing_a_role_is_a_loss_head_and_strips_holders():
    org = Org.found(roles={
        "member": RoleSpec(scope_set=("invite:member",), requires="self"),
    })
    alice = org.admit("alice", "member")
    state = org.fold()
    assert_holds(state, alice, "invite:member")

    ids = drive_actions(org, [
        Step("narrow_v2", lambda o: o.define_role(
            "member", RoleSpec(scope_set=(), requires="self"), version=2)),
    ])
    state = org.fold()
    assert_verdict(state, ids["narrow_v2"], admitted=True)
    assert ids["narrow_v2"] in state.loss_heads, "a narrowing must be a contraction"
    # Roles bind by name: every holder loses the scope on the next fold, with
    # no revocation event anywhere.
    assert_holds(state, alice, "invite:member", expected=False)


def test_b_widening_and_covering_redefinitions_are_not_contractions():
    org = Org.found(roles={
        "member": RoleSpec(scope_set=(), requires="self"),
    })
    ids = drive_actions(org, [
        Step("widen_v2", lambda o: o.define_role(
            "member", RoleSpec(scope_set=("invite:member",), requires="self"), version=2)),
        Step("cover_v3", lambda o: o.define_role(
            "member", RoleSpec(scope_set=("invite:*",), requires="self"), version=3)),
    ])
    state = org.fold()
    assert_verdict(state, ids["widen_v2"], admitted=True)
    assert_verdict(state, ids["cover_v3"], admitted=True)
    assert ids["widen_v2"] not in state.loss_heads
    assert ids["cover_v3"] not in state.loss_heads, \
        "a redefinition to a covering pattern removes nothing"
    assert state.role_defs["member"].version == 3


def test_b_root_widening_reaches_every_granter_by_design():
    """Operator ruling 2026-09-06: a granter is a superset of the grantee in
    reach. Root widening Member widens what every role:grant:member holder
    can obtain — including by granting Member to themselves — with no chain
    re-check and no self-grant refusal. Asserted as INTENDED behavior so a
    future change is deliberate (J4 on graph://d1b3db8f-879)."""
    org = Org.found(roles={
        "member": RoleSpec(scope_set=(), requires="self"),
        "admin": RoleSpec(scope_set=("role:grant:member",), requires="self"),
    })
    alice = org.admit("alice", "member")
    bob = org.admit("bob", "admin")
    state = org.fold()
    assert_holds(state, bob, "link:publish", expected=False)

    ids = drive_actions(org, [
        Step("root_widens", lambda o: o.define_role(
            "member", RoleSpec(scope_set=("link:publish",), requires="self"), version=2)),
        Step("bob_self_grant", lambda o: o.grant(bob, "member", author=bob)),
    ])
    state = org.fold()
    assert_verdict(state, ids["root_widens"], admitted=True)
    assert_verdict(state, ids["bob_self_grant"], admitted=True)
    # Alice held Member by name: she gained the scope with no new event.
    assert_holds(state, alice, "link:publish")
    # Bob dispensed Member to himself and now holds what Member carries.
    assert_member(state, bob, roles=["admin", "member"])
    assert_holds(state, bob, "link:publish")


# ── (c) attenuation ───────────────────────────────────────────


def _org_with_admin():
    """Owner (founder), Admin = define + grant/invite Member, Member = []."""
    return Org.found(roles={
        "member": RoleSpec(scope_set=(), requires="self"),
        "admin": RoleSpec(
            scope_set=("invite:member", "role:define", "role:grant:member"),
            requires="self"),
    })


def test_c1_role_held_define_cannot_carry_a_scope_but_may_be_empty():
    """Role-held scopes never enter the delegable set, so an Admin holding
    role:define through the role overreaches on any scoped definition —
    while attenuates() over an EMPTY set is vacuously true, so an empty
    definition is valid (the correction recorded on the design note)."""
    org = _org_with_admin()
    bob = org.admit("bob", "admin")
    ids = drive_actions(org, [
        Step("scoped", lambda o: o.define_role(
            "helper", RoleSpec(scope_set=("invite:member",), requires="self"), author=bob)),
        Step("empty", lambda o: o.define_role(
            "observer", RoleSpec(scope_set=(), requires="self"), author=bob)),
    ])
    state = org.fold()
    assert_verdict(state, ids["scoped"], admitted=False, reason=R_ROLE_DEFINE_OVERREACH)
    assert_verdict(state, ids["empty"], admitted=True)
    assert "helper" not in state.role_defs and "observer" in state.role_defs


def test_c2_a_root_delegation_lets_an_admin_define_within_its_closure():
    org = _org_with_admin()
    bob = org.admit("bob", "admin")
    ids = drive_actions(org, [
        Step("delegate", lambda o: o.delegate(
            bob, ("invite:*", "role:define", "role:grant:*"), redelegate=True)),
        Step("within", lambda o: o.define_role(
            "helper", RoleSpec(scope_set=("invite:member",), requires="self"), author=bob)),
        Step("beyond", lambda o: o.define_role(
            "publisher", RoleSpec(scope_set=("link:publish",), requires="self"), author=bob)),
    ])
    state = org.fold()
    assert_verdict(state, ids["delegate"], admitted=True)
    assert_verdict(state, ids["within"], admitted=True)
    assert_verdict(state, ids["beyond"], admitted=False, reason=R_ROLE_DEFINE_OVERREACH)


def test_c3_no_role_define_at_all_is_unauthorized():
    org = _org_with_admin()
    carol = org.admit("carol", "member")
    ids = drive_actions(org, [
        Step("define", lambda o: o.define_role(
            "helper", RoleSpec(scope_set=(), requires="self"), author=carol)),
    ])
    assert_verdict(org.fold(), ids["define"], admitted=False,
                   reason=R_ROLE_DEFINE_UNAUTHORIZED)


def test_c4_grant_needs_role_grant_scope():
    org = _org_with_admin()
    bob = org.admit("bob", "admin")
    carol = org.admit("carol", "member")
    dave = KeyPair.generate()
    ids = drive_actions(org, [
        Step("admin_grants", lambda o: o.grant(dave, "member", author=bob)),
        Step("member_grants", lambda o: o.grant(KeyPair.generate(), "member", author=carol)),
    ])
    state = org.fold()
    assert_verdict(state, ids["admin_grants"], admitted=True)
    assert state.roles(dave.public_hex) == ("member",)
    assert_verdict(state, ids["member_grants"], admitted=False,
                   reason=R_ROLE_GRANT_UNAUTHORIZED)


def test_c5_inviting_for_a_role_needs_that_invite_scope():
    org = Org.found(roles={
        "member": RoleSpec(scope_set=("invite:member",), requires="self"),
        "admin": RoleSpec(scope_set=("invite:*",), requires="self"),
    })
    carol = org.admit("carol", "member")
    ids = drive_actions(org, [
        Step("for_member", lambda o: o.invite("member", sponsor=carol, invite_key=KeyPair.generate())),
        Step("for_admin", lambda o: o.invite("admin", sponsor=carol, invite_key=KeyPair.generate())),
    ])
    state = org.fold()
    assert_verdict(state, ids["for_member"], admitted=True)
    assert_verdict(state, ids["for_admin"], admitted=False, reason=R_INVITE_OVERREACH)


# ── (d) a member invites a member ─────────────────────────────


def test_d_member_invites_member_and_the_sponsor_vouch_admits():
    org = Org.found(roles={
        "member": RoleSpec(scope_set=("invite:member",), requires="sponsor"),
    })
    # A sponsor-vouched role: even the root's invite admits only with the
    # sponsor's (here the root's) countersignature.
    alice = org.admit("alice", "member", approvers=[org.sim.root])
    assert_member(org.fold(), alice, roles=["member"])
    bob, ik = KeyPair.generate(), KeyPair.generate()
    first = drive_actions(org, [
        Step("alice_invites", lambda o: o.invite("member", sponsor=alice, invite_key=ik)),
    ])
    second = drive_actions(org, [
        Step("unvouched", lambda o: o.claim(first["alice_invites"], ik, bob)),
    ])
    state = org.fold()
    assert_verdict(state, first["alice_invites"], admitted=True)
    assert_verdict(state, second["unvouched"], admitted=False, reason=R_APPROVAL_MISSING)

    third = drive_actions(org, [
        Step("vouched", lambda o: o.claim(first["alice_invites"], ik, bob, approvers=[alice])),
    ])
    state = org.fold()
    assert_verdict(state, third["vouched"], admitted=True)
    assert_member(state, bob, roles=["member"])
    # The role's scopes came from its definition, not from the inviter.
    assert_holds(state, bob, "invite:member")


# ── (e) N-of-M admission ──────────────────────────────────────


def test_e_n_of_m_counts_distinct_eligible_approvers_derived_from_scopes():
    """eligible(M) is a predicate over the fold — root, the sponsor, or a
    holder of role:grant:<role> — never a configured list; need is the
    role's static threshold. Untouched by the roles design (QA-R8)."""
    org = Org.found(roles={
        "member": RoleSpec(scope_set=(), requires="self"),
        "admin": RoleSpec(scope_set=("role:grant:reviewer",), requires="self"),
        "reviewer": RoleSpec(scope_set=("link:publish",), requires="admin-ack", threshold=2),
    })
    # Before any admin exists the threshold cannot be assembled: a warning,
    # not a refusal (root is not counted as a routine approver).
    warned = {w["role"]: w for w in unassemblable_thresholds(org.fold())}
    assert "reviewer" in warned and warned["reviewer"]["approver_threshold"] == 2

    alice = org.admit("alice", "member")
    bob = org.admit("bob", "admin")
    carol = org.admit("carol", "admin")
    assert "reviewer" not in {w["role"] for w in unassemblable_thresholds(org.fold())}

    dave, ik = KeyPair.generate(), KeyPair.generate()
    first = drive_actions(org, [
        Step("invite", lambda o: o.invite("reviewer", invite_key=ik)),
    ])
    inv = first["invite"]
    second = drive_actions(org, [
        Step("one_admin", lambda o: o.claim(inv, ik, dave, approvers=[bob])),
        Step("admin_plus_ineligible", lambda o: o.claim(inv, ik, dave, approvers=[bob, alice])),
    ])
    state = org.fold()
    for name in ("one_admin", "admin_plus_ineligible"):
        assert_verdict(state, second[name], admitted=False, reason=R_APPROVAL_MISSING)
    assert_not_member(state, dave)

    third = drive_actions(org, [
        Step("two_admins", lambda o: o.claim(inv, ik, dave, approvers=[bob, carol])),
    ])
    state = org.fold()
    assert_verdict(state, third["two_admins"], admitted=True)
    assert_member(state, dave, roles=["reviewer"])
    assert_holds(state, dave, "link:publish")
