"""Member recovery — the third door on member.rekey (auto-c3yl1, items 1 & 3).

A stolen persona is moved off by its owner's enrolled recovery key, without the
owner ever holding the current (stolen) key.
"""

from __future__ import annotations

from tools.network.idkit import KeyPair
from tools.network.ledger.events import sign_rekey_recovery
from tools.network.ledger.fold import (
    fold,
    R_REKEY_UNAUTHORIZED,
    R_RECOVERY_NOT_ENROLLED,
    R_RECOVERY_SIG_BAD,
    R_RECOVERY_EQUALS_ROOT,
)
from .conftest import Sim, key


def _enrolled_member(sim, recovery_pub):
    sim.role_define(sim.root, "member", ["link:publish"])
    ik, persona = KeyPair.generate(), KeyPair.generate()
    invite = sim.invite(sim.root, "member", invite_key=ik)
    sim.claim(invite, ik, persona,
              recovery={"policy": "recovery-key", "recovery_pub": recovery_pub})
    return persona


def test_recovery_key_moves_a_stolen_persona():
    sim = Sim()
    recovery = KeyPair.generate()
    persona = _enrolled_member(sim, recovery.public_hex)
    # The attacker holds `persona` (the current key). The owner recovers with the
    # recovery key + a fresh new key, never touching the stolen current key.
    new_key = KeyPair.generate()
    sig = sign_rekey_recovery(recovery, sim.genesis_id, key(persona), key(persona), key(new_key))
    ev = sim.rekey(new_key, persona, persona, new_key, recovery_sig=sig)
    state = fold(sim.ledger)
    assert state.valid[ev] is True
    assert state.members[key(persona)].current_key == key(new_key)
    assert state.holds(key(new_key), "link:publish")


def test_no_recovery_sig_is_plain_unauthorized():
    sim = Sim()
    recovery = KeyPair.generate()
    persona = _enrolled_member(sim, recovery.public_hex)
    stranger, new_key = KeyPair.generate(), KeyPair.generate()
    ev = sim.rekey(stranger, persona, persona, new_key)  # no recovery_sig
    state = fold(sim.ledger)
    assert state.valid[ev] is False and state.reasons[ev] == R_REKEY_UNAUTHORIZED


def test_recovery_refused_when_not_enrolled():
    sim = Sim()
    # policy "none": no recovery enrolled.
    sim.role_define(sim.root, "member", ["link:publish"])
    ik, persona = KeyPair.generate(), KeyPair.generate()
    invite = sim.invite(sim.root, "member", invite_key=ik)
    sim.claim(invite, ik, persona)  # no recovery
    recovery, new_key = KeyPair.generate(), KeyPair.generate()
    sig = sign_rekey_recovery(recovery, sim.genesis_id, key(persona), key(persona), key(new_key))
    ev = sim.rekey(new_key, persona, persona, new_key, recovery_sig=sig)
    state = fold(sim.ledger)
    # Refused, never ignored — an attacker cannot omit enrollment to soften the path.
    assert state.valid[ev] is False and state.reasons[ev] == R_RECOVERY_NOT_ENROLLED


def test_wrong_recovery_key_is_refused():
    sim = Sim()
    recovery = KeyPair.generate()
    persona = _enrolled_member(sim, recovery.public_hex)
    attacker, new_key = KeyPair.generate(), KeyPair.generate()
    # The attacker signs with THEIR key, not the enrolled recovery key.
    sig = sign_rekey_recovery(attacker, sim.genesis_id, key(persona), key(persona), key(new_key))
    ev = sim.rekey(new_key, persona, persona, new_key, recovery_sig=sig)
    state = fold(sim.ledger)
    assert state.valid[ev] is False and state.reasons[ev] == R_RECOVERY_SIG_BAD


