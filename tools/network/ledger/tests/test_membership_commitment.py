"""Membership commitment (auto-i24yr): trees, fold projections, checkpoints.

Covers the bead's acceptance list: pinned root vectors (an accidental
hash-order or domain change fails loudly), proof round-trips across sizes
and positions, rekey swapping exactly one leaf, the membership:checkpoint
scope moving a persona between checkpointer trees, determinism, checkpoint
build/validate round-trips including seed and reset, and one test per
validation refusal.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import fold
from tools.network.ledger import membership_commitment as mc

from .conftest import Sim

A, B, C = "11" * 32, "22" * 32, "33" * 32

# Pinned vectors — recompute ONLY for a deliberate, versioned domain change.
R1 = "9fd0e760ef8df246921f3e3d43e69e2cc7b18801577abe10f78ed64deedebb41"
R2 = "550473dfff1ccc6b48bdc95b721683895d303ac5904e77b39c3b2cc6d81e4c3d"
R3 = "64708a27739a167daf113d92d5f8c8642524ff5a0d36f89e0d3b1a83a1e02b04"
EMPTY = "1705270d8260bc73098fa95a08318c10e6975a61d44d7848040b4199ff7c7b00"


class TestTree:
    def test_pinned_vectors(self):
        assert mc.compute_root([A]) == R1
        assert mc.compute_root([A, B]) == R2
        assert mc.compute_root([C, A, B]) == R3
        assert mc.compute_root([]) == EMPTY

    def test_input_order_and_duplicates_are_irrelevant(self):
        assert mc.compute_root([B, A]) == mc.compute_root([A, B]) == R2
        assert mc.compute_root([A, B, A]) == R2

    @pytest.mark.parametrize("n", [1, 2, 3, 10, 100])
    def test_proof_round_trip_first_middle_last(self, n):
        pubs = [f"{i:064x}" for i in range(1, n + 1)]
        root = mc.compute_root(pubs)
        for target in {pubs[0], pubs[n // 2], pubs[-1]}:
            index, path = mc.inclusion_proof(pubs, target)
            mc.verify_inclusion(root, target, index, path)
        # Padded perfect tree: every proof has exactly ceil(log2 n) siblings.
        depth = (n - 1).bit_length()
        assert all(
            len(mc.inclusion_proof(pubs, p)[1]) == depth for p in (pubs[0], pubs[-1])
        )

    def test_proof_for_absent_persona_refused(self):
        with pytest.raises(mc.MembershipCommitmentError, match="not in the committed"):
            mc.inclusion_proof([A, B], C)

    def test_verify_refusals(self):
        pubs = [A, B, C]
        root = mc.compute_root(pubs)
        index, path = mc.inclusion_proof(pubs, B)
        with pytest.raises(mc.MembershipCommitmentError, match="does not verify"):
            mc.verify_inclusion(root, C, index, path)  # wrong leaf
        with pytest.raises(mc.MembershipCommitmentError, match="does not verify"):
            mc.verify_inclusion(mc.compute_root([A, B]), B, index, path)  # wrong root
        with pytest.raises(mc.MembershipCommitmentError, match="exceeds the tree"):
            mc.verify_inclusion(root, B, index + (1 << len(path)), path)
        with pytest.raises(mc.MembershipCommitmentError, match="64 lowercase hex"):
            mc.verify_inclusion(root, B, index, ["zz" * 32] * len(path))
        with pytest.raises(mc.MembershipCommitmentError, match="at most"):
            mc.verify_inclusion(root, B, 0, ["00" * 32] * (mc.MAX_PROOF_DEPTH + 1))


def org_with_owner():
    """Genesis + an owner-role member claimed by a founder persona — the
    solo-org shape: the founder is sole member AND sole checkpointer via the
    owner's ``*`` scope, with no special casing."""
    sim = Sim()
    sim.role_define(sim.root, "owner", ["*"], requires="self")
    founder, ik = KeyPair.generate(), KeyPair.generate()
    invite = sim.invite(sim.root, "owner", invite_key=ik)
    sim.claim(invite, ik, founder)
    return sim, founder


