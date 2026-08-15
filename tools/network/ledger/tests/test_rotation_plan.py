"""Carrying a personal-root rotation into every organization (auto-t1nek).

The dangerous state is the half-finished one: some organizations moved to your
new identity, others still honouring the key a thief holds. These tests hold
the two things that make that safe -- the move actually carries your standing,
and an incomplete ceremony is visible rather than silent.
"""

from __future__ import annotations

import pytest

from tools.network.idkit import KeyPair
from tools.network.idkit.errors import MalformedError
from tools.network.idkit.persona import derive_persona
from tools.network.ledger import fold
from tools.network.ledger.rotation_plan import (
    DONE,
    FOREIGN,
    PENDING,
    build_rekey_payload,
    classify,
    is_complete,
    plan_persona_rotation,
    signing_key_for,
    unfinished,
)

from .conftest import Sim

OLD = bytes(range(32))
NEW = bytes(reversed(range(32)))


def test_a_new_root_derives_a_member_nobody_has_heard_of():
    """The gap itself, stated as a test so it cannot quietly change."""
    genesis = "a" * 64
    assert derive_persona(OLD, genesis).public_hex != derive_persona(NEW, genesis).public_hex


def test_a_plan_names_where_each_organization_must_move():
    steps = plan_persona_rotation(OLD, NEW, [("acme", "a" * 64), ("globex", "b" * 64)])
    assert [s.slug for s in steps] == ["acme", "globex"]
    for step in steps:
        assert step.old_pub == derive_persona(OLD, step.genesis_id).public_hex
        assert step.new_pub == derive_persona(NEW, step.genesis_id).public_hex
        # The member id is stable -- that is what carries roles across.
        assert step.persona_id == step.old_pub
        assert step.old_pub != step.new_pub


def test_each_organization_moves_to_a_different_key():
    """Derivation is per-organization, so one leaked key is not all of them."""
    steps = plan_persona_rotation(OLD, NEW, [("acme", "a" * 64), ("globex", "b" * 64)])
    assert steps[0].new_pub != steps[1].new_pub


def test_rotating_to_the_same_root_is_refused():
    with pytest.raises(MalformedError, match="different root"):
        plan_persona_rotation(OLD, OLD, [("acme", "a" * 64)])


def test_a_duplicated_organization_is_refused():
    with pytest.raises(MalformedError, match="twice"):
        plan_persona_rotation(OLD, NEW, [("acme", "a" * 64), ("acme", "a" * 64)])


def test_a_payload_built_with_the_wrong_seeds_is_refused():
    """A step and the seeds it is executed with must describe one identity."""
    steps = plan_persona_rotation(OLD, NEW, [("acme", "a" * 64)])
    stranger = bytes([7] * 32)
    with pytest.raises(MalformedError, match="do not derive"):
        build_rekey_payload(steps[0], stranger, NEW)


# ── The move, on a real ledger ────────────────────────────────────────────


def joined_org(root_seed):
    """An organization where the member derived from *root_seed* holds a role."""
    sim = Sim()
    sim.role_define(sim.root, "member", ["link:publish"])
    persona = derive_persona(root_seed, sim.genesis_id)
    invite_key = KeyPair.generate()
    invite = sim.invite(sim.root, "member", invite_key=invite_key)
    sim.claim(invite, invite_key, persona)
    return sim, persona


def test_the_move_carries_your_standing_to_the_new_identity():
    sim, old_persona = joined_org(OLD)
    steps = plan_persona_rotation(OLD, NEW, [("acme", sim.genesis_id)])
    step = steps[0]

    payload = build_rekey_payload(step, OLD, NEW)
    event = sim.emit(signing_key_for(step, OLD), payload)
    state = fold(sim.ledger)

    assert state.valid[event] is True
    member = state.members[old_persona.public_hex]
    assert member.current_key == step.new_pub
    assert member.roles == ("member",)
    # The new identity holds the role; the old key no longer does.
    assert state.holds(step.new_pub, "link:publish")
    assert not state.holds(step.old_pub, "link:publish")


def test_after_the_move_the_old_identity_is_no_longer_recognised():
    """The whole security point: the thief's derived member stops working."""
    sim, _old = joined_org(OLD)
    step = plan_persona_rotation(OLD, NEW, [("acme", sim.genesis_id)])[0]
    sim.emit(signing_key_for(step, OLD), build_rekey_payload(step, OLD, NEW))
    state = fold(sim.ledger)
    assert not state.holds(step.old_pub, "link:publish")


def test_a_stranger_cannot_perform_your_move():
    sim, _old = joined_org(OLD)
    step = plan_persona_rotation(OLD, NEW, [("acme", sim.genesis_id)])[0]
    bad = sim.emit(KeyPair.generate(), build_rekey_payload(step, OLD, NEW))
    state = fold(sim.ledger)
    assert state.valid[bad] is False


# ── Seeing where an interrupted ceremony got to ───────────────────────────


def test_an_unfinished_ceremony_is_visible():
    steps = plan_persona_rotation(
        OLD, NEW, [("acme", "a" * 64), ("globex", "b" * 64), ("initech", "c" * 64)]
    )
    by_slug = {s.slug: s for s in steps}
    status = classify(steps, {
        "acme": by_slug["acme"].new_pub,     # moved
        "globex": by_slug["globex"].old_pub,  # not yet
        "initech": None,                      # unknown to that org
    })
    assert status == {"acme": DONE, "globex": PENDING, "initech": FOREIGN}
    assert not is_complete(status)
    assert unfinished(status) == ["globex", "initech"]


def test_a_finished_ceremony_reports_complete():
    steps = plan_persona_rotation(OLD, NEW, [("acme", "a" * 64), ("globex", "b" * 64)])
    status = classify(steps, {s.slug: s.new_pub for s in steps})
    assert is_complete(status)
    assert unfinished(status) == []


def test_an_organization_recognising_neither_key_is_never_complete():
    """Guessing about an org this plan does not describe would be worse."""
    steps = plan_persona_rotation(OLD, NEW, [("acme", "a" * 64)])
    status = classify(steps, {"acme": KeyPair.generate().public_hex})
    assert status == {"acme": FOREIGN}
    assert not is_complete(status)


def test_an_empty_ceremony_is_not_complete():
    """Nothing planned means nothing was checked -- not that all is well."""
    assert not is_complete(classify([], {}))


def test_resuming_repeats_nothing_already_done():
    sim_a, _ = joined_org(OLD)
    sim_b, _ = joined_org(OLD)
    steps = plan_persona_rotation(
        OLD, NEW, [("acme", sim_a.genesis_id), ("globex", sim_b.genesis_id)]
    )
    # Only the first organization is moved -- the ceremony is interrupted.
    first = steps[0]
    sim_a.emit(signing_key_for(first, OLD), build_rekey_payload(first, OLD, NEW))
    state_a = fold(sim_a.ledger)
    current = {
        "acme": state_a.members[first.persona_id].current_key,
        "globex": steps[1].old_pub,
    }
    assert unfinished(classify(steps, current)) == ["globex"]
