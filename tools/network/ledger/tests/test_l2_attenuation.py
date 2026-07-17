"""L2 — attenuation-only at every hop, invites and roles included.

Overreaching events carry *valid signatures*; the fold is what must judge
them invalid ("rejected at fold time").
"""

from __future__ import annotations

from tools.network.idkit import KeyPair
from tools.network.ledger import fold
from tools.network.ledger.fold import (
    R_INVITE_OVERREACH,
    R_NOT_REDELEGABLE,
    R_ROLE_DEFINE_OVERREACH,
    R_ROLE_GRANT_UNAUTHORIZED,
    R_SCOPE_ESCALATION,
)

from .conftest import Sim


class TestDelegationAttenuation:
    def test_child_cannot_exceed_parent_scope(self, sim):
        a, b = KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, a, ["link:publish"], redelegate=True)
        bad = sim.delegate(a, b, ["link:publish", "tunnel:serve"])
        state = sim.fold()
        assert state.valid[bad] is False
        assert state.reasons[bad] == R_SCOPE_ESCALATION
        assert not state.holds(b.public_hex, "tunnel:serve")
        assert not state.holds(b.public_hex, "link:publish")  # whole event inert

    def test_unrelated_key_cannot_delegate_at_all(self, sim):
        stranger, b = KeyPair.generate(), KeyPair.generate()
        bad = sim.delegate(stranger, b, ["link:publish"])
        state = sim.fold()
        assert state.valid[bad] is False
        assert state.reasons[bad] == R_SCOPE_ESCALATION

    def test_redelegation_requires_flag(self, sim):
        a, b = KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, a, ["link:publish"], redelegate=False)
        bad = sim.delegate(a, b, ["link:publish"])
        state = sim.fold()
        assert state.valid[bad] is False
        assert state.reasons[bad] == R_NOT_REDELEGABLE

    def test_redelegation_chain_narrows(self, sim):
        a, b, c = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, a, ["link:publish", "link:revoke"], redelegate=True)
        sim.delegate(a, b, ["link:publish"], redelegate=True)
        ok = sim.delegate(b, c, ["link:publish"])
        wide = sim.delegate(b, c, ["link:revoke"])  # b no longer holds this
        state = sim.fold()
        assert state.valid[ok] is True
        assert state.valid[wide] is False
        assert state.holds(c.public_hex, "link:publish")
        assert not state.holds(c.public_hex, "link:revoke")

    def test_wildcard_pattern_attenuation(self, sim):
        a, b = KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, a, ["role:grant:*"], redelegate=True)
        narrower = sim.delegate(a, b, ["role:grant:member"])
        state = sim.fold()
        assert state.valid[narrower] is True
        assert state.holds(b.public_hex, "role:grant:member")
        assert not state.holds(b.public_hex, "role:define")

    def test_wildcard_cannot_be_minted_from_narrow(self, sim):
        a, b = KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, a, ["role:grant:member"], redelegate=True)
        bad = sim.delegate(a, b, ["role:grant:*"])
        state = sim.fold()
        assert state.valid[bad] is False
        assert state.reasons[bad] == R_SCOPE_ESCALATION

    def test_self_delegation_cannot_escalate(self, sim):
        a = KeyPair.generate()
        sim.delegate(sim.root, a, ["link:publish"], redelegate=True)
        bad = sim.delegate(a, a, ["link:publish", "tunnel:serve"], redelegate=True)
        state = sim.fold()
        assert state.valid[bad] is False
        assert not state.holds(a.public_hex, "tunnel:serve")

    def test_mutual_delegation_cycle_cannot_bootstrap(self, sim):
        a, b = KeyPair.generate(), KeyPair.generate()
        g1 = sim.delegate(a, b, ["link:publish"], redelegate=True)
        g2 = sim.delegate(b, a, ["link:publish"], redelegate=True)
        state = sim.fold()
        assert state.valid[g1] is False and state.valid[g2] is False
        assert not state.holds(a.public_hex, "link:publish")
        assert not state.holds(b.public_hex, "link:publish")


class TestInviteScoping:
    """'Can invite into role X' is itself a scope — enforced at fold time."""

    def setup_sim(self):
        sim = Sim()
        sim.role_define(sim.root, "member", ["link:publish"])
        sim.role_define(sim.root, "admin", ["link:publish", "role:grant:member"])
        return sim

    def test_scoped_invite_accepted(self):
        sim = self.setup_sim()
        sponsor, ik = KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, sponsor, ["invite:member"])
        ok = sim.invite(sponsor, "member", invite_key=ik)
        assert sim.fold().valid[ok] is True

    def test_overreaching_invite_rejected(self):
        sim = self.setup_sim()
        sponsor, ik = KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, sponsor, ["invite:member"])
        bad = sim.invite(sponsor, "admin", invite_key=ik)
        state = sim.fold()
        assert state.valid[bad] is False
        assert state.reasons[bad] == R_INVITE_OVERREACH

    def test_overreaching_claim_never_creates_member(self):
        sim = self.setup_sim()
        sponsor, ik, persona = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, sponsor, ["invite:member"])
        bad_invite = sim.invite(sponsor, "admin", invite_key=ik)
        claim = sim.claim(bad_invite, ik, persona)
        state = sim.fold()
        assert state.valid[claim] is False
        assert persona.public_hex not in state.members
        assert not state.holds(persona.public_hex, "role:grant:member")

    def test_invite_wildcard_scope(self):
        sim = self.setup_sim()
        sponsor, ik = KeyPair.generate(), KeyPair.generate()
        sim.delegate(sim.root, sponsor, ["invite:*"])
        ok = sim.invite(sponsor, "admin", invite_key=ik)
        assert sim.fold().valid[ok] is True


class TestRoleAttenuation:
    def test_role_define_scope_set_needs_delegable_coverage(self, sim):
        definer = KeyPair.generate()
        sim.delegate(sim.root, definer, ["link:publish", "role:define"], redelegate=True)
        ok = sim.role_define(definer, "publisher", ["link:publish"])
        bad = sim.role_define(definer, "super", ["tunnel:serve"])
        state = sim.fold()
        assert state.valid[ok] is True
        assert state.valid[bad] is False
        assert state.reasons[bad] == R_ROLE_DEFINE_OVERREACH
        assert "super" not in state.role_defs

    def test_role_grant_requires_scope(self, sim):
        sim.role_define(sim.root, "member", ["link:publish"])
        granter, persona = KeyPair.generate(), KeyPair.generate()
        bad = sim.role_grant(granter, persona, "member")
        sim.delegate(sim.root, granter, ["role:grant:member"])
        ok = sim.role_grant(granter, persona, "member")
        state = sim.fold()
        assert state.valid[bad] is False
        assert state.reasons[bad] == R_ROLE_GRANT_UNAUTHORIZED
        assert state.valid[ok] is True
        assert state.roles(persona.public_hex) == ("member",)

    def test_role_scopes_are_not_delegable(self, sim):
        """Scopes held via a role cannot be re-delegated onward."""
        sim.role_define(sim.root, "member", ["link:publish"])
        persona, other = KeyPair.generate(), KeyPair.generate()
        sim.role_grant(sim.root, persona, "member")
        bad = sim.delegate(persona, other, ["link:publish"])
        state = sim.fold()
        assert state.holds(persona.public_hex, "link:publish")
        assert state.valid[bad] is False
        assert state.reasons[bad] == R_NOT_REDELEGABLE
        assert not state.holds(other.public_hex, "link:publish")