def add_member(sim, role="member", scopes=("link:publish",)):
    if role not in sim.fold().role_defs:
        sim.role_define(sim.root, role, list(scopes), requires="self")
    persona, ik = KeyPair.generate(), KeyPair.generate()
    invite = sim.invite(sim.root, role, invite_key=ik)
    sim.claim(invite, ik, persona)
    return persona


class TestFoldProjections:
    def test_founder_is_sole_member_and_checkpointer(self):
        sim, founder = org_with_owner()
        state = sim.fold()
        assert mc.member_pubs(state) == (founder.public_hex,)
        assert mc.checkpointer_pubs(state) == (founder.public_hex,)
        assert mc.members_root(state) == mc.compute_root([founder.public_hex])

    def test_plain_member_is_not_a_checkpointer(self):
        sim, founder = org_with_owner()
        member = add_member(sim)
        state = sim.fold()
        assert set(mc.member_pubs(state)) == {founder.public_hex, member.public_hex}
        assert mc.checkpointer_pubs(state) == (founder.public_hex,)

    def test_checkpoint_scope_grant_moves_the_persona(self):
        sim, founder = org_with_owner()
        member = add_member(sim)
        sim.role_define(sim.root, "steward", [mc.CHECKPOINT_SCOPE], requires="self")
        sim.role_grant(sim.root, member, "steward")
        state = sim.fold()
        assert set(mc.checkpointer_pubs(state)) == {
            founder.public_hex, member.public_hex}
        sim.role_revoke(sim.root, member, "steward")
        assert mc.checkpointer_pubs(sim.fold()) == (founder.public_hex,)

    def test_rekey_swaps_exactly_one_leaf(self):
        sim, founder = org_with_owner()
        member = add_member(sim)
        before = set(mc.member_pubs(sim.fold()))
        new_key = KeyPair.generate()
        sim.rekey(member, member, member, new_key)
        after = set(mc.member_pubs(sim.fold()))
        assert before - after == {member.public_hex}
        assert after - before == {new_key.public_hex}
        assert mc.members_root(sim.fold()) != mc.compute_root(sorted(before))

    def test_determinism_across_independent_folds(self):
        sim, _ = org_with_owner()
        add_member(sim)
        heads = tuple(sim.ledger.heads())
        one = fold(sim.ledger, heads=heads)
        two = fold(sim.ledger, heads=heads)
        assert mc.members_root(one) == mc.members_root(two)
        assert mc.checkpointers_root(one) == mc.checkpointers_root(two)


def chain(sim, founder):
    """seed (root-signed) → cp1 (founder-signed) over the current fold."""
    state = sim.fold()
    genesis = sim.genesis_id
    seed = mc.build_root_checkpoint(
        org=state.org, seq=0, genesis_id=genesis, ledger_head=genesis,
        members_root_hex=mc.members_root(state),
        checkpointers_root_hex=mc.checkpointers_root(state),
        ts=1_800_000_000, root=sim.root)
    cp1 = mc.build_checkpoint(
        org=state.org, seq=1, prev=mc.checkpoint_hash(seed),
        ledger_head=sorted(state.heads)[0],
        members_root_hex=mc.members_root(state),
        checkpointers_root_hex=mc.checkpointers_root(state),
        ts=1_800_000_100, signer=founder,
        prev_checkpointer_pubs=mc.checkpointer_pubs(state))
    return seed, cp1