def test_recovery_sig_is_genesis_bound():
    sim = Sim()
    recovery = KeyPair.generate()
    persona = _enrolled_member(sim, recovery.public_hex)
    new_key = KeyPair.generate()
    # A recovery signature minted under a DIFFERENT genesis must not verify here.
    sig = sign_rekey_recovery(recovery, "ff" * 32, key(persona), key(persona), key(new_key))
    ev = sim.rekey(new_key, persona, persona, new_key, recovery_sig=sig)
    state = fold(sim.ledger)
    assert state.valid[ev] is False and state.reasons[ev] == R_RECOVERY_SIG_BAD


def test_recovery_pub_equal_to_persona_is_refused_at_claim():
    import pytest
    from tools.network.ledger.errors import SchemaError
    sim = Sim()
    sim.role_define(sim.root, "member", ["link:publish"])
    ik, persona = KeyPair.generate(), KeyPair.generate()
    invite = sim.invite(sim.root, "member", invite_key=ik)
    with pytest.raises(SchemaError):
        sim.claim(invite, ik, persona,
                  recovery={"policy": "recovery-key", "recovery_pub": key(persona)})


def test_recovery_pub_equal_to_root_is_refused_in_fold():
    sim = Sim()
    sim.role_define(sim.root, "member", ["link:publish"])
    ik, persona = KeyPair.generate(), KeyPair.generate()
    invite = sim.invite(sim.root, "member", invite_key=ik)
    ev = sim.claim(invite, ik, persona,
                   recovery={"policy": "recovery-key", "recovery_pub": sim.root.public_hex})
    state = fold(sim.ledger)
    assert state.valid[ev] is False and state.reasons[ev] == R_RECOVERY_EQUALS_ROOT


# -- item 4: a recovery rekey revokes the key it leaves (the concurrent race) --

from .test_l3_safety_merge import replay_states


def test_recovery_beats_a_concurrent_attacker_rekey_in_every_order():
    """The attacker still holds the stolen current key and forks a self-rekey
    onto their own key, concurrent with the owner's recovery rekey. The recovery
    must win in EVERY replay order — the attacker's self-authorized rekey loses
    to the recovery's implicit revocation of old_pub (the _rotation_race
    analogue), not to a hash-order coin flip."""
    sim = Sim()
    recovery = KeyPair.generate()
    persona = _enrolled_member(sim, recovery.public_hex)

    base = sim.ledger.heads()
    owner_key, attacker_key = KeyPair.generate(), KeyPair.generate()
    sig = sign_rekey_recovery(recovery, sim.genesis_id, key(persona), key(persona), key(owner_key))
    # Owner's recovery rekey and attacker's self-rekey, both forked off `base`.
    recover = sim.rekey(owner_key, persona, persona, owner_key, recovery_sig=sig, parents=base)
    steal = sim.rekey(persona, persona, persona, attacker_key, parents=base)
    sim.checkpoint(sim.root)

    for state in replay_states(sim):
        member = state.members[key(persona)]
        assert member.current_key == key(owner_key), "owner recovery must win the race"
        assert member.current_key != key(attacker_key)
        assert state.holds(key(owner_key), "link:publish")
        assert not state.holds(key(attacker_key), "link:publish")
        # The recovery rekey itself is valid; the attacker's is issuance-valid in
        # its own ancestry but loses at projection.
        assert state.valid[recover] is True


def test_attacker_cannot_rekey_again_from_the_revoked_stolen_key():
    """After recovery lands, the attacker still holds old_pub but it is revoked,
    so a fresh self-rekey from it is refused."""
    sim = Sim()
    recovery = KeyPair.generate()
    persona = _enrolled_member(sim, recovery.public_hex)
    owner_key = KeyPair.generate()
    sig = sign_rekey_recovery(recovery, sim.genesis_id, key(persona), key(persona), key(owner_key))
    sim.rekey(owner_key, persona, persona, owner_key, recovery_sig=sig)
    # old_pub (persona) is now current-superseded AND revoked; a rekey naming it
    # as old is refused (not current) regardless, and it is revoked besides.
    attacker_key = KeyPair.generate()
    again = sim.rekey(persona, persona, persona, attacker_key)
    state = fold(sim.ledger)
    assert state.valid[again] is False
    assert state.members[key(persona)].current_key == key(owner_key)
