"""Invite lifecycle: scoped grants, claim_requires, expiry, races, cascade."""

from __future__ import annotations

import hashlib

from tools.network.idkit import KeyPair, generate_token
from tools.network.ledger import (
    INVITE_CLAIMED,
    INVITE_DEAD,
    INVITE_EXPIRED,
    INVITE_LIVE,
    INVITE_REVOKED,
    fold,
)
from tools.network.ledger.fold import (
    R_APPROVAL_BAD,
    R_APPROVAL_MISSING,
    R_CLAIM_BAD_TOKEN,
    R_CLAIM_WRONG_KEY,
    R_INVITE_ALREADY_CLAIMED,
    R_INVITE_DEAD,
    R_INVITE_EXPIRED,
    R_INVITE_NOT_IN_ANCESTRY,
    R_PERSONA_EXISTS,
)

from .conftest import FAR, Sim
from .test_l3_safety_merge import replay_states


def org_with_member_role(requires="self"):
    sim = Sim()
    sim.role_define(sim.root, "member", ["link:publish"], requires=requires)
    sponsor = KeyPair.generate()
    sim.delegate(sim.root, sponsor, ["invite:member"])
    return sim, sponsor


class TestLifecycle:
    def test_full_join_flow(self):
        sim, sponsor = org_with_member_role()
        ik, persona = KeyPair.generate(), KeyPair.generate()
        invite = sim.invite(sponsor, "member", invite_key=ik)
        assert fold(sim.ledger).invites[invite] == INVITE_LIVE
        claim = sim.claim(invite, ik, persona)
        state = fold(sim.ledger)
        assert state.valid[claim] is True
        member = state.members[persona.public_hex]
        assert member.sponsor == sponsor.public_hex  # provenance recorded forever
        assert member.roles == ("member",)
        assert member.current_key == persona.public_hex
        assert state.invites[invite] == INVITE_CLAIMED
        assert state.holds(persona.public_hex, "link:publish")

    def test_token_invite_flow(self):
        """Bearer safety (auto-g6q9d, register B6): a token-bound claim
        never self-completes — the old immediate-admit behavior of this
        test is now exactly what the invariant forbids; with one sponsor
        countersignature the same claim admits (the original intent:
        a correct token joins the member — now via countersigned staging).
        """
        from tools.network.ledger.fold import R_APPROVAL_MISSING

        sim, sponsor = org_with_member_role()
        token = generate_token()
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        persona = KeyPair.generate()
        invite = sim.invite(sponsor, "member", token_hash=token_hash)
        bare = sim.claim(invite, persona, persona, token=token)
        state = fold(sim.ledger)
        assert state.valid[bare] is False
        assert state.reasons[bare] == R_APPROVAL_MISSING
        assert persona.public_hex not in state.members

        # The sponsor's countersignature completes it.
        persona2 = KeyPair.generate()
        invite2 = sim.invite(sponsor, "member", token_hash=token_hash)
        signed = sim.claim(invite2, persona2, persona2, token=token,
                           approvers=[sponsor])
        state = fold(sim.ledger)
        assert state.valid[signed] is True
        assert persona2.public_hex in state.members

    def test_wrong_token_rejected(self):
        sim, sponsor = org_with_member_role()
        token_hash = hashlib.sha256(b"right").hexdigest()
        persona = KeyPair.generate()
        invite = sim.invite(sponsor, "member", token_hash=token_hash)
        claim = sim.claim(invite, persona, persona, token="wrong")
        state = fold(sim.ledger)
        assert state.valid[claim] is False
        assert state.reasons[claim] == R_CLAIM_BAD_TOKEN

    def test_claim_must_be_signed_by_invite_key(self):
        sim, sponsor = org_with_member_role()
        ik, persona, mallory = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
        invite = sim.invite(sponsor, "member", invite_key=ik)
        claim = sim.claim(invite, mallory, persona)  # wrong signer
        state = fold(sim.ledger)
        assert state.valid[claim] is False
        assert state.reasons[claim] == R_CLAIM_WRONG_KEY

    def test_claim_requires_invite_in_ancestry(self):
        sim, sponsor = org_with_member_role()
        ik, persona = KeyPair.generate(), KeyPair.generate()
        base = sim.ledger.heads()
        invite = sim.invite(sponsor, "member", invite_key=ik, parents=base)
        claim = sim.claim(invite, ik, persona, parents=base)  # concurrent, unseen
        state = fold(sim.ledger)
        assert state.valid[claim] is False
        assert state.reasons[claim] == R_INVITE_NOT_IN_ANCESTRY

    def test_invite_single_use(self):
        sim, sponsor = org_with_member_role()
        ik, p1, p2 = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
        invite = sim.invite(sponsor, "member", invite_key=ik)
        sim.claim(invite, ik, p1)
        second = sim.claim(invite, ik, p2)  # causally after the first
        state = fold(sim.ledger)
        assert state.valid[second] is False
        assert state.reasons[second] == R_INVITE_ALREADY_CLAIMED
        assert p2.public_hex not in state.members

    def test_persona_key_is_unique(self):
        sim, sponsor = org_with_member_role()
        ik1, ik2, persona = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
        i1 = sim.invite(sponsor, "member", invite_key=ik1)
        i2 = sim.invite(sponsor, "member", invite_key=ik2)
        sim.claim(i1, ik1, persona)
        dup = sim.claim(i2, ik2, persona)
        state = fold(sim.ledger)
        assert state.valid[dup] is False
        assert state.reasons[dup] == R_PERSONA_EXISTS


