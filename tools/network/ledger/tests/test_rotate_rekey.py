"""Root rotation (continuity chain) and member rekey."""

from __future__ import annotations

from tools.network.idkit import KeyPair
from tools.network.ledger import (
    fold,
    sign_rotate_continuity,
    sign_rekey_continuity,
    sign_rotate_recovery,
)
from tools.network.ledger.fold import (
    R_BAD_CONTINUITY,
    R_BAD_RECOVERY_CONTINUITY,
    R_NOT_ROOT,
    R_RECOVERY_CONTINUITY_MISSING,
    R_RECOVERY_NOT_DECLARED,
    R_REKEY_REVOKED_KEY,
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

    def test_rekey_continuity_is_new_key_and_domain_bound(self):
        # The NEW key must sign the rekey binding over member.rekey's OWN
        # domain (persona-bound). So neither a wrong signer NOR a valid
        # key.rotate proof (right new key, wrong domain) is accepted — no
        # persona strands on an uncontrolled key, and no cross-event-type
        # continuity replay is reachable (x97iz).
        sim, persona = self.build_member()
        new_key, other = KeyPair.generate(), KeyPair.generate()
        wrong_signer = sign_rekey_continuity(other, persona.public_hex, persona.public_hex)
        bad_a = sim.rekey(persona, persona, persona, new_key, continuity=wrong_signer)
        rotate_style = sign_rotate_continuity(new_key, persona.public_hex)
        bad_b = sim.rekey(persona, persona, persona, new_key, continuity=rotate_style)
        state = fold(sim.ledger)
        assert state.reasons[bad_a] == R_BAD_CONTINUITY
        assert state.reasons[bad_b] == R_BAD_CONTINUITY
        assert state.members[persona.public_hex].current_key == persona.public_hex

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

    def test_revoked_key_cannot_self_rekey_out_of_revocation(self):
        """Codex-found hole (auto-16cjl validation): a revoked key must not
        escape revocation by rekeying itself to a fresh key. Exact repro:
        define role -> invite -> claim -> revoke_key -> self-rekey."""
        sim, persona = self.build_member()
        sim.revoke_key(sim.root, persona)
        new_key = KeyPair.generate()
        escape = sim.rekey(persona, persona, persona, new_key)  # signed by revoked key
        for state in replay_states(sim):
            assert state.valid[escape] is False
            assert state.reasons[escape] == R_REKEY_REVOKED_KEY
            member = state.members[persona.public_hex]
            assert member.current_key == persona.public_hex  # binding unchanged
            assert not state.holds(new_key.public_hex, "link:publish")
            assert not state.holds(persona.public_hex, "link:publish")

    def test_self_rekey_racing_key_revoke_loses(self):
        """The concurrent variant: revoke wins the race in every replay
        order — a revoked key cannot outrun revocation on a fork either."""
        sim, persona = self.build_member()
        new_key = KeyPair.generate()
        base = sim.ledger.heads()
        sim.revoke_key(sim.root, persona, parents=base)
        escape = sim.rekey(persona, persona, persona, new_key, parents=base)
        sim.checkpoint(sim.root)
        for state in replay_states(sim):
            assert state.valid[escape] is True  # issuance-valid in its ancestry…
            member = state.members[persona.public_hex]
            assert member.current_key == persona.public_hex  # …but the revoke wins
            assert not state.holds(new_key.public_hex, "link:publish")
            assert not state.holds(persona.public_hex, "link:publish")

    def test_revoking_stale_old_key_after_rekey_is_noop(self):
        """Cleanup-revoking the abandoned old key must not undo a rekey
        that causally preceded it."""
        sim, persona = self.build_member()
        new_key = KeyPair.generate()
        sim.rekey(persona, persona, persona, new_key)
        sim.revoke_key(sim.root, persona)  # causally after the rekey
        for state in replay_states(sim):
            member = state.members[persona.public_hex]
            assert member.current_key == new_key.public_hex
            assert state.holds(new_key.public_hex, "link:publish")

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


class TestRecoveryFactorRotation:
    """auto-n9cy3: an in-possession key.rotate needs the declared recovery
    factor's co-signature, so a stolen root alone cannot rotate the org."""

    def test_recovery_key_policy_admits_dual_signed_rotation(self):
        rk = KeyPair.generate()
        sim = Sim(recovery_key=rk)
        new_root = KeyPair.generate()
        ok = sim.rotate(sim.root, new_root, recovery_key=rk)
        assert fold(sim.ledger).valid[ok] is True

    def test_recovery_key_policy_refuses_root_only_rotation(self):
        rk = KeyPair.generate()
        sim = Sim(recovery_key=rk)
        bad = sim.rotate(sim.root, KeyPair.generate())  # no co-signature
        state = fold(sim.ledger)
        assert state.valid[bad] is False
        assert state.reasons[bad] == R_RECOVERY_CONTINUITY_MISSING

    def test_recovery_key_policy_refuses_wrong_recovery_signer(self):
        rk, mallory = KeyPair.generate(), KeyPair.generate()
        sim = Sim(recovery_key=rk)
        bad = sim.rotate(sim.root, KeyPair.generate(), recovery_key=mallory)
        state = fold(sim.ledger)
        assert state.valid[bad] is False
        assert state.reasons[bad] == R_BAD_RECOVERY_CONTINUITY

    def test_recovery_co_signature_binds_the_exact_transition(self):
        # A recovery co-signature minted for one {old,new} authorises no other:
        # replaying it onto a different new_pub is refused.
        rk = KeyPair.generate()
        sim = Sim(recovery_key=rk)
        intended, attacker = KeyPair.generate(), KeyPair.generate()
        replayed = sign_rotate_recovery(
            rk, sim.genesis_id, sim.root.public_hex, intended.public_hex
        )
        bad = sim.rotate(sim.root, attacker, recovery_continuity=replayed)
        state = fold(sim.ledger)
        assert state.valid[bad] is False
        assert state.reasons[bad] == R_BAD_RECOVERY_CONTINUITY

    def test_recovery_co_signature_is_genesis_bound(self):
        # A co-signature minted under ANOTHER genesis is rejected here, even for
        # the same recovery key and the same {old_pub,new_pub} -- so a recovery
        # co-sig can never be replayed across orgs by construction, not merely
        # because old_pub is org-unique today.
        rk = KeyPair.generate()
        sim = Sim(recovery_key=rk)
        new_root = KeyPair.generate()
        foreign = sign_rotate_recovery(
            rk, "f" * 64, sim.root.public_hex, new_root.public_hex
        )
        bad = sim.rotate(sim.root, new_root, recovery_continuity=foreign)
        state = fold(sim.ledger)
        assert state.valid[bad] is False
        assert state.reasons[bad] == R_BAD_RECOVERY_CONTINUITY

    def test_none_policy_refuses_an_unvalidatable_co_signature(self):
        # Under policy "none" a recovery co-signature the ledger cannot validate
        # against a declared factor is refused, never ignored.
        sim = Sim()
        bad = sim.rotate(sim.root, KeyPair.generate(), recovery_key=KeyPair.generate())
        state = fold(sim.ledger)
        assert state.valid[bad] is False
        assert state.reasons[bad] == R_RECOVERY_NOT_DECLARED

    def test_none_policy_admits_a_plain_rotation(self):
        sim = Sim()  # no recovery declared
        ok = sim.rotate(sim.root, KeyPair.generate())
        assert fold(sim.ledger).valid[ok] is True
