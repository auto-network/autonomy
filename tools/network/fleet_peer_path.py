"""Generation fencing for one peer's connection attempts (auto-ieh3l).

Contract graph://7ed8a519-356 section 9: one controller per
``(authority_domain, durable_peer_machine_pub)`` owns that peer's attempts,
and "a stale accept, a late descriptor, or an old disconnect closes only its
captured generation: it cannot unregister a replacement slot, replace a newer
descriptor, or cancel the peer's current work."

TWO VALUES, TWO JOBS, deliberately not merged:

* ``pair_id`` says WHICH CONNECTION IS LIVE. Agreed with auto-fh2nv
  2026-09-10: a fresh 128-bit random value per pair, never reused and NOT
  monotonic, so it is fenced by EQUALITY. A callback carrying anything other
  than the id we currently hold is from a dead attempt.
* ``descriptor_generation`` says WHICH DESCRIPTOR IS CURRENT. Monotonic per
  machine, published on the peer's reachability row, so it is fenced by
  ORDERING.

Conflating them is what section 9 warns about: one answers "is this connection
still mine", the other "is this address information still true", and a value
that tries to be both is wrong for one of them.

This module is the decision only. It holds no socket and performs no I/O, so
the rules can be exercised without a relay, and it plugs into the carrier's
endpoint when auto-fh2nv lands.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

#: Reset codes the carrier reports on a torn-down leg, and whether the cause
#: is the peer/protocol saying STOP or the transport saying NOT NOW. Retrying
#: a deliberate close hammers a peer that meant to hang up; not retrying a
#: transport failure turns a recoverable outage into a permanent one. Both are
#: silent, which is why the code is carried rather than collapsed to a bool.
RESET_ORDERLY = 1
RESET_OPEN_TIMEOUT = 2
RESET_ROUTE_RELEASED = 5
RESET_TUNNEL_LOST = 6
RESET_PROTOCOL = 7

#: Backwards-compatible name for code 1; the carrier calls it "orderly".
RESET_PEER_CLOSED = RESET_ORDERLY

#: The codes that mean STOP: the peer or the protocol saying do not come back.
#: EVERYTHING ELSE RETRIES, including a code this table has never seen.
#:
#: The default is inverted deliberately, and it took two findings to get here.
#: First: auto-fh2nv's crosstalk summary listed four codes, their contract
#: defines five, and the missing one — 5, "route released / activation
#: failed" — is retryable. Under a retryable-allowlist it fell through to STOP
#: and this controller would have abandoned a peer whose route was merely
#: replaced. Second: reading their WIRE module rather than their contract
#: shows seven codes, not five — 3 (overflow) and 4 (byte budget) exist too,
#: and would have fallen through the same way.
#:
#: An allowlist of retryable codes fails closed into permanent silence every
#: time the other side adds a code. A stop-list fails into a bounded retry
#: instead: wrong for a genuinely terminal condition, but the backoff envelope
#: saturates at max_backoff, so the cost is a slow poll rather than a storm —
#: against a permanent silent give-up, which is the failure this whole bead
#: exists to remove.
_TERMINAL_RESETS = frozenset({RESET_ORDERLY, RESET_PROTOCOL})

#: Codes this module was written knowing about. A code outside it still gets a
#: decision (retry); it is recorded so "the carrier grew a code" is visible
#: rather than inferred from behaviour.
_KNOWN_RESETS = frozenset({
    RESET_ORDERLY, RESET_OPEN_TIMEOUT, RESET_ROUTE_RELEASED,
    RESET_TUNNEL_LOST, RESET_PROTOCOL,
})

#: What the controller decided to do about an event.
IGNORE = "ignore"        # not ours, or from a dead attempt
RETRY = "retry"          # our live attempt failed recoverably
STOP = "stop"            # our live attempt ended and must not be retried
STAND_DOWN = "stand_down"  # another attempt holds this peer; not ours to cancel


@dataclass
class PeerPathController:
    """One peer's attempts, and the fencing that keeps old ones harmless."""

    authority_domain: str
    peer_machine_pub: str
    #: The pair we currently believe is ours. None when nothing is open.
    pair_id: Optional[str] = None
    #: Highest descriptor generation seen for this peer.
    descriptor_generation: int = -1
    _history: list = field(default_factory=list, repr=False)

    def opened(self, pair_id: str) -> None:
        """Record the pair the carrier admitted for us."""
        if not pair_id:
            raise ValueError("a pair id is required to fence on")
        self.pair_id = pair_id

    def open_refused(self, reason: str) -> str:
        """The carrier refused our open.

        ``operation-already-open`` means another attempt already holds this
        peer. By this controller's own one-per-peer guarantee that attempt is
        not ours, so cancelling or replacing it would be reaching into someone
        else's work — exactly the "cannot unregister a replacement slot" the
        contract forbids. Any other refusal is a real failure for us.
        """
        if reason == "operation-already-open":
            return STAND_DOWN
        return RETRY

    def transport_failed(self) -> str:
        """The dial failed before any pair was admitted.

        auto-fh2nv surfaces three distinct failures out of open(), and the
        adapter must keep them apart because they produce different decisions
        here:

            FleetStreamRefused(reason)        -> open_refused(reason)
            FleetStreamClosed(pair_id, code)  -> reset(pair_id, code)
            ConnectionError                   -> transport_failed()

        A bare transport fault never reached admission, so there is no pair_id
        to fence on and nothing to clear: the relay minted nothing and this
        controller holds nothing. Retry is immediately legal.

        This method exists so the mapping is TOTAL. Without it an adapter
        author has to decide what a ConnectionError means, and the plausible
        wrong answer — folding it in with a refusal — turns a transient
        network fault into a permanent stand-down.
        """
        return RETRY

    def reset(self, pair_id: str, code: int) -> str:
        """A leg was torn down. Decide whether it was OURS and what to do."""
        if self.pair_id is None or pair_id != self.pair_id:
            # A dead attempt, or a pair we never opened. It cannot cancel the
            # peer's current work and it cannot clear our live pair.
            return IGNORE
        self.pair_id = None
        if code not in _KNOWN_RESETS:
            self._history.append(("unknown-reset", code))
        return STOP if code in _TERMINAL_RESETS else RETRY

    def descriptor(self, generation: int, *, verified: bool = False) -> bool:
        """Accept a descriptor. Ordering decides the ordinary case; the
        SIGNATURE decides the hard one.

        Newer than what we hold: accept, always.

        Older or equal: normally ignore, because a late arrival must not
        replace a newer descriptor (contract section 9). But if the caller has
        VERIFIED it — machine signature against the row key, and the machine
        present in the current roster (section 1) — then accept it and reset
        the high-water mark to it.

        WHY THE VERIFIED CASE EXISTS, agreed with auto-0905-002201
        2026-09-10. Their publisher guarantees strictly-increasing generations
        across restarts, crashes, failed publishes and clock steps, with one
        residual window it cannot close from inside: if the machine-local
        counter store is rebuilt while the machine identity survives AND every
        projection of that machine's own descriptor is unobservable at the
        next publish, the counter restarts at 1. We would then hold a higher
        number than the live descriptor and reject the CURRENT one forever,
        routing on addresses that may be gone, with no error at either end.

        The asymmetry decides it. Rejecting wrongly is a permanent silent
        outage. Accepting a replayed old descriptor costs only liveness: its
        addresses are hints, and a hint pointing at the wrong host is refused
        at the hello, which checks the peer's machine_pub against the expected
        one. So a validly signed descriptor from a rostered machine is treated
        as current whatever number it carries, and ordering is demoted to what
        it actually is — an optimisation against reprocessing, not the
        authority.

        A downgrade is returned as accepted but is NOT silent: callers record
        it, because "we went backwards" is exactly the event that should be
        visible if the residual window ever opens.
        """
        if generation > self.descriptor_generation:
            self.descriptor_generation = generation
            return True
        if verified:
            self.descriptor_generation = generation
            self._history.append(("descriptor-downgrade", generation))
            return True
        return False

    def unknown_resets(self) -> list:
        """Reset codes seen that this module was not written knowing about.

        Empty is expected. A non-empty list means the carrier grew a code and
        this table has not caught up — visible rather than inferred from a
        peer that quietly stopped retrying.
        """
        return [code for kind, code in self._history
                if kind == "unknown-reset"]

    def downgrades(self) -> list:
        """Generations accepted below the high-water mark, in order.

        Empty is the expected answer. A non-empty list means a peer's
        generation counter went backwards, which is the one failure mode the
        publisher cannot rule out — so it is recorded rather than inferred
        from an absence of symptoms.
        """
        return [gen for kind, gen in self._history
                if kind == "descriptor-downgrade"]
