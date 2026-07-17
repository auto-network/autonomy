"""L3 — safety-biased merge: concurrent revoke beats grant; hash tiebreak.

Branches are constructed with explicit parents so the revoke and the grant
are causally concurrent, then every assertion is re-checked across shuffled
replay orders into fresh replicas.
"""

from __future__ import annotations

import random

from tools.network.idkit import KeyPair
from tools.network.ledger import Ledger, fold

from .conftest import Sim


def replay_states(sim, trials=4, seed=13):
    """The folded state from the builder ledger plus shuffled replicas."""
    events = list(sim.ledger.events())
    rng = random.Random(seed)
    states = [fold(sim.ledger)]
    for _ in range(trials):
        shuffled = events[:]
        rng.shuffle(shuffled)
        replica = Ledger()
        replica.ingest(shuffled)
        states.append(fold(replica))
    fingerprints = {s.fingerprint() for s in states}
    assert len(fingerprints) == 1, "replay orders diverged"
    return states


class TestConcurrentRevokeBeatsGrant:
    def build(self):
        """root -> alice; then concurrently: root revokes alice's key while
        alice delegates to bob."""
        sim = Sim()
        alice, bob = KeyPair.generate(), KeyPair.generate()
        g_alice = sim.delegate(sim.root, alice, ["link:publish"], redelegate=True)
        base = sim.ledger.heads()
        revoke = sim.revoke_key(sim.root, alice, parents=base)
        g_bob = sim.delegate(alice, bob, ["link:publish"], parents=base)
        sim.checkpoint(sim.root)  # merge both branches
        return sim, alice, bob, g_alice, revoke, g_bob

    def test_folds_to_revoked_in_all_replay_orders(self):
        sim, alice, bob, _, _, g_bob = self.build()
        for state in replay_states(sim):
            assert state.valid[g_bob] is True  # issuance-valid in its ancestry…
            assert not state.holds(alice.public_hex, "link:publish")
            assert not state.holds(bob.public_hex, "link:publish")  # …but folds dead

    def test_race_kill_is_permanent(self):
        """Re-granting alice later does NOT revive the racing grant to bob:
        a grant that lost a revoke race is permanently void."""
        sim, alice, bob, _, _, _ = self.build()
        sim.delegate(sim.root, alice, ["link:publish"], redelegate=True)
        for state in replay_states(sim):
            assert state.holds(alice.public_hex, "link:publish")  # explicit re-grant
            assert not state.holds(bob.public_hex, "link:publish")  # race loss sticks

    def test_grant_after_revoke_in_causal_order_is_simply_invalid(self):
        sim = Sim()
        alice, bob = KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, alice, ["link:publish"], redelegate=True)
        sim.revoke_key(sim.root, alice)
        late = sim.delegate(alice, bob, ["link:publish"])  # sees the revoke
        for state in replay_states(sim):
            assert state.valid[late] is False
            assert not state.holds(bob.public_hex, "link:publish")


class TestConcurrentRoleRevoke:
    def test_role_revoke_beats_concurrent_grant(self):
        sim = Sim()
        sim.role_define(sim.root, "member", ["link:publish"])
        granter, persona = KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, granter, ["role:grant:member"])
        base = sim.ledger.heads()
        sim.role_grant(granter, persona, "member", parents=base)
        sim.role_revoke(sim.root, persona, "member", parents=base)
        sim.checkpoint(sim.root)
        for state in replay_states(sim):
            assert state.roles(persona.public_hex) == ()
            assert not state.holds(persona.public_hex, "link:publish")

    def test_regrant_after_revoke_survives(self):
        sim = Sim()
        sim.role_define(sim.root, "member", ["link:publish"])
        persona = KeyPair.generate()
        sim.role_grant(sim.root, persona, "member")
        sim.role_revoke(sim.root, persona, "member")
        sim.role_grant(sim.root, persona, "member")  # causally after the revoke
        for state in replay_states(sim):
            assert state.roles(persona.public_hex) == ("member",)
            assert state.holds(persona.public_hex, "link:publish")


class TestHashTiebreak:
    def test_concurrent_role_defines_pick_one_winner(self):
        sim = Sim()
        base = sim.ledger.heads()
        d1 = sim.role_define(sim.root, "member", ["link:publish"], version=2, parents=base)
        d2 = sim.role_define(sim.root, "member", ["link:revoke"], version=2, parents=base)
        sim.checkpoint(sim.root)
        expected_winner = min(d1, d2)  # equal version: lower event hash wins
        for state in replay_states(sim):
            assert state.role_defs["member"].event_id == expected_winner

    def test_higher_version_beats_hash(self):
        sim = Sim()
        base = sim.ledger.heads()
        sim.role_define(sim.root, "member", ["link:publish"], version=1, parents=base)
        d2 = sim.role_define(sim.root, "member", ["link:revoke"], version=3, parents=base)
        sim.checkpoint(sim.root)
        for state in replay_states(sim):
            assert state.role_defs["member"].event_id == d2
            assert state.role_defs["member"].version == 3

    def test_concurrent_claims_single_deterministic_winner(self):
        sim = Sim()
        sim.role_define(sim.root, "member", [])
        ik = KeyPair.generate()
        p1, p2 = KeyPair.generate(), KeyPair.generate()
        invite = sim.invite(sim.root, "member", invite_key=ik)
        base = sim.ledger.heads()
        c1 = sim.claim(invite, ik, p1, parents=base)
        c2 = sim.claim(invite, ik, p2, parents=base)
        sim.checkpoint(sim.root)
        winner = min(c1, c2)
        winner_persona = p1 if winner == c1 else p2
        loser_persona = p2 if winner == c1 else p1
        for state in replay_states(sim):
            assert set(state.members) == {winner_persona.public_hex}
            assert state.members[winner_persona.public_hex].claim_id == winner
            assert loser_persona.public_hex not in state.members

    def test_concurrent_rotations_single_deterministic_winner(self):
        sim = Sim()
        n1, n2 = KeyPair.generate(), KeyPair.generate()
        base = sim.ledger.heads()
        r1 = sim.rotate(sim.root, n1, parents=base)
        r2 = sim.rotate(sim.root, n2, parents=base)
        sim.checkpoint(sim.root, parents=[r1, r2])
        states = replay_states(sim)
        winner_new = n1.public_hex if min(r1, r2) == r1 else n2.public_hex
        for state in states:
            assert state.root == winner_new
            assert state.lineage[-1] == winner_new
