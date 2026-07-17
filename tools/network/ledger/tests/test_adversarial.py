"""Adversarial shapes: things a hostile author or replica might try."""

from __future__ import annotations

from tools.network.idkit import KeyPair
from tools.network.ledger import fold
from tools.network.ledger.fold import (
    R_REVOKE_BAD_TARGET,
    R_REVOKE_NOT_IN_ANCESTRY,
    R_SCOPE_ESCALATION,
)

from .conftest import Sim
from .test_l3_safety_merge import replay_states


class TestRevokeAbuse:
    def test_revoke_of_non_authority_events_rejected(self, sim):
        a = KeyPair.generate()
        g = sim.delegate(sim.root, a, ["link:publish"])
        rev = sim.revoke_event(sim.root, g)
        for target in (sim.genesis_id, rev, sim.checkpoint(sim.root)):
            bad = sim.revoke_event(sim.root, target)
            state = fold(sim.ledger)
            assert state.valid[bad] is False
            assert state.reasons[bad] == R_REVOKE_BAD_TARGET

    def test_revoke_of_unseen_event_rejected(self, sim):
        a = KeyPair.generate()
        base = sim.ledger.heads()
        g = sim.delegate(sim.root, a, ["link:publish"], parents=base)
        rev = sim.revoke_event(sim.root, g, parents=base)  # concurrent: never saw g
        state = fold(sim.ledger)
        assert state.valid[rev] is False
        assert state.reasons[rev] == R_REVOKE_NOT_IN_ANCESTRY
        assert state.holds(a.public_hex, "link:publish")

    def test_demoted_granter_keeps_revocation_over_own_subtree(self, sim):
        """Revocation authority is provenance, not live authority: a demoted
        admin can still clean up what they created (attenuation-safe)."""
        a, b = KeyPair.generate(), KeyPair.generate()
        g_a = sim.delegate(sim.root, a, ["link:publish"], redelegate=True)
        g_b = sim.delegate(a, b, ["link:publish"])
        sim.revoke_event(sim.root, g_a)  # demote a
        sim.delegate(sim.root, b, ["link:publish"])  # b re-anchored directly
        rev = sim.revoke_event(a, g_b)  # a revokes their own old grant
        state = fold(sim.ledger)
        assert state.valid[rev] is True

    def test_stranger_cannot_revoke_key(self, sim):
        a, mallory = KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, a, ["link:publish"])
        bad = sim.revoke_key(mallory, a)
        state = fold(sim.ledger)
        assert state.valid[bad] is False
        assert state.holds(a.public_hex, "link:publish")


class TestEscalationAttempts:
    def test_deep_chain_cannot_regain_dropped_scope(self, sim):
        keys = [KeyPair.generate() for _ in range(5)]
        sim.delegate(sim.root, keys[0], ["link:publish", "link:revoke"], redelegate=True)
        sim.delegate(keys[0], keys[1], ["link:publish", "link:revoke"], redelegate=True)
        sim.delegate(keys[1], keys[2], ["link:publish"], redelegate=True)  # drops link:revoke
        bad = sim.delegate(keys[2], keys[3], ["link:publish", "link:revoke"])
        ok = sim.delegate(keys[2], keys[4], ["link:publish"])
        state = fold(sim.ledger)
        assert state.valid[bad] is False and state.reasons[bad] == R_SCOPE_ESCALATION
        assert state.valid[ok] is True
        assert not state.holds(keys[3].public_hex, "link:revoke")
        assert not state.holds(keys[3].public_hex, "link:publish")
        assert state.holds(keys[4].public_hex, "link:publish")

    def test_two_keys_cannot_launder_scope_between_branches(self, sim):
        """a holds X, b holds Y concurrently; neither can mint X+Y."""
        a, b, c = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
        base = sim.ledger.heads()
        sim.delegate(sim.root, a, ["link:publish"], redelegate=True, parents=base)
        sim.delegate(sim.root, b, ["link:revoke"], redelegate=True, parents=base)
        sim.checkpoint(sim.root)
        bad_a = sim.delegate(a, c, ["link:publish", "link:revoke"])
        bad_b = sim.delegate(b, c, ["link:publish", "link:revoke"])
        state = fold(sim.ledger)
        assert state.valid[bad_a] is False
        assert state.valid[bad_b] is False
        assert state.authority(c.public_hex) == frozenset()

    def test_role_scope_invites_compose_attenuation(self, sim):
        """A role whose scope_set includes invite:member makes membership
        viral — but only for that role, and only while the member holds it."""
        sim.role_define(sim.root, "member", ["invite:member"])
        sim.role_define(sim.root, "admin", [])
        ik1, p1 = KeyPair.generate(), KeyPair.generate()
        i1 = sim.invite(sim.root, "member", invite_key=ik1)
        sim.claim(i1, ik1, p1)

        ik2, p2 = KeyPair.generate(), KeyPair.generate()
        i2 = sim.invite(p1, "member", invite_key=ik2)  # member invites a peer
        sim.claim(i2, ik2, p2)
        bad = sim.invite(p1, "admin", invite_key=KeyPair.generate())  # overreach

        state = fold(sim.ledger)
        assert p2.public_hex in state.members
        assert state.valid[bad] is False

        sim.role_revoke(sim.root, p1, "member")
        ik3 = KeyPair.generate()
        dead = sim.invite(p1, "member", invite_key=ik3)
        state = fold(sim.ledger)
        assert state.valid[dead] is False
        assert p2.public_hex in state.members  # existing member unaffected

    def test_concurrent_key_revoke_and_claim_by_that_member(self, sim):
        """Member's key revoked concurrently with the member sponsoring an
        invite claim: the revoke wins over the whole downstream."""
        sim.role_define(sim.root, "member", ["invite:member"])
        ik1, p1 = KeyPair.generate(), KeyPair.generate()
        i1 = sim.invite(sim.root, "member", invite_key=ik1)
        sim.claim(i1, ik1, p1)
        base = sim.ledger.heads()
        sim.revoke_key(sim.root, p1, parents=base)
        ik2, p2 = KeyPair.generate(), KeyPair.generate()
        i2 = sim.invite(p1, "member", invite_key=ik2, parents=base)
        c2 = sim.claim(i2, ik2, p2, parents=[i2])
        sim.checkpoint(sim.root)
        for state in replay_states(sim):
            assert state.valid[i2] is True  # authorized in its own ancestry
            assert p2.public_hex not in state.members  # but the race kills it
            assert not state.holds(p2.public_hex, "invite:member")
        assert fold(sim.ledger).valid[c2] is True


class TestReplicaDivergenceResistance:
    def test_validity_judgement_identical_across_replicas(self, sim):
        """Replicas that saw events in different orders agree on every
        judgement, including the invalid ones (shared convergence)."""
        a, b, mallory = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, a, ["link:publish"], redelegate=True)
        sim.delegate(a, b, ["link:publish"])
        sim.delegate(mallory, b, ["tunnel:serve"])  # unauthorized
        base = sim.ledger.heads()
        sim.revoke_key(sim.root, a, parents=base)
        sim.delegate(a, mallory, ["link:publish"], parents=base)
        sim.checkpoint(sim.root)
        states = replay_states(sim, trials=6, seed=99)
        reference = states[0]
        for state in states[1:]:
            assert state.valid == reference.valid
            assert state.reasons == reference.reasons