class TestExpiry:
    def test_claim_after_expiry_rejected(self):
        sim, sponsor = org_with_member_role()
        ik, persona = KeyPair.generate(), KeyPair.generate()
        expiry = sim.next_ts() + 500
        invite = sim.invite(sponsor, "member", invite_key=ik, expiry=expiry)
        claim = sim.claim(invite, ik, persona)  # sim clock has moved past expiry
        state = fold(sim.ledger)
        assert state.valid[claim] is False
        assert state.reasons[claim] == R_INVITE_EXPIRED

    def test_unclaimed_invite_reports_expired_with_now(self):
        sim, sponsor = org_with_member_role()
        ik = KeyPair.generate()
        expiry = sim.next_ts() + 500
        invite = sim.invite(sponsor, "member", invite_key=ik, expiry=expiry)
        assert fold(sim.ledger, now=expiry - 100).invites[invite] == INVITE_LIVE
        assert fold(sim.ledger, now=expiry + 100).invites[invite] == INVITE_EXPIRED


class TestClaimRequires:
    def test_sponsor_countersignature_required(self):
        sim, sponsor = org_with_member_role(requires="sponsor")
        ik, persona = KeyPair.generate(), KeyPair.generate()
        invite = sim.invite(sponsor, "member", invite_key=ik)
        bare = sim.claim(invite, ik, persona)
        state = fold(sim.ledger)
        assert state.valid[bare] is False
        assert state.reasons[bare] == R_APPROVAL_MISSING

        persona2, ik2 = KeyPair.generate(), KeyPair.generate()
        invite2 = sim.invite(sponsor, "member", invite_key=ik2)
        ok = sim.claim(invite2, ik2, persona2, approvers=[sponsor])
        state = fold(sim.ledger)
        assert state.valid[ok] is True
        assert persona2.public_hex in state.members

    def test_admin_ack_requires_authorized_approver(self):
        sim = Sim()
        sim.role_define(sim.root, "member", [], requires="admin-ack")
        sponsor, admin, rando = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, sponsor, ["invite:member"])
        sim.delegate(sim.root, admin, ["role:grant:member"])

        ik1, p1 = KeyPair.generate(), KeyPair.generate()
        i1 = sim.invite(sponsor, "member", invite_key=ik1)
        bad = sim.claim(i1, ik1, p1, approvers=[rando])
        state = fold(sim.ledger)
        assert state.valid[bad] is False
        assert state.reasons[bad] == R_APPROVAL_MISSING

        ik2, p2 = KeyPair.generate(), KeyPair.generate()
        i2 = sim.invite(sponsor, "member", invite_key=ik2)
        ok = sim.claim(i2, ik2, p2, approvers=[admin])
        assert fold(sim.ledger).valid[ok] is True

    def test_forged_approval_signature_rejected(self):
        sim, sponsor = org_with_member_role(requires="sponsor")
        ik, persona = KeyPair.generate(), KeyPair.generate()
        invite = sim.invite(sponsor, "member", invite_key=ik)
        payload = {
            "type": "member.claim",
            "invite_ref": invite,
            "persona_pub": persona.public_hex,
            "profile": {},
            "approvals": [{"key": sponsor.public_hex, "sig": "0" * 128}],
        }
        claim = sim.emit(ik, payload)
        state = fold(sim.ledger)
        assert state.valid[claim] is False
        assert state.reasons[claim] == R_APPROVAL_BAD


