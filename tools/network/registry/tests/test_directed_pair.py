"""Exact destination resolution for a directed fleet pair.

Real ``Tunnel`` objects in a real ``TunnelHub``, registered and replaced
through the hub's own methods: the property under test is which slot a
lookup lands on after real registration, so mocking the hub would test
the mock. Only ``ws`` is a stand-in, because resolution never touches it.
"""

from __future__ import annotations

from tools.network.registry.relay import Tunnel, TunnelHub
from tools.network.registry.directed_pair import (
    CAP_FLEET_DIRECTED_STREAM as CAP,
    PAIR_DESTINATION_ABSENT,
    PAIR_DESTINATION_CAPABILITY,
    PAIR_DESTINATION_IDENTITY_MISSING,
    PAIR_OK,
    PAIR_SELF,
    PAIR_SOURCE_CAPABILITY,
    PAIR_SOURCE_IDENTITY_MISSING,
    PAIR_SOURCE_STALE,
    resolve_directed_pair,
)

ORG_A = "org-a"
ORG_B = "org-b"
PERSONA_A = "ab" * 32
PERSONA_B = "cd" * 32
MACHINE_A = "11" * 32
MACHINE_B = "22" * 32


def _tunnel(org=ORG_A, persona=PERSONA_A, machine=MACHINE_A, caps=(CAP,)):
    return Tunnel(object(), org, persona_pub=persona, machine=machine,
                  caps=tuple(caps))


def _hub(*tunnels):
    hub = TunnelHub()
    for tunnel in tunnels:
        hub.register(tunnel)
    return hub


def test_the_exact_destination_slot_is_returned():
    source = _tunnel(machine=MACHINE_A)
    dest = _tunnel(machine=MACHINE_B)
    hub = _hub(source, dest)

    assert resolve_directed_pair(hub, source, PERSONA_A, MACHINE_B) == (
        dest, PAIR_OK
    )


def test_the_same_persona_on_another_machine_is_not_the_destination():
    """Slot identity is (persona, machine); persona alone must not match."""
    source = _tunnel(machine=MACHINE_A)
    hub = _hub(source)

    assert resolve_directed_pair(hub, source, PERSONA_A, MACHINE_B) == (
        None, PAIR_DESTINATION_ABSENT
    )


def test_the_same_machine_under_another_persona_is_not_the_destination():
    source = _tunnel(persona=PERSONA_A, machine=MACHINE_A)
    other = _tunnel(persona=PERSONA_B, machine=MACHINE_B)
    hub = _hub(source, other)

    assert resolve_directed_pair(hub, source, PERSONA_A, MACHINE_B) == (
        None, PAIR_DESTINATION_ABSENT
    )


def test_a_destination_in_another_org_is_not_reachable():
    """Org comes from the authenticated source, never from the request."""
    source = _tunnel(org=ORG_A, machine=MACHINE_A)
    foreign = _tunnel(org=ORG_B, machine=MACHINE_B)
    hub = _hub(source, foreign)

    assert resolve_directed_pair(hub, source, PERSONA_A, MACHINE_B) == (
        None, PAIR_DESTINATION_ABSENT
    )


def test_a_peer_cannot_pair_with_itself():
    source = _tunnel(machine=MACHINE_A)
    hub = _hub(source)

    assert resolve_directed_pair(hub, source, PERSONA_A, MACHINE_A) == (
        None, PAIR_SELF
    )


def test_reconnect_resolves_to_the_new_tunnel_not_the_stale_one():
    """`register` replaces a slot; the lookup must follow the replacement."""
    source = _tunnel(machine=MACHINE_A)
    old_dest = _tunnel(machine=MACHINE_B)
    hub = _hub(source, old_dest)
    new_dest = _tunnel(machine=MACHINE_B)
    replaced = hub.register(new_dest)

    assert replaced is old_dest
    dest, reason = resolve_directed_pair(hub, source, PERSONA_A, MACHINE_B)
    assert reason == PAIR_OK
    assert dest is new_dest and dest is not old_dest


def test_a_superseded_source_cannot_open_a_pair():
    """The old object still works for its own handler after a reconnect.

    It no longer holds its slot, so it must not act for that slot here.
    """
    stale_source = _tunnel(machine=MACHINE_A)
    dest = _tunnel(machine=MACHINE_B)
    hub = _hub(stale_source, dest)
    hub.register(_tunnel(machine=MACHINE_A))      # same slot, new generation

    assert resolve_directed_pair(hub, stale_source, PERSONA_A, MACHINE_B) == (
        None, PAIR_SOURCE_STALE
    )


