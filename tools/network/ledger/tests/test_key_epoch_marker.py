"""The frontier-advancing marker, and the silent failure it exists to stop.

A re-keyed credential that cites the SAME authority frontier as the one it
replaces does not supersede it. Neither strictly descends the other, so both
stay current and the winner is the greater ``kem_key_id`` -- a hash, with no
notion of recency. Against a fixed incumbent that happens to hash high, the
re-key loses EVERY time, and every new grant keeps flowing to the credential
a removed machine still holds. The re-key appears to succeed and achieves
nothing (F-001).

Contraction writes a ledger event and escapes this by itself. Disenrollment
and opportunistic refresh write nothing, so they need a marker.
"""

from __future__ import annotations

from tools.network.dag_tag import AUTHORITY, tag_dag

import pytest

from tools.network.idkit import KeyPair
from tools.network.ledger import fold
from tools.network.ledger.fold import R_KEY_EPOCH_UNAUTHORIZED, R_UNKNOWN_PERSONA
from tools.network.storagekit import credentials

from .conftest import Sim


def member_org():
    sim = Sim()
    sim.role_define(sim.root, "member", ["link:publish"])
    persona = KeyPair.generate()
    invite_key = KeyPair.generate()
    sim.claim(sim.invite(sim.root, "member", invite_key=invite_key), invite_key, persona)
    return sim, persona


def heads(sim):
    return tuple(sorted(sim.ledger.heads()))


@tag_dag(AUTHORITY)
def ancestry(sim):
    return lambda hs: set(sim.ledger.ancestry(hs))


def mark_epoch(sim, author, persona_id):
    return sim.emit(author, {"type": "key.epoch", "persona": persona_id})


# ── The failure, pinned so it cannot come back unnoticed ──────────────────


def test_a_rekey_citing_the_same_frontier_can_lose_to_the_incumbent():
    """Without a marker, superseding is a coin toss on a hash."""
    sim, persona = member_org()
    at = heads(sim)
    losses = 0
    for i in range(60):
        incumbent, _ = credentials.build(persona, sim.genesis_id, bytes([i]) * 32, at, (1, 0))
        rekey, _ = credentials.build(persona, sim.genesis_id, bytes([255 - i]) * 32, at, (2, 0))
        winner = credentials.select_current_credential([incumbent, rekey], ancestry(sim))
        if winner.kem_key_id == incumbent.kem_key_id:
            losses += 1
    assert losses > 0, (
        "a same-frontier re-key never lost; the tie-break may have gained "
        "recency, which would make the marker unnecessary -- check before "
        "deleting it"
    )


def test_without_a_marker_the_winner_is_the_hash_and_never_the_newer_one():
    """Why a fixed incumbent can be inescapable, stated deterministically.

    The outcome is decided ENTIRELY by comparing identifiers, so an incumbent
    that happens to hash above a given re-key beats it every single time, no
    matter how much later that re-key is. This asserts the rule rather than a
    sampled rate, which would only be flaky.
    """
    sim, persona = member_org()
    at = heads(sim)
    for i in range(40):
        incumbent, _ = credentials.build(
            persona, sim.genesis_id, bytes([i]) * 32, at, (1, 0)
        )
        rekey, _ = credentials.build(
            persona, sim.genesis_id, bytes([255 - i]) * 32, at, (9, 0)
        )
        winner = credentials.select_current_credential([incumbent, rekey], ancestry(sim))
        assert winner.kem_key_id == max(incumbent.kem_key_id, rekey.kem_key_id), (
            "same-frontier selection must be decided by identifier alone"
        )
        # And being newer -- a strictly later clock -- buys the re-key nothing.
        if incumbent.kem_key_id > rekey.kem_key_id:
            assert winner.kem_key_id == incumbent.kem_key_id


# ── The fix ───────────────────────────────────────────────────────────────


