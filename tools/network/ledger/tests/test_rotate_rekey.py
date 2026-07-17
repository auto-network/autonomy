"""Root rotation (continuity chain) and member rekey."""

from __future__ import annotations

from tools.network.idkit import KeyPair
from tools.network.ledger import fold, sign_rotate_continuity
from tools.network.ledger.fold import (
    R_BAD_CONTINUITY,
    R_NOT_ROOT,
    R_REKEY_UNAUTHORIZED,
    R_REKEY_WRONG_KEY,
    R_UNKNOWN_PERSONA,
)

from .conftest import Sim
from .test_l3_safety_merge import replay_states


class TestRotation:
    def test_rotation_moves_root(self, sim):
        new_root = KeyPair.generate()
        sim.rotate(sim.root, new_root)
        a = KeyPair.generate()
        by_new = sim.delegate(new_root, a, ["link:publish"])
        state = fold(sim.ledger)
        assert state.root == new_root.public_hex
        assert state.lineage == (sim.root.public_hex, new_root.public_hex)
        assert state.valid[by_new] is True
        assert state.holds(a.public_hex, "link:publish")

    def test_old_root_loses_authority_after_rotation(self, sim):
        new_root = KeyPair.generate()
        sim.rotate(sim.root, new_root)
        a = KeyPair.generate()
        stale = sim.delegate(sim.root, a, ["link:publish"])  # sees the rotation
        state = fold(sim.ledger)
        assert state.valid[stale] is False
        assert not state.holds(a.public_hex, "link:publish")

    def test_rotation_preserves_earlier_grants(self, sim):
        """Reputation/continuity: grants made while root stay anchored."""
        a = KeyPair.generate()
        sim.delegate(sim.root, a, ["link:publish"])
        new_root = KeyPair.generate()
        sim.rotate(sim.root, new_root)
        state = fold(sim.ledger)
        assert state.holds(a.public_hex, "link:publish")

    def test_rotation_by_non_root_rejected(self, sim):
        mallory, m2 = KeyPair.generate(), KeyPair.generate()
        bad = sim.rotate(mallory, m2)
        state = fold(sim.ledger)
        assert state.valid[bad] is False
        assert state.reasons[bad] == R_NOT_ROOT
        assert state.root == sim.root.public_hex

    def test_continuity_must_be_signed_by_new_key(self, sim):
        new_root, other = KeyPair.generate(), KeyPair.generate()
        forged = sign_rotate_continuity(other, sim.root.public_hex)
        bad = sim.rotate(sim.root, new_root, continuity=forged)
        state = fold(sim.ledger)
        assert state.valid[bad] is False
        assert state.reasons[bad] == R_BAD_CONTINUITY
        assert state.root == sim.root.public_hex

    def test_rotation_beats_concurrent_stolen_root_grants(self, sim):
        """A stolen root key cannot outrun its own rotation: grants it mints
        concurrently with the key.rotate fold to dead in every order."""
        new_root, attacker = KeyPair.generate(), KeyPair.generate()
        base = sim.ledger.heads()
        sim.rotate(sim.root, new_root, parents=base)
        stolen = sim.delegate(sim.root, attacker, ["link:publish"], parents=base)
        sim.checkpoint(new_root)
        for state in replay_states(sim):
            assert state.root == new_root.public_hex
            assert state.valid[stolen] is True  # issuance-valid in its ancestry…
            assert not state.holds(attacker.public_hex, "link:publish")  # …but loses

    def test_chained_rotations(self, sim):
        r2, r3 = KeyPair.generate(), KeyPair.generate()
        sim.rotate(sim.root, r2)
        sim.rotate(r2, r3)
        for state in replay_states(sim):
            assert state.root == r3.public_hex
            assert state.lineage == (
                sim.root.public_hex,
                r2.public_hex,
                r3.public_hex,
            )


class TestRekey:
    def build_member(self):
        sim = Sim()
        sim.role_define(sim.root, "member", ["link:publish"])
        ik, persona = KeyPair.generate(), KeyPair.generate()
        invite = sim.invite(sim.root, "member", invite_key=ik)
        sim.claim(invite, ik, persona)
        return sim, persona

    def test_self_rekey_moves_roles_to_new_key(self):
        sim, persona = self.build_member()
        new_key = KeyPair.generate()
        ok = sim.rekey(persona, persona, persona, new_key)
        for state in replay_states(sim):
            assert state.valid[ok] is True
            member = state.members[persona.public_hex]
            assert member.current_key == new_key.public_hex
            assert member.roles == ("member",)
            assert state.holds(new_key.public_hex, "link:publish")
            assert not state.holds(persona.public_hex, "link:publish")

    def test_root_may_rekey_a_member(self):
        sim, persona = self.build_member()
        new_key = KeyPair.generate()
        ok = sim.rekey(sim.root, persona, persona, new_key)
        assert fold(sim.ledger).valid[ok] is True

    def test_stranger_cannot_rekey(self):
        sim, persona = self.build_member()
        mallory, new_key = KeyPair.generate(), KeyPair.generate()
        bad = sim.rekey(mallory, persona, persona, new_key)
        state = fold(sim.ledger)
        assert state.valid[bad] is False
        assert state.reasons[bad] == R_REKEY_UNAUTHORIZED
        assert state.members[persona.public_hex].current_key == persona.public_hex

    def test_rekey_must_name_current_key(self):
        sim, persona = self.build_member()
        k2, k3 = KeyPair.generate(), KeyPair.generate()
        sim.rekey(persona, persona, persona, k2)
        stale = sim.rekey(persona, persona, persona, k3)  # old_pub is stale now
        state = fold(sim.ledger)
        assert state.valid[stale] is False
        assert state.reasons[stale] == R_REKEY_WRONG_KEY
        assert state.members[persona.public_hex].current_key == k2.public_hex

    def test_rekey_unknown_persona_rejected(self, sim):
        a, b = KeyPair.generate(), KeyPair.generate()
        bad = sim.rekey(a, a, a, b)
        state = fold(sim.ledger)
        assert state.valid[bad] is False
        assert state.reasons[bad] == R_UNKNOWN_PERSONA

    def test_rekey_after_key_compromise_restores_role_scopes(self):
        sim, persona = self.build_member()
        sim.revoke_key(sim.root, persona)  # compromised key killed
        state = fold(sim.ledger)
        assert not state.holds(persona.public_hex, "link:publish")
        new_key = KeyPair.generate()
        ok = sim.rekey(sim.root, persona, persona, new_key)
        state = fold(sim.ledger)
        assert state.valid[ok] is True
        assert state.holds(new_key.public_hex, "link:publish")


class TestCheckpoint:
    def test_root_checkpoint_valid_and_inert(self, sim):
        a = KeyPair.generate()
        sim.delegate(sim.root, a, ["link:publish"])
        before = fold(sim.ledger)
        cp = sim.checkpoint(sim.root)
        after = fold(sim.ledger)
        assert after.valid[cp] is True
        assert after.checkpoints == (cp,)
        assert after.holds(a.public_hex, "link:publish") == before.holds(
            a.public_hex, "link:publish"
        )

    def test_unauthorized_checkpoint_invalid(self, sim):
        mallory = KeyPair.generate()
        bad = sim.checkpoint(mallory)
        state = fold(sim.ledger)
        assert state.valid[bad] is False

    def test_delegated_checkpoint_scope(self, sim):
        a = KeyPair.generate()
        sim.delegate(sim.root, a, ["checkpoint"])
        cp = sim.checkpoint(a)
        assert fold(sim.ledger).valid[cp] is True