def test_an_unregistered_source_cannot_open_a_pair():
    source = _tunnel(machine=MACHINE_A)
    dest = _tunnel(machine=MACHINE_B)
    hub = _hub(dest)                              # source never registered

    assert resolve_directed_pair(hub, source, PERSONA_A, MACHINE_B) == (
        None, PAIR_SOURCE_STALE
    )


def test_a_legacy_empty_machine_source_fails_closed():
    """`machine=""` is the shared v1 slot, not a machine identity."""
    source = _tunnel(machine="")
    dest = _tunnel(machine=MACHINE_B)
    hub = _hub(source, dest)

    assert resolve_directed_pair(hub, source, PERSONA_A, MACHINE_B) == (
        None, PAIR_SOURCE_IDENTITY_MISSING
    )


def test_an_empty_destination_machine_fails_closed():
    source = _tunnel(machine=MACHINE_A)
    legacy = _tunnel(machine="")
    hub = _hub(source, legacy)

    assert resolve_directed_pair(hub, source, PERSONA_A, "") == (
        None, PAIR_DESTINATION_IDENTITY_MISSING
    )


def test_an_empty_destination_persona_fails_closed():
    source = _tunnel(machine=MACHINE_A)
    hub = _hub(source)

    assert resolve_directed_pair(hub, source, "", MACHINE_B) == (
        None, PAIR_DESTINATION_IDENTITY_MISSING
    )


def test_a_missing_capability_names_the_side_that_lacks_it():
    """A caller reporting the wrong side sends an operator to the wrong box."""
    plain_source = _tunnel(machine=MACHINE_A, caps=())
    dest = _tunnel(machine=MACHINE_B)
    hub = _hub(plain_source, dest)
    assert resolve_directed_pair(hub, plain_source, PERSONA_A, MACHINE_B) == (
        None, PAIR_SOURCE_CAPABILITY
    )

    source = _tunnel(machine=MACHINE_A)
    plain_dest = _tunnel(machine=MACHINE_B, caps=())
    hub = _hub(source, plain_dest)
    assert resolve_directed_pair(hub, source, PERSONA_A, MACHINE_B) == (
        None, PAIR_DESTINATION_CAPABILITY
    )


def test_capability_alone_does_not_stand_in_for_identity():
    """Both legs carrying the capability is not proof of a valid slot."""
    source = _tunnel(machine="", caps=(CAP,))
    dest = _tunnel(machine=MACHINE_B, caps=(CAP,))
    hub = _hub(source, dest)

    assert resolve_directed_pair(hub, source, PERSONA_A, MACHINE_B)[1] == (
        PAIR_SOURCE_IDENTITY_MISSING
    )


# ── lifecycle ───────────────────────────────────────────────────────

from tools.network.registry.directed_pair import (  # noqa: E402
    DirectedPair,
    PAIR_ACTIVE,
    PAIR_AWAITING_ACCEPT,
    PAIR_DESTINATION_REPLACED,
    PAIR_NOT_A_LEG,
    PAIR_NOT_ACTIVE,
    PAIR_OFFERED,
    PAIR_SOURCE_REPLACED,
    PAIR_TERMINAL,
)


def _offered(hub, source, dest):
    pair = DirectedPair(source, dest)
    pair.leg_accepted(source)
    pair.leg_accepted(dest)
    return pair


def test_a_pair_activates_once_both_legs_accept():
    source, dest = _tunnel(machine=MACHINE_A), _tunnel(machine=MACHINE_B)
    hub = _hub(source, dest)
    pair = DirectedPair(source, dest)

    assert pair.activate(hub) == PAIR_AWAITING_ACCEPT
    pair.leg_accepted(source)
    assert pair.activate(hub) == PAIR_AWAITING_ACCEPT
    pair.leg_accepted(dest)
    assert pair.activate(hub) == PAIR_OK
    assert pair.state == PAIR_ACTIVE


def test_activation_rechecks_that_each_leg_still_holds_its_slot():
    """Resolution was instantaneous, not a lease.

    Both legs can be replaced between resolving the pair and activating
    it, so activation must recheck rather than trust the pinned object.
    """
    source, dest = _tunnel(machine=MACHINE_A), _tunnel(machine=MACHINE_B)
    hub = _hub(source, dest)
    pair = _offered(hub, source, dest)
    hub.register(_tunnel(machine=MACHINE_B))       # destination reconnects

    assert pair.activate(hub) == PAIR_DESTINATION_REPLACED
    assert pair.state == PAIR_TERMINAL              # failed activation is terminal

    source2, dest2 = _tunnel(machine=MACHINE_A), _tunnel(machine=MACHINE_B)
    hub2 = _hub(source2, dest2)
    pair2 = _offered(hub2, source2, dest2)
    hub2.register(_tunnel(machine=MACHINE_A))       # source reconnects
    assert pair2.activate(hub2) == PAIR_SOURCE_REPLACED


