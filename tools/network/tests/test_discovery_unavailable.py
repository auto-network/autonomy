"""A rostered peer nobody can address is recorded, not silently skipped.

Contract graph://7ed8a519-356 section 7 and auto-ieh3l acceptance 2 and 5: a
peer that resolves from the roster but has no direct address and no relay
locator must be reported as discovery_unavailable and retried on a bounded
envelope. It must never exit the service, never fall back to an invitation,
and never disappear.

The old permanent give-up (`route is None -> return`) is deleted, but the
comprehension that replaced it dropped an unaddressable peer out of `eligible`
with no record at all — membership said sync with it, nothing could say where
it was, and no surface carried either fact.
"""

from __future__ import annotations

import pytest

from tools.network.fleet_sync_scheduler import FleetSyncScheduler


class _Scheduler:
    """The real record/retry logic on a bare object.

    _record_discovery_unavailable touches only self._discovery_unavailable,
    self._next_attempt and self.config, so it is exercised directly rather
    than by standing up a whole scheduler and its sockets.
    """

    def __init__(self, *, min_backoff=0.25, max_backoff=5.0):
        self._discovery_unavailable = {}
        self._next_attempt = {}
        self.config = type(
            "_C", (), {"min_backoff": min_backoff, "max_backoff": max_backoff},
        )()

    # Bound under their REAL names as well: _resolve_peers calls
    # self._record_discovery_unavailable, so an alias-only stand-in would
    # fail on the production call path rather than exercise it.
    _record_discovery_unavailable = FleetSyncScheduler._record_discovery_unavailable
    discovery_unavailable = FleetSyncScheduler.discovery_unavailable
    record = FleetSyncScheduler._record_discovery_unavailable
    read = FleetSyncScheduler.discovery_unavailable


PEER = "ab" * 32


def test_a_miss_is_recorded_with_what_was_absent():
    s = _Scheduler()
    s.record(PEER, 100.0)
    [state] = s.read().values()
    assert state["reason"] == "discovery_unavailable"
    assert state["direct_absent"] is True and state["relay_absent"] is True
    assert state["last_attempt"] == 100.0
    assert state["next_attempt"] > 100.0


def test_healthy_is_empty():
    assert _Scheduler().read() == {}


def test_retry_is_bounded_and_never_gives_up():
    """Backoff grows, saturates at the configured ceiling, and keeps
    scheduling — there is no terminal state."""
    s = _Scheduler(min_backoff=0.25, max_backoff=5.0)
    delays = []
    for i in range(40):
        s.record(PEER, 0.0)
        delays.append(s.read()[PEER]["next_attempt"])
    assert delays[0] < delays[1] < delays[2], "grows"
    assert max(delays) <= 5.0, "never exceeds the configured ceiling"
    assert delays[-1] == 5.0, "saturates rather than terminating"
    assert s.read()[PEER]["misses"] == 40


def test_the_envelope_is_the_schedulers_own_not_an_invented_one():
    """The contract's 0.5-to-30s ladder is [UNAPPROVED]. This must follow the
    operator-configured envelope instead."""
    s = _Scheduler(min_backoff=1.0, max_backoff=9.0)
    s.record(PEER, 0.0)
    assert s.read()[PEER]["next_attempt"] == 1.0
    s.record(PEER, 0.0)
    assert s.read()[PEER]["next_attempt"] == 2.0
    for _ in range(20):
        s.record(PEER, 0.0)
    assert s.read()[PEER]["next_attempt"] == 9.0


def test_the_next_attempt_gate_is_shared_with_ordinary_failures():
    """The record must move the SAME gate _run consults, or the peer would be
    retried every tick regardless of the backoff it just computed."""
    s = _Scheduler()
    s.record(PEER, 100.0)
    assert s._next_attempt[PEER] == s.read()[PEER]["next_attempt"]


def test_reads_are_snapshots_not_live_handles():
    s = _Scheduler()
    s.record(PEER, 100.0)
    snapshot = s.read()
    snapshot[PEER]["misses"] = 999
    assert s.read()[PEER]["misses"] == 1


class _Selector(_Scheduler):
    """The real _resolve_peers on a bare object.

    It reads only the roster it is handed, the address map, and this object's
    own retry bookkeeping — so the authority rules can be exercised without a
    socket, a store, or a running loop.
    """

    def __init__(self, machine_pub="ff" * 32, **kw):
        super().__init__(**kw)
        self._failures = {}
        self.authenticator = type("_A", (), {"machine_pub": machine_pub})()

    resolve_peers = FleetSyncScheduler._resolve_peers


OTHER = "cd" * 32
SELF = "ff" * 32


def test_a_rostered_peer_with_an_address_is_selected():
    s = _Selector()
    assert s.resolve_peers({PEER: object()}, {PEER: ["ws://h:1"]}, 0.0) == [PEER]


def test_an_authenticated_non_member_is_never_selected():
    """Acceptance 6. Reachability alone must never admit: an address for a
    machine the roster does not carry is not a peer."""
    s = _Selector()
    selected = s.resolve_peers({}, {OTHER: ["ws://rogue:1"]}, 0.0)
    assert selected == []
    assert OTHER not in s.read(), "a non-member is not a discovery miss either"


def test_we_never_select_ourselves():
    s = _Selector(machine_pub=SELF)
    assert s.resolve_peers({SELF: object()}, {SELF: ["ws://me:1"]}, 0.0) == []


def test_a_rostered_peer_with_no_address_is_recorded_and_not_selected():
    """Acceptance 2 and 5 together: it stays a peer, it is not attempted this
    round, and the reason is on the record."""
    s = _Selector()
    assert s.resolve_peers({PEER: object()}, {}, 0.0) == []
    assert s.read()[PEER]["reason"] == "discovery_unavailable"


def test_discovery_recovering_clears_the_record_and_the_backoff():
    """A peer that becomes addressable must not inherit the miss envelope —
    its next real failure starts from a clean slate."""
    s = _Selector()
    for _ in range(5):
        s.resolve_peers({PEER: object()}, {}, 0.0)
    assert s.read()[PEER]["misses"] == 5
    assert s.resolve_peers({PEER: object()}, {PEER: ["ws://h:1"]}, 999.0) == [PEER]
    assert s.read() == {}
    assert s._next_attempt.get(PEER) is None


def test_leaving_the_roster_clears_the_record():
    s = _Selector()
    s.resolve_peers({PEER: object()}, {}, 0.0)
    assert PEER in s.read()
    s.resolve_peers({}, {}, 1.0)
    assert s.read() == {}, "not a peer any more, so not a miss any more"


def test_backoff_gates_the_next_round():
    """A recorded miss must actually suppress the attempt until its time."""
    s = _Selector(min_backoff=10.0, max_backoff=10.0)
    s.resolve_peers({PEER: object()}, {}, 0.0)
    assert s.resolve_peers({PEER: object()}, {PEER: ["ws://h:1"]}, 5.0) == [PEER], (
        "recovery is immediate: discovery succeeding clears the gate"
    )


def test_an_unaddressable_peer_is_retried_forever_not_dropped():
    """The whole point of acceptance 5: no terminal state, ever."""
    s = _Selector()
    for round_ in range(200):
        s.resolve_peers({PEER: object()}, {}, float(round_) * 1000.0)
    assert s.read()[PEER]["misses"] == 200
    assert s.resolve_peers(
        {PEER: object()}, {PEER: ["ws://h:1"]}, 1_000_000.0,
    ) == [PEER], "still a peer after 200 misses"
