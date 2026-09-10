"""A rostered peer that publishes only a relay locator is still attempted.

auto-e38g4, closing the boundary auto-ew9wf named: its fallback covers "every
discovered address FAILED", not "no address exists". ``_resolve_peers`` recorded
an address-less peer as ``discovery_unavailable`` and never attempted it -- and
a peer behind NAT that publishes nothing but a serving slot is precisely the
peer a relay exists for.
"""

from __future__ import annotations

import pytest

from tools.network import fleet_sync_scheduler as fss

PEER = "33" * 32
OWN = "11" * 32
PERSONA = "ab" * 32
SLOT = "cc" * 32


def _locator():
    return {
        "relay_base": "wss://auto.network",
        "org_uuid": "11111111-1111-4111-8111-111111111111",
        "persona_pub": PERSONA,
        "serving_machine_pub": SLOT,
    }


class _Authenticator:
    machine_pub = OWN


class _Scheduler:
    """Just enough of the scheduler to exercise selection: _resolve_peers reads
    the authenticator's own key, the backoff config and its own two maps."""

    def __init__(self, locators_provider=None):
        self.authenticator = _Authenticator()
        self.config = fss.FleetSyncRuntimeConfig(
            machine_key=None, personal_root_pub="99" * 32,
            roster_entries=lambda: (), peer_addresses=lambda: {},
            personal_db_path=None,
            peer_relay_locators=locators_provider,
        )
        self._discovery_unavailable = {}
        self._failures = {}
        self._next_attempt = {}

    resolve = fss.FleetSyncScheduler._resolve_peers
    record = fss.FleetSyncScheduler._record_discovery_unavailable
    read_locators = fss.FleetSyncScheduler._peer_relay_locators

    def _record_discovery_unavailable(self, *a, **kw):
        return _Scheduler.record(self, *a, **kw)


def _resolve(scheduler, addresses, locators=None):
    return _Scheduler.resolve(
        scheduler, {OWN: None, PEER: None}, addresses, 1000.0, locators)


def test_a_peer_with_neither_is_recorded_and_not_attempted():
    scheduler = _Scheduler()

    assert _resolve(scheduler, {}, {}) == []
    state = scheduler._discovery_unavailable[PEER]
    assert state["direct_absent"] is True
    # Now that a locator map is actually consulted, the record can say the
    # locator was missing. It stayed silent about that for as long as there
    # was nothing to consult.
    assert state["relay_absent"] is True


def test_a_peer_with_only_a_locator_is_attempted():
    scheduler = _Scheduler()

    assert _resolve(scheduler, {}, {PEER: _locator()}) == [PEER]
    assert PEER not in scheduler._discovery_unavailable


def test_a_locator_arriving_later_clears_the_miss_and_its_backoff():
    """The peer was unaddressable, backed off, then published a slot. The next
    round must not still be serving the old backoff."""
    scheduler = _Scheduler()
    _resolve(scheduler, {}, {})
    assert scheduler._next_attempt[PEER] > 1000.0

    assert _resolve(scheduler, {}, {PEER: _locator()}) == [PEER]
    assert PEER not in scheduler._next_attempt


def test_a_direct_address_alone_is_unchanged():
    scheduler = _Scheduler()

    assert _resolve(scheduler, {PEER: ["ws://10.0.0.2:9410"]}, {}) == [PEER]
    assert scheduler._discovery_unavailable == {}


def test_a_caller_that_consults_no_locator_source_asserts_nothing():
    """_record_discovery_unavailable must not claim an absence nobody looked
    for -- the reason the field was withheld in the first place."""
    scheduler = _Scheduler()

    scheduler._record_discovery_unavailable(PEER, 1000.0)

    assert "relay_absent" not in scheduler._discovery_unavailable[PEER]


def test_a_failing_locator_source_does_not_stop_the_round():
    def boom():
        raise RuntimeError("reachability cache exploded")

    scheduler = _Scheduler(boom)

    assert _Scheduler.read_locators(scheduler) == {}


def test_no_locator_source_configured_reads_empty():
    assert _Scheduler.read_locators(_Scheduler()) == {}
