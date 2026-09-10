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

from tools.network.relaykit.fleet_stream_wire import CAP_FLEET_DIRECTED_STREAM

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tools.network.registry.relay import Tunnel, TunnelHub

#: Negotiated capability a tunnel must carry on BOTH legs before it can
#: take part in a directed fleet pair. The name is owned by the relaykit
#: wire module so the connector and the relay cannot drift; it is
#: re-exported here for the resolver's callers and tests.
__all__ = ["CAP_FLEET_DIRECTED_STREAM"]

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


#: Pair lifecycle. A pair is OFFERED until both legs accept, ACTIVE only
#: after a successful ``activate``, and TERMINAL once closed for any
#: reason including a failed activation.
PAIR_OFFERED = "offered"
PAIR_ACTIVE = "active"
PAIR_TERMINAL = "terminal"

#: Lifecycle refusals, distinct from the resolution reasons above.
PAIR_NOT_A_LEG = "tunnel-is-not-a-leg-of-this-pair"
PAIR_NOT_OFFERED = "pair-is-no-longer-offered"
PAIR_AWAITING_ACCEPT = "both-legs-have-not-accepted"
PAIR_SOURCE_REPLACED = "source-tunnel-replaced-since-resolution"
PAIR_DESTINATION_REPLACED = "destination-tunnel-replaced-since-resolution"
PAIR_NOT_ACTIVE = "pair-is-not-active"


class DirectedPair:
    """Lifecycle state for one directed pair of pinned tunnels.

    Pure state. It performs no I/O, encodes no frames, and owns no
    queues, credit or reservations. What it records is what the relay
    TOLD it; it cannot observe a socket.

    Two things it deliberately does NOT prove, because pure state
    cannot:

    * that both READY controls were enqueued before any DATA was
      accepted. Ordering is a property of the caller's enqueue path.
      This object only refuses DATA before activation and records READY
      receipt when told.
    * that resources were released concurrently. ``close`` gives
      logical idempotence -- exactly one caller observes the terminal
      transition -- not proof of I/O cleanup.

    EVENTS ARE FENCED BY TUNNEL IDENTITY, not by leg label. Every method
    takes the pinned ``Tunnel`` object and rejects any other, so an
    event from a superseded tunnel can still terminate ITS OWN pair and
    release what that pair holds, while being structurally unable to
    address the replacement pair, which pins a different object. A
    blanket "ignore stale events" rule would instead leak the old pair.
    """

    def __init__(self, source: "Tunnel", destination: "Tunnel"):
        self.source = source
        self.destination = destination
        self.state = PAIR_OFFERED
        self.terminal_reason: Optional[str] = None
        self._accepted: set = set()
        self._ready: set = set()
        self._cleanup_claimed = False

    # ── leg identity ────────────────────────────────────────────────

    def _is_leg(self, tunnel: "Tunnel") -> bool:
        return tunnel is self.source or tunnel is self.destination

    # ── transitions ─────────────────────────────────────────────────

    def leg_accepted(self, tunnel: "Tunnel") -> str:
        """Record one leg's OPEN_OK. Idempotent per leg."""
        if not self._is_leg(tunnel):
            return PAIR_NOT_A_LEG
        if self.state != PAIR_OFFERED:
            return PAIR_NOT_OFFERED
        self._accepted.add(id(tunnel))
        return PAIR_OK

    def activate(self, hub: "TunnelHub") -> str:
        """Go ACTIVE, or terminate with a reason.

        Resolution was instantaneous, not a lease: either tunnel may
        have been replaced in its slot since. This re-runs the SAME
        admission rule rather than a weaker copy of it, because a
        ``DirectedPair`` can be constructed without going through the
        resolver -- and a hand-built pair of live tunnels from two
        different organizations would otherwise activate, each leg
        validating happily under its own org.

        The resolver answers "which tunnel is the destination for this
        source, right now". Pinned-identity equality on its answer is
        what turns that into "and it is still the one we pinned".

        A failed activation is terminal; the pair does not linger.
        """
        if self.state == PAIR_TERMINAL:
            return self.terminal_reason or PAIR_NOT_OFFERED
        if self.state == PAIR_ACTIVE:
            return PAIR_OK
        if len(self._accepted) < 2:
            return PAIR_AWAITING_ACCEPT

        current, reason = resolve_directed_pair(
            hub, self.source,
            self.destination.persona_pub, self.destination.machine,
        )
        if reason != PAIR_OK:
            self._terminate(reason)
            return reason
        if current is not self.destination:
            self._terminate(PAIR_DESTINATION_REPLACED)
            return PAIR_DESTINATION_REPLACED

        self.state = PAIR_ACTIVE
        return PAIR_OK

    def ready_enqueued(self, tunnel: "Tunnel") -> str:
        """Record that this leg's READY control was ENQUEUED.

        Named for the boundary it actually sits on. The caller reports
        what it put on the queue; nothing here observes delivery, and
        ``may_send`` therefore means "we have enqueued this leg's
        READY", not "this leg has it".
        """
        if not self._is_leg(tunnel):
            return PAIR_NOT_A_LEG
        if self.state != PAIR_ACTIVE:
            return PAIR_NOT_ACTIVE
        self._ready.add(id(tunnel))
        return PAIR_OK

    def close(self, tunnel: "Optional[Tunnel]", reason: str) -> bool:
        """Terminate. True only for the caller that made it terminal.

        A superseded tunnel closing its own pair is legitimate and is
        how that pair's resources are released. Passing ``None`` closes
        on the relay's own initiative.
        """
        if tunnel is not None and not self._is_leg(tunnel):
            return False
        if self.state == PAIR_TERMINAL:
            return False
        self._terminate(reason)
        return True

    def _terminate(self, reason: str) -> None:
        self.state = PAIR_TERMINAL
        self.terminal_reason = reason

    def claim_cleanup(self) -> bool:
        """True for exactly one caller, for the pair's whole lifetime.

        ``close`` answers a different question -- "did I cause the
        terminal transition" -- and it is the WRONG hook to release
        resources on. ``activate`` terminates the pair internally when
        admission fails, so a caller that released only when ``close``
        returned True would strand every failed offer: nobody caused
        that transition from outside.

        Call this from an unconditional ``finally`` covering every
        outcome, including a failed activation and a pair that never
        activated at all.
        """
        if self._cleanup_claimed:
            return False
        self._cleanup_claimed = True
        return True

    # ── gates ───────────────────────────────────────────────────────

    def may_send(self, tunnel: "Tunnel") -> bool:
        """A leg may send once its READY has been enqueued."""
        return (
            self._is_leg(tunnel)
            and self.state == PAIR_ACTIVE
            and id(tunnel) in self._ready
        )

    def may_receive(self, tunnel: "Tunnel") -> bool:
        """A leg may receive from its own acceptance onward.

        Receiving is deliberately NOT gated on READY: the peer that is
        told first will send while this side's READY is still in flight,
        and refusing that frame would fail the first message of every
        pair whose legs are told microseconds apart.
        """
        return (
            self._is_leg(tunnel)
            and self.state != PAIR_TERMINAL
            and id(tunnel) in self._accepted
        )

    def accepts_data(self, tunnel: "Tunnel") -> str:
        """Whether DATA from *tunnel* is admissible right now."""
        if not self._is_leg(tunnel):
            return PAIR_NOT_A_LEG
        if self.state != PAIR_ACTIVE:
            return PAIR_NOT_ACTIVE
        return PAIR_OK
