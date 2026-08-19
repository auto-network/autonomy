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