class TestCheckpoints:
    def test_seed_and_member_chain_round_trip(self):
        sim, founder = org_with_owner()
        seed, cp1 = chain(sim, founder)
        mc.validate_checkpoint(seed, root_pub=sim.root.public_hex)
        mc.validate_checkpoint(cp1, root_pub=sim.root.public_hex, prev_record=seed)

    def test_reset_validates_at_any_seq_without_prev(self):
        sim, founder = org_with_owner()
        state = sim.fold()
        reset = mc.build_root_checkpoint(
            org=state.org, seq=42, genesis_id=sim.genesis_id,
            ledger_head=sorted(state.heads)[0],
            members_root_hex=mc.members_root(state),
            checkpointers_root_hex=mc.checkpointers_root(state),
            ts=1_800_000_200, root=sim.root)
        mc.validate_checkpoint(reset, root_pub=sim.root.public_hex)

    def test_non_checkpointer_cannot_build(self):
        sim, founder = org_with_owner()
        member = add_member(sim)
        state = sim.fold()
        with pytest.raises(mc.MembershipCommitmentError, match="not in the committed"):
            mc.build_checkpoint(
                org=state.org, seq=1, prev="0" * 64, ledger_head="0" * 64,
                members_root_hex=mc.members_root(state),
                checkpointers_root_hex=mc.checkpointers_root(state),
                ts=1, signer=member,
                prev_checkpointer_pubs=mc.checkpointer_pubs(state))

    def test_member_seq_zero_refused_at_build(self):
        sim, founder = org_with_owner()
        state = sim.fold()
        with pytest.raises(mc.MembershipCommitmentError, match="seq 1"):
            mc.build_checkpoint(
                org=state.org, seq=0, prev="0" * 64, ledger_head="0" * 64,
                members_root_hex=mc.members_root(state),
                checkpointers_root_hex=mc.checkpointers_root(state),
                ts=1, signer=founder,
                prev_checkpointer_pubs=mc.checkpointer_pubs(state))

    def test_validation_refusals(self):
        sim, founder = org_with_owner()
        seed, cp1 = chain(sim, founder)
        root_pub = sim.root.public_hex

        skipped = dict(cp1, seq=3)
        with pytest.raises(mc.MembershipCommitmentError, match="exactly one"):
            mc.validate_checkpoint(skipped, root_pub=root_pub, prev_record=seed)

        broken = dict(cp1, prev="0" * 64)
        with pytest.raises(mc.MembershipCommitmentError, match="hash-link"):
            mc.validate_checkpoint(broken, root_pub=root_pub, prev_record=seed)

        wrong_org = dict(cp1, org="someone-else")
        with pytest.raises(mc.MembershipCommitmentError, match="org does not match"):
            mc.validate_checkpoint(wrong_org, root_pub=root_pub, prev_record=seed)

        # Tampered field: linkage and proof are fine, the signature is not.
        tampered = dict(cp1, members_root="0" * 64)
        with pytest.raises(mc.MembershipCommitmentError, match="signature"):
            mc.validate_checkpoint(tampered, root_pub=root_pub, prev_record=seed)

        with pytest.raises(mc.MembershipCommitmentError, match="previous record"):
            mc.validate_checkpoint(cp1, root_pub=root_pub)  # member form, no prev

        # Form confusion both ways.
        rootish = {k: v for k, v in cp1.items()
                   if k not in ("proof", "proof_index")}
        with pytest.raises(mc.MembershipCommitmentError, match="fields"):
            # member-signed record missing its proof fields
            mc.validate_checkpoint(rootish, root_pub=root_pub, prev_record=seed)
        seedish = dict(seed, proof=[], proof_index=0)
        with pytest.raises(mc.MembershipCommitmentError, match="fields"):
            mc.validate_checkpoint(seedish, root_pub=root_pub)

        # Signer proven under the WRONG root: rebuild cp1 against a bogus
        # previous checkpointers_root — the embedded proof no longer lands.
        bogus_prev = dict(seed, checkpointers_root=mc.compute_root([A, B]))
        with pytest.raises(mc.MembershipCommitmentError, match="does not verify"):
            mc.validate_checkpoint(
                dict(cp1, prev=mc.checkpoint_hash(bogus_prev)),
                root_pub=root_pub, prev_record=bogus_prev)


