"""Backward key resolution: was this ever this persona's key? (auto-7c7po)

Design of record graph://21a0da9e-1c2, drivers D2/D9. ``persona_for_key``
answers continuity (the past); ``current_key`` answers currency (the
present); ``key_revoked`` answers integrity — three independent facts a
verification boundary needs together.
"""

from __future__ import annotations

from tools.network.idkit import KeyPair

from .conftest import Sim


def member(sim, persona=None):
    """Admit one self-claiming member; returns their persona keypair."""
    persona = persona or KeyPair.generate()
    sim.role_define(sim.root, "member", requires="self")
    invite = sim.invite(sim.root, "member", invite_key=persona)
    sim.claim(invite, persona, persona)
    return persona


def test_a_current_member_key_resolves_to_its_persona():
    sim = Sim()
    persona = member(sim)
    state = sim.fold()
    assert state.persona_for_key(persona.public_hex) == persona.public_hex


def test_keys_retired_by_one_and_by_three_rekeys_still_resolve():
    sim = Sim()
    persona = member(sim)
    keys = [persona]
    for _ in range(3):
        new = KeyPair.generate()
        sim.rekey(keys[-1], persona, keys[-1], new)
        keys.append(new)
    state = sim.fold()
    for retired in keys:
        assert state.persona_for_key(retired.public_hex) == persona.public_hex, (
            "every key in the continuity chain resolves, however many "
            "rekeys retired it"
        )
    assert state.members[persona.public_hex].current_key == keys[-1].public_hex


def test_the_persona_id_resolves_before_and_after_a_rekey():
    sim = Sim()
    persona = member(sim)
    assert sim.fold().persona_for_key(persona.public_hex) == persona.public_hex
    new = KeyPair.generate()
    sim.rekey(persona, persona, persona, new)
    assert sim.fold().persona_for_key(persona.public_hex) == persona.public_hex


def test_two_members_keys_resolve_to_their_own_personas():
    sim = Sim()
    alice = KeyPair.generate()
    bob = KeyPair.generate()
    sim.role_define(sim.root, "member", requires="self")
    for persona in (alice, bob):
        invite = sim.invite(sim.root, "member", invite_key=persona)
        sim.claim(invite, persona, persona)
    alice_new = KeyPair.generate()
    sim.rekey(alice, alice, alice, alice_new)
    state = sim.fold()
    assert state.persona_for_key(alice_new.public_hex) == alice.public_hex
    assert state.persona_for_key(bob.public_hex) == bob.public_hex


def test_an_unknown_key_resolves_to_nothing_and_that_is_not_an_error():
    sim = Sim()
    member(sim)
    assert sim.fold().persona_for_key("ff" * 32) is None


def test_a_race_losing_rekey_still_bound_its_key():
    """fold.py resolves concurrent rekeys by lowest event hash — but the
    loser's continuity signature is a genuine statement that the persona
    controlled that key. Rows signed under it stay valid; only current_key
    follows the winner."""
    sim = Sim()
    persona = member(sim)
    frontier = sim.ledger.heads()
    first = KeyPair.generate()
    second = KeyPair.generate()
    id_a = sim.rekey(persona, persona, persona, first, parents=frontier)
    id_b = sim.rekey(persona, persona, persona, second, parents=frontier)
    state = sim.fold()
    winner, loser = (first, second) if id_a < id_b else (second, first)
    assert state.members[persona.public_hex].current_key == winner.public_hex
    assert state.persona_for_key(winner.public_hex) == persona.public_hex
    assert state.persona_for_key(loser.public_hex) == persona.public_hex, (
        "the losing branch still bound the key to the persona"
    )


def test_a_rekey_racing_its_own_keys_revocation_never_bound_the_new_key():
    """A self-authorized rekey concurrent with the revocation of its
    authorizing key loses (_rekey_alive): a revoked key cannot outrun
    revocation by rekeying, so the binding never held."""
    sim = Sim()
    persona = member(sim)
    frontier = sim.ledger.heads()
    escaped = KeyPair.generate()
    sim.rekey(persona, persona, persona, escaped, parents=frontier)
    sim.revoke_key(sim.root, persona, parents=frontier)
    state = sim.fold()
    assert state.persona_for_key(escaped.public_hex) is None
    assert state.persona_for_key(persona.public_hex) == persona.public_hex


def test_a_revoked_key_reports_revoked_and_still_resolves():
    """Two independent facts: what the key signed stays attributable, and
    the key itself signs nothing new. A boundary needs both."""
    sim = Sim()
    persona = member(sim)
    new = KeyPair.generate()
    sim.rekey(persona, persona, persona, new)
    sim.revoke_key(sim.root, persona)
    state = sim.fold()
    assert state.key_revoked(persona.public_hex) is True
    assert state.persona_for_key(persona.public_hex) == persona.public_hex
    assert state.key_revoked(new.public_hex) is False
    assert state.persona_for_key(new.public_hex) == persona.public_hex


def test_the_fold_remains_free_of_wall_clock_reads():
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1].joinpath("fold.py").read_text()
    assert not re.search(r"time\.time\(|datetime\.(utc)?now", src)
