"""Resolve the exact destination tunnel for a directed fleet pair.

One question, one answer, with a named reason when the answer is no:
given the AUTHENTICATED source tunnel and a requested destination
``(persona_pub, machine)``, which live tunnel is the destination?

This consumes ``TunnelHub.get_slot`` and adds nothing to the wire. It
mints no identifiers, opens no stream, registers nothing, forwards
nothing, and does not touch viewer, fanout or public ``tls-stream/1``
paths. The READY handshake and the pair lifecycle build on this and are
deliberately not here: READY cannot be specified until "which
destination" has a single answer with typed failures.

Reasons are named rather than boolean because a caller that reports the
wrong side sends an operator after the wrong machine. That is not
hypothetical: the connector stop path reported an unassigned tunnel
server while the real cause was a machine absent from the roster.
"""

from __future__ import annotations

from typing import Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tools.network.registry.relay import Tunnel, TunnelHub

#: Negotiated capability a tunnel must carry on BOTH legs before it can
#: take part in a directed fleet pair. Defining the name here does not
#: negotiate it; wiring it into the hello is separate work.
CAP_FLEET_DIRECTED_STREAM = "fleet-directed-stream/1"

#: Every refusal this resolver can return. Callers report the reason
#: they were given; they do not re-derive one.
PAIR_OK = "ok"
PAIR_SOURCE_STALE = "source-tunnel-superseded"
PAIR_SOURCE_IDENTITY_MISSING = "source-machine-identity-missing"
PAIR_DESTINATION_IDENTITY_MISSING = "destination-machine-identity-missing"
PAIR_DESTINATION_ABSENT = "destination-slot-absent"
PAIR_SELF = "destination-is-the-source"
PAIR_SOURCE_CAPABILITY = "source-capability-not-negotiated"
PAIR_DESTINATION_CAPABILITY = "destination-capability-not-negotiated"


def _identifies_a_machine(persona_pub: object, machine: object) -> bool:
    """Both halves of a slot key must name something.

    ``Tunnel.machine`` defaults to ``""``, which is the legacy v1 slot:
    every v1 connection for a persona shares it. Pairing on an empty
    identity would therefore resolve to whichever v1 tunnel happens to
    hold the slot, so it fails closed here rather than later.
    """
    return bool(
        isinstance(persona_pub, str) and persona_pub
        and isinstance(machine, str) and machine
    )


def resolve_directed_pair(
    hub: "TunnelHub",
    source: "Tunnel",
    destination_persona_pub: str,
    destination_machine_pub: str,
) -> Tuple[Optional["Tunnel"], str]:
    """The destination tunnel for a directed pair, or ``(None, reason)``.

    The organization is taken from *source*, which the caller has
    already authenticated, and never from the request body. A capability
    proves what a connection can speak; it is not a machine identity, so
    both are checked.
    """
    if not _identifies_a_machine(source.persona_pub, source.machine):
        return None, PAIR_SOURCE_IDENTITY_MISSING
    if not _identifies_a_machine(
        destination_persona_pub, destination_machine_pub
    ):
        return None, PAIR_DESTINATION_IDENTITY_MISSING

    # A reconnect installs a new tunnel in the same slot. The old object
    # stays usable to its own handler, so a superseded source must not be
    # able to open pairs on behalf of a slot it no longer holds.
    current_source = hub.get_slot(
        source.org, source.persona_pub, source.machine
    )
    if current_source is not source:
        return None, PAIR_SOURCE_STALE

    destination = hub.get_slot(
        source.org, destination_persona_pub, destination_machine_pub
    )
    if destination is None:
        return None, PAIR_DESTINATION_ABSENT
    if destination is source:
        return None, PAIR_SELF

    if CAP_FLEET_DIRECTED_STREAM not in (source.caps or ()):
        return None, PAIR_SOURCE_CAPABILITY
    if CAP_FLEET_DIRECTED_STREAM not in (destination.caps or ()):
        return None, PAIR_DESTINATION_CAPABILITY

    return destination, PAIR_OK