# ── Delegate-signed checkpoints (DelegateCheckpoint.spthy; OrgAdmission P2) ──
from tools.network.storagekit.delegate import storage_delegate_scopes as _sds

DOMAIN = "dd" * 32


def _grant(sim, persona, child, *, ttl=90 * 86_400_000, checkpointer=True, ts=None):
    scopes = _sds(DOMAIN, checkpointer=checkpointer)
    gid = sim.delegate(persona, child, scopes, ttl=ttl, ts=ts)
    assert sim.fold().valid[gid], sim.fold().reasons.get(gid)
    return sim.ledger.get(gid)


def _seed(sim, checkpointers=None, ts=1_800_000_000):
    state = sim.fold()
    return mc.build_root_checkpoint(
        org="org-1", seq=0, genesis_id=sim.genesis_id, ledger_head=sim.genesis_id,
        members_root_hex=mc.members_root(state),
        checkpointers_root_hex=mc.compute_root(checkpointers) if checkpointers
        else mc.checkpointers_root(state),
        ts=ts, root=sim.root)


def _delegate_record(sim, seed, child, grant, *, ts, genesis=None, prev_checkpointers=None):
    state = sim.fold()
    return mc.build_delegate_checkpoint(
        org="org-1", seq=seed["seq"] + 1, prev=mc.checkpoint_hash(seed),
        ledger_head=sorted(state.heads)[0], members_root_hex=mc.members_root(state),
        checkpointers_root_hex=mc.checkpointers_root(state), ts=ts, delegate=child,
        grant_wire=grant.to_json().decode("utf-8"),
        genesis_id=genesis or sim.genesis_id,
        prev_checkpointer_pubs=prev_checkpointers or mc.checkpointer_pubs(state))