def test_a_marker_lets_the_new_credential_supersede_deterministically():
    sim, persona = member_org()
    before = heads(sim)
    incumbent, _ = credentials.build(persona, sim.genesis_id, b"\x01" * 32, before, (1, 0))

    event = mark_epoch(sim, persona, persona.public_hex)
    state = fold(sim.ledger)
    assert state.valid[event] is True

    after = heads(sim)
    assert after != before, "the marker must move the authority frontier"

    # Whatever the hashes are, the newer credential wins -- 60 different seeds.
    for i in range(60):
        rekey, _ = credentials.build(
            persona, sim.genesis_id, bytes([i]) * 32, after, (2, 0)
        )
        winner = credentials.select_current_credential(
            [incumbent, rekey], ancestry(sim)
        )
        assert winner.kem_key_id == rekey.kem_key_id, (
            "a credential citing a strictly later frontier must supersede"
        )


def test_the_marker_is_what_carries_the_causality():
    """The new frontier descends the old one; that is the whole mechanism."""
    sim, persona = member_org()
    before = frozenset(heads(sim))
    mark_epoch(sim, persona, persona.public_hex)
    after = frozenset(heads(sim))
    assert before <= frozenset(sim.ledger.ancestry(tuple(after)))
    assert not after <= frozenset(sim.ledger.ancestry(tuple(before)))


# ── Inert by construction ─────────────────────────────────────────────────


def test_a_marker_changes_no_authority_state():
    sim, persona = member_org()
    before = fold(sim.ledger)
    fingerprint_before = before.fingerprint()
    held_before = before.holds(persona.public_hex, "link:publish")

    mark_epoch(sim, persona, persona.public_hex)
    after = fold(sim.ledger)

    assert after.holds(persona.public_hex, "link:publish") == held_before
    assert after.members[persona.public_hex].roles == before.members[persona.public_hex].roles
    assert after.root == before.root
    # The fingerprint commits to the heads, and the heads are exactly what a
    # marker is for -- so it must differ, and everything else must not.
    assert after.fingerprint() != fingerprint_before
    assert _state_minus_heads(after) == _state_minus_heads(before), (
        "a marker must change nothing but the frontier"
    )


def _state_minus_heads(state):
    return {
        "org": state.org,
        "root": state.root,
        "lineage": list(state.lineage),
        "authority": {k: sorted(v) for k, v in sorted(state._held.items()) if v},
        "members": {
            pid: (m.current_key, list(m.roles)) for pid, m in sorted(state.members.items())
        },
    }


def test_a_marker_is_not_a_checkpoint():
    sim, persona = member_org()
    mark_epoch(sim, persona, persona.public_hex)
    assert fold(sim.ledger).checkpoints == ()


# ── Who may write one ─────────────────────────────────────────────────────


def test_a_stranger_cannot_mark_an_epoch():
    sim, persona = member_org()
    bad = mark_epoch(sim, KeyPair.generate(), persona.public_hex)
    state = fold(sim.ledger)
    assert state.valid[bad] is False
    assert state.reasons[bad] == R_KEY_EPOCH_UNAUTHORIZED


def test_the_root_may_mark_an_epoch_for_a_member():
    sim, persona = member_org()
    ok = mark_epoch(sim, sim.root, persona.public_hex)
    assert fold(sim.ledger).valid[ok] is True


def test_an_epoch_for_an_unknown_member_is_refused():
    sim, _persona = member_org()
    bad = mark_epoch(sim, sim.root, KeyPair.generate().public_hex)
    state = fold(sim.ledger)
    assert state.valid[bad] is False
    assert state.reasons[bad] == R_UNKNOWN_PERSONA


def test_a_marker_carries_nothing_but_the_persona():
    """I1 in the ledger: the marker must not become a smuggling channel."""
    from tools.network.ledger.errors import SchemaError
    from tools.network.ledger.events import EVENT_TYPES

    with pytest.raises(SchemaError):
        EVENT_TYPES["key.epoch"]({"persona": "a" * 64, "extra": "x"})
    with pytest.raises(SchemaError):
        EVENT_TYPES["key.epoch"]({})
