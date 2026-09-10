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
RESET_TUNNEL_LOST = 6
RESET_PEER_CLOSED = 1
RESET_OPEN_TIMEOUT = 2
RESET_PROTOCOL = 7

_RETRYABLE_RESETS = frozenset({RESET_TUNNEL_LOST, RESET_OPEN_TIMEOUT})

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

    def reset(self, pair_id: str, code: int) -> str:
        """A leg was torn down. Decide whether it was OURS and what to do."""
        if self.pair_id is None or pair_id != self.pair_id:
            # A dead attempt, or a pair we never opened. It cannot cancel the
            # peer's current work and it cannot clear our live pair.
            return IGNORE
        self.pair_id = None
        return RETRY if code in _RETRYABLE_RESETS else STOP

    def descriptor(self, generation: int) -> bool:
        """Accept a descriptor only if it is NEWER than what we hold.

        Ordering, not equality: a late descriptor for an older generation
        cannot replace a newer one. Returns whether it was accepted.
        """
        if generation <= self.descriptor_generation:
            return False
        self.descriptor_generation = generation
        return True