class TestDelegateCheckpoints:
    def test_delegate_signed_advance_is_attributed_to_the_granting_persona(self):
        sim, founder = org_with_owner()
        child = KeyPair.generate()
        grant = _grant(sim, founder, child)
        seed = _seed(sim)
        now = grant.hlc.ts // 1000 + 10
        record = _delegate_record(sim, seed, child, grant, ts=now)
        assert record["signer"] == child.public_hex
        mc.validate_checkpoint(record, root_pub=sim.root.public_hex, prev_record=seed, now=now)
        assert mc.checkpoint_signer_persona(record) == founder.public_hex
        # The chain continues from it: a persona-signed record hash-links to it.
        state = sim.fold()
        nxt = mc.build_checkpoint(
            org="org-1", seq=2, prev=mc.checkpoint_hash(record),
            ledger_head=sorted(state.heads)[0], members_root_hex=mc.members_root(state),
            checkpointers_root_hex=mc.checkpointers_root(state), ts=now + 1,
            signer=founder, prev_checkpointer_pubs=mc.checkpointer_pubs(state))
        mc.validate_checkpoint(nxt, root_pub=sim.root.public_hex, prev_record=record, now=now + 1)

    def test_the_verifiers_clock_judges_ts_and_ttl(self):
        """F1: a stolen delegate cannot backdate its way inside the TTL."""
        sim, founder = org_with_owner()
        child = KeyPair.generate()
        grant = _grant(sim, founder, child, ttl=3_600_000)
        seed = _seed(sim)
        now = grant.hlc.ts // 1000 + 10
        backdated = _delegate_record(sim, seed, child, grant, ts=now - 400)
        with pytest.raises(mc.MembershipCommitmentError, match="outside the verifier"):
            mc.validate_checkpoint(backdated, root_pub=sim.root.public_hex, prev_record=seed, now=now)
        later = now + 7_200
        expired = _delegate_record(sim, seed, child, grant, ts=later)
        with pytest.raises(mc.MembershipCommitmentError, match="expired"):
            mc.validate_checkpoint(expired, root_pub=sim.root.public_hex, prev_record=seed, now=later)
        # History replay (now=None) checks structure and signatures only.
        mc.validate_checkpoint(expired, root_pub=sim.root.public_hex, prev_record=seed, now=None)

    def test_every_attribution_check_is_named(self):
        sim, founder = org_with_owner()
        child = KeyPair.generate()
        grant = _grant(sim, founder, child)
        seed = _seed(sim)
        now = grant.hlc.ts // 1000 + 10
        # A grant proof made over another organization's genesis.
        wrong_genesis = _delegate_record(sim, seed, child, grant, ts=now, genesis="ee" * 32)
        with pytest.raises(mc.MembershipCommitmentError, match="proof of possession"):
            mc.validate_checkpoint(wrong_genesis, root_pub=sim.root.public_hex, prev_record=seed, now=now)
        # A storage-only grant (no checkpoint scope) attributes nothing.
        other = KeyPair.generate()
        storage_only = _grant(sim, founder, other, checkpointer=False)
        with pytest.raises(mc.MembershipCommitmentError, match="checkpoint scope"):
            mc.validate_checkpoint(
                _delegate_record(sim, seed, other, storage_only, ts=now),
                root_pub=sim.root.public_hex, prev_record=seed, now=now)
        # A grant for another key than the signer.
        mismatched = dict(_delegate_record(sim, seed, child, grant, ts=now))
        mismatched["grant"] = storage_only.to_json().decode("utf-8")
        with pytest.raises(mc.MembershipCommitmentError, match="does not name the signer|does not verify"):
            mc.validate_checkpoint(mismatched, root_pub=sim.root.public_hex, prev_record=seed, now=now)
        # The granting persona is not in the previous checkpointers root.
        stranger_seed = _seed(sim, checkpointers=[KeyPair.generate().public_hex])
        outside = _delegate_record(sim, stranger_seed, child, grant, ts=now,
                                   prev_checkpointers=[founder.public_hex])
        with pytest.raises(mc.MembershipCommitmentError):
            mc.validate_checkpoint(outside, root_pub=sim.root.public_hex, prev_record=stranger_seed, now=now)
        # A tampered record fails the delegate's signature.
        tampered = dict(_delegate_record(sim, seed, child, grant, ts=now))
        tampered["members_root"] = "f" * 64
        with pytest.raises(mc.MembershipCommitmentError, match="signature"):
            mc.validate_checkpoint(tampered, root_pub=sim.root.public_hex, prev_record=seed, now=now)


class TestMemberSetEventTypes:
    def test_types_outside_the_set_never_change_members_root(self):
        """MEMBER_SET_EVENT_TYPES is the own-fold candidate walk's set: a fold
        at any other event as head reproduces the root of its nearest
        member-set ancestor, so those events are never folded for candidates."""
        sim, founder = org_with_owner()
        member = add_member(sim)
        before = mc.members_root(sim.fold())
        sim.role_define(sim.root, "extra", ["link:publish"], requires="self")
        sim.role_grant(sim.root, member.public_hex, "extra")
        sim.invite(sim.root, "member", invite_key=KeyPair.generate())
        _grant(sim, founder, KeyPair.generate(), checkpointer=False)
        checked = 0
        for event in sim.ledger.events():
            if event.type in mc.MEMBER_SET_EVENT_TYPES or event.type == "genesis":
                continue
            # The event as head against its own parents as heads: the same set.
            with_event = mc.members_root(sim.fold(heads=[event.event_id]))
            without = mc.members_root(sim.fold(heads=list(event.parents)))
            assert with_event == without, event.type
            checked += 1
        assert checked >= 4  # role.define, role.grant, invite, delegate
        assert mc.members_root(sim.fold()) == before
        assert set(mc.MEMBER_SET_EVENT_TYPES) == {
            "member.claim", "member.admission", "member.rekey", "revoke", "role.revoke", "key.rotate"}
