"""The direct dial caller tries a peer's recorded address first.

`fleet_candidate_order.order_candidates` is unit-tested on its own. What
these cover is the WIRING in `FleetSyncScheduler._run`: that the ordered
sequence is what reaches `_sync_peer`, that one telemetry read serves the
whole round, and that a telemetry failure leaves dialing exactly as it
was. `_run` is driven directly against a minimal stub so the assertions
are about the call site and nothing else.
"""

from __future__ import annotations

import asyncio
import random
import time
import types

from tools.network import fleet_sync_scheduler as fss

PEER = "aa" * 32
SELF = "bb" * 32
TAILNET = "ws://100.122.70.30:9410"
BRIDGE = "ws://172.16.0.2:9410"
#: The call site does not inject a clock, so `order_candidates` compares
#: against `time.time_ns()`. A fixed constant here is dated either ahead
#: of the real clock (rejected by the future guard) or outside the decay
#: window; derive it from the real clock instead.
def _recently():
    return time.time_ns() - 1_000_000_000


def _row(address, at_ns=None):
    at_ns = _recently() if at_ns is None else at_ns
    return {"peer": PEER, "channel": "personal", "direction": "pull",
            "scope": "personal",
            "payload": {"last_success_address": address,
                        "last_success_at_ns": at_ns}}


def _run_one_round(monkeypatch, rows, addresses=(TAILNET, BRIDGE),
                   reads=None):
    """Drive one round of `_run` and return what `_sync_peer` received."""
    seen: list[tuple[str, tuple[str, ...]]] = []
    stopping = asyncio.Event()

    async def fake_sync_peer(machine_pub, addrs):
        seen.append((machine_pub, tuple(addrs)))
        stopping.set()          # one round only

    async def fake_sync_org_peers(own_fleet, now):
        return None

    def read_channel_rows(*a, **k):
        if reads is not None:
            reads.append(1)
        if isinstance(rows, Exception):
            raise rows
        return rows

    monkeypatch.setattr(fss, "resolve", lambda entries, anchor_root_pub: {PEER: object()})
    monkeypatch.setattr(
        "tools.network.fleet_sync_telemetry.read_channel_rows", read_channel_rows
    )

    stub = types.SimpleNamespace(
        _stopping=stopping,
        _roster_snapshot=(),
        _next_attempt={},
        _rng=random.Random(0),
        _last_round_selection=(),
        authenticator=types.SimpleNamespace(machine_pub=SELF),
        store=types.SimpleNamespace(peer_last_success=lambda: {}),
        config=types.SimpleNamespace(
            personal_root_pub="cc" * 32,
            peer_addresses=lambda: {PEER: tuple(addresses)},
            max_concurrent_pulls=4,
            poll_interval=0.01,
        ),
        _sync_peer=fake_sync_peer,
        _sync_org_peers=fake_sync_org_peers,
    )
    asyncio.run(fss.FleetSyncScheduler._run(stub))
    return seen


def test_the_recorded_address_reaches_the_dial_caller_first(monkeypatch):
    """THE ONE THAT MATTERS: ordering is applied where dialing happens."""
    seen = _run_one_round(monkeypatch, [_row(BRIDGE)])

    assert seen == [(PEER, (BRIDGE, TAILNET))]


def test_without_a_record_the_existing_order_is_untouched(monkeypatch):
    """No hint is not a negative hint; the existing rank still decides."""
    seen = _run_one_round(monkeypatch, [])

    assert seen == [(PEER, (TAILNET, BRIDGE))]


def test_a_telemetry_failure_does_not_break_the_round(monkeypatch):
    """Ordering must never be able to stop a peer being dialed."""
    seen = _run_one_round(monkeypatch, RuntimeError("settings unavailable"))

    assert seen == [(PEER, (TAILNET, BRIDGE))]


def test_telemetry_is_read_once_per_round_not_once_per_peer(monkeypatch):
    """The read is a blocking store call; it belongs outside the gather."""
    reads: list[int] = []
    _run_one_round(monkeypatch, [_row(BRIDGE)], reads=reads)

    assert len(reads) == 1


def test_no_candidate_is_added_or_dropped_on_the_dial_path(monkeypatch):
    """Whatever the hint says, the peer keeps exactly its own addresses."""
    for rows in ([_row(BRIDGE)], [_row("ws://10.0.0.9:9410")], []):
        seen = _run_one_round(monkeypatch, rows)
        assert sorted(seen[0][1]) == sorted((TAILNET, BRIDGE))


def test_a_hint_older_than_the_window_is_not_promoted_on_the_dial_path(monkeypatch):
    """The 24-hour decay policy applies at the call site, not just in the
    unit under it: the call site injects no clock, so this exercises the
    real `time.time_ns()` comparison."""
    from tools.network import fleet_candidate_order as fco

    stale = time.time_ns() - fco.DEFAULT_MAX_AGE_NS - 1_000_000_000
    seen = _run_one_round(monkeypatch, [_row(BRIDGE, at_ns=stale)])

    assert seen == [(PEER, (TAILNET, BRIDGE))]


def test_a_hint_dated_ahead_of_the_clock_is_not_promoted_on_the_dial_path(monkeypatch):
    """A clock jump must not pin an address at the front of the dial order."""
    ahead = time.time_ns() + 60_000_000_000
    seen = _run_one_round(monkeypatch, [_row(BRIDGE, at_ns=ahead)])

    assert seen == [(PEER, (TAILNET, BRIDGE))]