def test_activation_rechecks_negotiated_eligibility():
    source, dest = _tunnel(machine=MACHINE_A), _tunnel(machine=MACHINE_B)
    hub = _hub(source, dest)
    pair = _offered(hub, source, dest)
    dest.caps = ()                                  # renegotiated away

    assert pair.activate(hub) == PAIR_DESTINATION_CAPABILITY
    assert pair.state == PAIR_TERMINAL


def test_data_before_activation_is_a_typed_refusal():
    source, dest = _tunnel(machine=MACHINE_A), _tunnel(machine=MACHINE_B)
    hub = _hub(source, dest)
    pair = _offered(hub, source, dest)

    assert pair.accepts_data(source) == PAIR_NOT_ACTIVE
    pair.activate(hub)
    assert pair.accepts_data(source) == PAIR_OK


def test_sending_waits_for_this_leg_s_own_ready_but_receiving_does_not():
    """The peer told first will send while our READY is still in flight.

    Gating RECEIVE on READY would fail the first frame of every pair
    whose legs are told microseconds apart.
    """
    source, dest = _tunnel(machine=MACHINE_A), _tunnel(machine=MACHINE_B)
    hub = _hub(source, dest)
    pair = _offered(hub, source, dest)
    pair.activate(hub)

    assert pair.may_send(source) is False
    assert pair.may_receive(source) is True         # since its own OPEN_OK
    pair.ready_delivered(source)
    assert pair.may_send(source) is True
    assert pair.may_send(dest) is False             # not told yet


def test_ready_is_only_recorded_for_an_active_pair():
    source, dest = _tunnel(machine=MACHINE_A), _tunnel(machine=MACHINE_B)
    hub = _hub(source, dest)
    pair = _offered(hub, source, dest)

    assert pair.ready_delivered(source) == PAIR_NOT_ACTIVE


def test_a_foreign_tunnel_cannot_drive_the_pair():
    """Events are fenced by pinned identity, not by leg label."""
    source, dest = _tunnel(machine=MACHINE_A), _tunnel(machine=MACHINE_B)
    hub = _hub(source, dest)
    pair = _offered(hub, source, dest)
    pair.activate(hub)
    stranger = _tunnel(persona=PERSONA_B, machine=MACHINE_A)

    assert pair.leg_accepted(stranger) == PAIR_NOT_A_LEG
    assert pair.ready_delivered(stranger) == PAIR_NOT_A_LEG
    assert pair.accepts_data(stranger) == PAIR_NOT_A_LEG
    assert pair.may_send(stranger) is False
    assert pair.may_receive(stranger) is False
    assert pair.close(stranger, "hostile") is False
    assert pair.state == PAIR_ACTIVE


def test_a_superseded_tunnel_closes_its_own_pair_and_not_the_replacement():
    """THE ONE THAT MATTERS for generation fencing.

    A stale tunnel must still be able to terminate the pair it belongs
    to, or that pair leaks. It must be structurally unable to reach the
    replacement's pair, which pins a different object.
    """
    source = _tunnel(machine=MACHINE_A)
    old_dest = _tunnel(machine=MACHINE_B)
    hub = _hub(source, old_dest)
    old_pair = _offered(hub, source, old_dest)
    old_pair.activate(hub)

    new_dest = _tunnel(machine=MACHINE_B)
    hub.register(new_dest)                          # replaces old_dest's slot
    new_pair = _offered(hub, source, new_dest)
    assert new_pair.activate(hub) == PAIR_OK

    assert old_pair.close(old_dest, "connection-lost") is True
    assert old_pair.state == PAIR_TERMINAL
    assert new_pair.state == PAIR_ACTIVE            # untouched
    assert new_pair.close(old_dest, "connection-lost") is False


def test_exactly_one_caller_observes_the_terminal_transition():
    """Logical idempotence. This is not proof of concurrent I/O cleanup."""
    source, dest = _tunnel(machine=MACHINE_A), _tunnel(machine=MACHINE_B)
    hub = _hub(source, dest)
    pair = _offered(hub, source, dest)
    pair.activate(hub)

    assert pair.close(source, "reset") is True
    assert pair.close(dest, "eof") is False
    assert pair.close(None, "relay-shutdown") is False
    assert pair.terminal_reason == "reset"


def test_a_terminal_pair_admits_nothing_further():
    source, dest = _tunnel(machine=MACHINE_A), _tunnel(machine=MACHINE_B)
    hub = _hub(source, dest)
    pair = _offered(hub, source, dest)
    pair.activate(hub)
    pair.close(source, "reset")

    assert pair.accepts_data(source) == PAIR_NOT_ACTIVE
    assert pair.may_send(source) is False
    assert pair.may_receive(source) is False
    assert pair.ready_delivered(source) == PAIR_NOT_ACTIVE