class TestInviteCascadeAndRaces:
    def test_unclaimed_invite_dies_with_inviter_demotion(self):
        sim, sponsor = org_with_member_role()
        ik = KeyPair.generate()
        invite = sim.invite(sponsor, "member", invite_key=ik)
        # find the sponsor's delegation and revoke it (demotion)
        g_s = next(
            e.event_id
            for e in sim.ledger.events()
            if e.type == "delegate" and e.payload["child_pub"] == sponsor.public_hex
        )
        sim.revoke_event(sim.root, g_s)
        for state in replay_states(sim):
            assert state.invites[invite] == INVITE_DEAD

        # A claim landing after the demotion (causal-ancestry validation) fails.
        persona = KeyPair.generate()
        claim = sim.claim(invite, ik, persona)
        state = fold(sim.ledger)
        assert state.valid[claim] is False
        assert state.reasons[claim] == R_INVITE_DEAD

    def test_claim_vs_revoke_race_revoke_wins(self):
        sim, sponsor = org_with_member_role()
        ik, persona = KeyPair.generate(), KeyPair.generate()
        invite = sim.invite(sponsor, "member", invite_key=ik)
        base = sim.ledger.heads()
        sim.revoke_event(sponsor, invite, parents=base)
        claim = sim.claim(invite, ik, persona, parents=base)  # concurrent
        sim.checkpoint(sim.root)
        for state in replay_states(sim):
            assert state.valid[claim] is True  # issuance-valid in its ancestry…
            assert persona.public_hex not in state.members  # …but the revoke wins
            assert state.invites[invite] == INVITE_REVOKED
            assert not state.holds(persona.public_hex, "link:publish")

    def test_claim_vs_inviter_demotion_race_demotion_wins(self):
        sim, sponsor = org_with_member_role()
        ik, persona = KeyPair.generate(), KeyPair.generate()
        g_s = next(
            e.event_id
            for e in sim.ledger.events()
            if e.type == "delegate" and e.payload["child_pub"] == sponsor.public_hex
        )
        invite = sim.invite(sponsor, "member", invite_key=ik)
        base = sim.ledger.heads()
        sim.revoke_event(sim.root, g_s, parents=base)
        claim = sim.claim(invite, ik, persona, parents=base)  # concurrent with demotion
        sim.checkpoint(sim.root)
        for state in replay_states(sim):
            assert persona.public_hex not in state.members
            assert not state.holds(persona.public_hex, "link:publish")
        assert fold(sim.ledger).valid[claim] is True  # judged at its own ancestry

    def test_revoke_after_claim_does_not_unmake_member(self):
        sim, sponsor = org_with_member_role()
        ik, persona = KeyPair.generate(), KeyPair.generate()
        invite = sim.invite(sponsor, "member", invite_key=ik)
        sim.claim(invite, ik, persona)
        sim.revoke_event(sponsor, invite)  # causally AFTER the claim
        for state in replay_states(sim):
            assert persona.public_hex in state.members

    def test_membership_explicitly_revocable(self):
        sim, sponsor = org_with_member_role()
        ik, persona = KeyPair.generate(), KeyPair.generate()
        invite = sim.invite(sponsor, "member", invite_key=ik)
        claim = sim.claim(invite, ik, persona)
        rev = sim.revoke_event(sponsor, claim)  # sponsor removes the member
        for state in replay_states(sim):
            assert state.valid[rev] is True
            assert persona.public_hex not in state.members
            assert not state.holds(persona.public_hex, "link:publish")

    def test_invite_key_revocation_kills_invite(self):
        sim, sponsor = org_with_member_role()
        ik, persona = KeyPair.generate(), KeyPair.generate()
        invite = sim.invite(sponsor, "member", invite_key=ik)
        sim.revoke_key(sponsor, ik)
        claim = sim.claim(invite, ik, persona)
        state = fold(sim.ledger)
        assert state.valid[claim] is False
        assert state.invites[invite] == INVITE_REVOKED


