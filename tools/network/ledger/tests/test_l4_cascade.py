"""L4 — cascade: no authority survives without a live path to root.

Revoking a delegation silently de-authorizes every descendant lacking
another live path; explicit re-grants restore; facts (memberships) do not
cascade — they die only by explicit revocation or by losing a race.
"""

from __future__ import annotations

from tools.network.idkit import KeyPair
from tools.network.ledger import fold

from .conftest import Sim
from .test_l3_safety_merge import replay_states


class TestCascade:
    def build_chain(self):
        sim = Sim()
        a, b, c = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
        g_a = sim.delegate(sim.root, a, ["link:publish", "link:revoke"], redelegate=True)
        g_b = sim.delegate(a, b, ["link:publish"], redelegate=True)
        g_c = sim.delegate(b, c, ["link:publish"])
        return sim, (a, b, c), (g_a, g_b, g_c)

    def test_revoking_root_grant_kills_whole_subtree(self):
        sim, (a, b, c), (g_a, _, _) = self.build_chain()
        sim.revoke_event(sim.root, g_a)
        for state in replay_states(sim):
            for kp in (a, b, c):
                assert not state.holds(kp.public_hex, "link:publish")

    def test_revoking_middle_grant_kills_descendants_only(self):
        sim, (a, b, c), (_, g_b, _) = self.build_chain()
        sim.revoke_event(sim.root, g_b)
        for state in replay_states(sim):
            assert state.holds(a.public_hex, "link:publish")
            assert not state.holds(b.public_hex, "link:publish")
            assert not state.holds(c.public_hex, "link:publish")

    def test_explicit_regrant_restores_subtree(self):
        sim, (a, b, c), (g_a, _, _) = self.build_chain()
        sim.revoke_event(sim.root, g_a)
        sim.delegate(sim.root, a, ["link:publish", "link:revoke"], redelegate=True)
        for state in replay_states(sim):
            # The re-grant restores a's authority, and with it the live path
            # supporting the (never-revoked) grants below.
            assert state.holds(a.public_hex, "link:publish")
            assert state.holds(b.public_hex, "link:publish")
            assert state.holds(c.public_hex, "link:publish")

    def test_narrower_regrant_restores_only_covered_scopes(self):
        sim, (a, b, c), (g_a, _, _) = self.build_chain()
        sim.revoke_event(sim.root, g_a)
        sim.delegate(sim.root, a, ["link:revoke"], redelegate=True)
        for state in replay_states(sim):
            assert state.holds(a.public_hex, "link:revoke")
            assert not state.holds(a.public_hex, "link:publish")
            assert not state.holds(b.public_hex, "link:publish")  # support too narrow
            assert not state.holds(c.public_hex, "link:publish")

    def test_multiple_paths_to_root_survival(self):
        """c holds via two sponsors; killing one path leaves c live."""
        sim = Sim()
        a, b, c = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, a, ["link:publish"], redelegate=True)
        g_b = sim.delegate(sim.root, b, ["link:publish"], redelegate=True)
        sim.delegate(a, c, ["link:publish"])
        sim.delegate(b, c, ["link:publish"])
        sim.revoke_key(sim.root, a)
        for state in replay_states(sim):
            assert not state.holds(a.public_hex, "link:publish")
            assert state.holds(c.public_hex, "link:publish")  # b's path survives
        sim.revoke_event(sim.root, g_b)
        for state in replay_states(sim):
            assert not state.holds(c.public_hex, "link:publish")  # last path gone

    def test_key_revoke_kills_old_grants_but_not_later_regrant(self):
        sim = Sim()
        a = KeyPair.generate()
        sim.delegate(sim.root, a, ["link:publish"])
        sim.delegate(sim.root, a, ["tunnel:serve"])
        sim.revoke_key(sim.root, a)
        sim.delegate(sim.root, a, ["link:revoke"])  # causally after the revoke
        for state in replay_states(sim):
            assert not state.holds(a.public_hex, "link:publish")
            assert not state.holds(a.public_hex, "tunnel:serve")
            assert state.holds(a.public_hex, "link:revoke")

    def test_revoke_authority_follows_provenance(self):
        """Only root, the granter, an upstream sponsor, or the grantee itself
        may revoke a delegation — a bystander cannot."""
        sim = Sim()
        a, b, stranger = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, a, ["link:publish"], redelegate=True)
        g_b = sim.delegate(a, b, ["link:publish"])
        sim.delegate(sim.root, stranger, ["tunnel:serve"])  # authorized, but unrelated
        bad = sim.revoke_event(stranger, g_b)
        ok_self = sim.revoke_event(b, g_b)  # grantee renounces
        state = fold(sim.ledger)
        assert state.valid[bad] is False
        assert state.valid[ok_self] is True
        assert not state.holds(b.public_hex, "link:publish")

    def test_upstream_sponsor_can_revoke_descendants(self):
        sim = Sim()
        a, b, c = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, a, ["link:publish"], redelegate=True)
        sim.delegate(a, b, ["link:publish"], redelegate=True)
        g_c = sim.delegate(b, c, ["link:publish"])
        rev = sim.revoke_event(a, g_c)  # a is upstream of b, so may revoke b's grant
        for state in replay_states(sim):
            assert state.valid[rev] is True
            assert not state.holds(c.public_hex, "link:publish")

    def test_membership_does_not_cascade_with_sponsor(self):
        """Claimed members survive later sponsor demotion (facts persist);
        the sponsorship trail is provenance, not a liveness dependency."""
        sim = Sim()
        sim.role_define(sim.root, "member", ["link:publish"])
        sponsor, ik, persona = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
        g_s = sim.delegate(sim.root, sponsor, ["invite:member"])
        invite = sim.invite(sponsor, "member", invite_key=ik)
        sim.claim(invite, ik, persona)
        sim.revoke_event(sim.root, g_s)  # demote sponsor AFTER the claim
        for state in replay_states(sim):
            assert persona.public_hex in state.members
            assert state.roles(persona.public_hex) == ("member",)
            assert state.holds(persona.public_hex, "link:publish")