class TestApproverThreshold:
    """N-of-M distinct admin approvers on member.claim (register D16)."""

    def test_threshold_folds_and_defaults_to_one(self):
        sim = Sim()
        sim.role_define(sim.root, "member", requires="admin-ack", approver_threshold=1)
        sim.role_define(sim.root, "guest", requires="admin-ack")
        state = sim.fold()
        assert state.role_defs["member"].approver_threshold == 1
        assert state.role_defs["guest"].approver_threshold == 1

    def test_fingerprint_commits_to_the_threshold(self):
        from tools.network.ledger import HLC, Ledger, fold, make_event

        from .conftest import ORG, T0

        root = KeyPair.generate()

        def build(count):
            ledger = Ledger()
            gid = ledger.add(
                make_event(
                    root,
                    {"type": "genesis", "org": ORG, "root_pub": root.public_hex},
                    [],
                    HLC(T0),
                )
            )
            ledger.add(
                make_event(
                    root,
                    {
                        "type": "role.define",
                        "name": "member",
                        "scope_set": [],
                        "claim_requires": "admin-ack",
                        "version": 1,
                        "approver_threshold": {"kind": "static", "count": count},
                    },
                    [gid],
                    HLC(T0 + 1_000),
                )
            )
            return fold(ledger).fingerprint()

        assert build(2) != build(3)

    def test_two_of_m_requires_two_distinct_authorized_approvers(self):
        from tools.network.ledger.fold import R_APPROVAL_MISSING

        sim = Sim()
        sim.role_define(sim.root, "member", requires="admin-ack", approver_threshold=2)
        admin1, admin2 = KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, admin1, ["role:grant:member"])
        sim.delegate(sim.root, admin2, ["role:grant:member"])

        invite_key, persona = KeyPair.generate(), KeyPair.generate()
        iid = sim.invite(sim.root, "member", invite_key=invite_key)
        under = sim.claim(iid, invite_key, persona, approvers=[admin1])
        state = sim.fold()
        assert state.valid[under] is False
        assert state.reasons[under] == R_APPROVAL_MISSING
        assert persona.public_hex not in state.members

        invite_key2, persona2 = KeyPair.generate(), KeyPair.generate()
        iid2 = sim.invite(sim.root, "member", invite_key=invite_key2)
        met = sim.claim(iid2, invite_key2, persona2, approvers=[admin1, admin2])
        state = sim.fold()
        assert state.valid[met] is True
        assert persona2.public_hex in state.members

    def test_sponsor_vouch_counts_as_one(self):
        from tools.network.ledger.fold import R_APPROVAL_MISSING

        sim = Sim()
        sim.role_define(sim.root, "member", requires="admin-ack", approver_threshold=2)
        sponsor, admin = KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, sponsor, ["invite:member"])
        sim.delegate(sim.root, admin, ["role:grant:member"])

        invite_key, persona = KeyPair.generate(), KeyPair.generate()
        iid = sim.invite(sponsor, "member", invite_key=invite_key)
        met = sim.claim(iid, invite_key, persona, approvers=[sponsor, admin])
        state = sim.fold()
        assert state.valid[met] is True  # sponsor vouch + one admin = two

        # One admin plus a signer with no admission authority: below the bar.
        rando = KeyPair.generate()
        invite_key2, persona2 = KeyPair.generate(), KeyPair.generate()
        iid2 = sim.invite(sponsor, "member", invite_key=invite_key2)
        under = sim.claim(iid2, invite_key2, persona2, approvers=[admin, rando])
        state = sim.fold()
        assert state.valid[under] is False
        assert state.reasons[under] == R_APPROVAL_MISSING
