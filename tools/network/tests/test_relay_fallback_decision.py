"""Direct exhausted is not the peer being unreachable (auto-ew9wf).

`_pull_scope` used to raise FleetSyncPeerUnreachable the moment every direct
address failed. The relay carrier now exists, so that moment becomes a
fallback — but only that moment: direct stays first, and a working direct path
is never displaced.

The dashboard cannot dial the relay itself. `fleet_relay_connect` needs
`connector.fleet_streams`, which lives in the CONNECTOR process, and the
process that pulls is not the process that holds the adapter (measured on
sjc-2 2026-09-10: the dashboard logs "fleet sync pull", the connector logs
"PULL REQUEST from"). So the pull is DELEGATED and one controller is kept on
the deciding side.
"""

from __future__ import annotations

import asyncio
import types

from tools.network import fleet_peer_path as fpp
from tools.network.fleet_sync_scheduler import FleetSyncScheduler

PEER = "ab" * 32


class _Refused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class _Closed(Exception):
    def __init__(self, pair_id, code):
        super().__init__(f"{pair_id}:{code}")
        self.pair_id = pair_id
        self.code = code


class _Sched:
    """The real fallback logic on a bare object."""

    def __init__(self, delegate=None):
        self._peer_paths = {}
        self._relay_hold = {}
        self._failures = {}
        # SimpleNamespace, not a class attribute: a plain function stored on a
        # CLASS binds as a method and would receive config as self. The real
        # FleetSyncRuntimeConfig is a dataclass, so relay_pull is an instance
        # attribute and no binding happens — the stub has to match that or it
        # tests a calling convention production does not use.
        self.config = types.SimpleNamespace(
            relay_pull=delegate, min_backoff=0.25, max_backoff=5.0,
        )

    _relay_fallback = FleetSyncScheduler._relay_fallback
    _backoff_delay = FleetSyncScheduler._backoff_delay
    # Already a plain function when read off the class in 3.12; no __func__.
    _relay_decision = staticmethod(FleetSyncScheduler._relay_decision)


def _run(coro):
    return asyncio.run(coro)


def test_no_delegate_configured_means_no_fallback():
    """The connector's own scheduler sets no relay_pull, so it must behave
    exactly as before rather than delegating to itself."""
    s = _Sched(delegate=None)
    assert _run(s._relay_fallback(PEER, "personal", RuntimeError("direct"))) is None


def test_a_successful_delegation_returns_the_connectors_outcome():
    async def delegate(**kwargs):
        assert kwargs["peer_machine_pub"] == PEER
        assert kwargs["scope"] == "autonomy"
        assert kwargs["operation_id"]
        return {"outcome": "ok", "transactions": 4}

    s = _Sched(delegate=delegate)
    got = _run(s._relay_fallback(PEER, "autonomy", RuntimeError("direct")))
    assert got == {"outcome": "ok", "transactions": 4}


def test_the_operation_id_is_minted_here_not_by_the_connector():
    """One controller: the deciding side mints the id, the connector is told."""
    seen = []

    async def delegate(**kwargs):
        seen.append(kwargs["operation_id"])
        return {"outcome": "ok"}

    s = _Sched(delegate=delegate)
    _run(s._relay_fallback(PEER, "personal", RuntimeError("d")))
    _run(s._relay_fallback(PEER, "personal", RuntimeError("d")))
    assert len(seen) == 2 and seen[0] != seen[1], "a fresh id per operation"
    assert all(seen), "never empty"


def test_one_controller_per_peer_is_reused():
    async def delegate(**kwargs):
        return {"outcome": "ok"}

    s = _Sched(delegate=delegate)
    _run(s._relay_fallback(PEER, "personal", RuntimeError("d")))
    first = s._peer_paths[PEER]
    _run(s._relay_fallback(PEER, "autonomy", RuntimeError("d")))
    assert s._peer_paths[PEER] is first, "same peer, same controller"


def test_a_refusal_stands_down_and_does_not_mask_the_direct_error():
    async def delegate(**kwargs):
        raise _Refused("operation-already-open")

    s = _Sched(delegate=delegate)
    assert _run(s._relay_fallback(PEER, "personal", RuntimeError("direct"))) is None


def test_the_three_carrier_failures_map_to_three_decisions():
    """Collapsing them would turn losing a race into a retry storm."""
    s = _Sched()
    c = fpp.PeerPathController(authority_domain="personal", peer_machine_pub=PEER)
    assert s._relay_decision(c, _Refused("operation-already-open")) == fpp.STAND_DOWN
    c.opened("pair-1")
    assert s._relay_decision(c, _Closed("pair-1", fpp.RESET_TUNNEL_LOST)) == fpp.RETRY
    c.opened("pair-2")
    assert s._relay_decision(c, _Closed("pair-2", fpp.RESET_ORDERLY)) == fpp.STOP
    assert s._relay_decision(c, ConnectionError("no tunnel")) == fpp.RETRY


def test_a_reset_for_someone_elses_pair_is_ignored_by_the_controller():
    """The fence lives in the controller, and the delegated path cannot reach
    it: a pair id read back under OUR operation id is ours by construction, so
    `_relay_decision` adopts it. This asserts the fence itself, directly."""
    c = fpp.PeerPathController(authority_domain="personal", peer_machine_pub=PEER)
    c.opened("mine")
    assert c.reset("theirs", fpp.RESET_TUNNEL_LOST) == fpp.IGNORE
    assert c.pair_id == "mine", "a stale reset did not clear the live pair"


def _counting(exc=None):
    calls = []

    async def delegate(**kwargs):
        calls.append(kwargs["operation_id"])
        if exc is not None:
            raise exc
        return {"outcome": "ok"}

    return delegate, calls


def test_a_stand_down_stops_the_next_pull_from_delegating_at_all():
    """The decision changes SCHEDULING, not only the log line. Another attempt
    holds this peer, so a second delegation would be the second concurrent
    open this bead promised never to create."""
    delegate, calls = _counting(_Refused("operation-already-open"))
    s = _Sched(delegate=delegate)
    assert _run(s._relay_fallback(PEER, "personal", RuntimeError("d"))) is None
    assert len(calls) == 1
    assert s._relay_hold[PEER][0] == fpp.STAND_DOWN
    assert _run(s._relay_fallback(PEER, "personal", RuntimeError("d"))) is None
    assert len(calls) == 1, "the relay was not dialed again while held off"


def test_a_stop_holds_the_relay_off_and_a_reset_can_reach_that_decision():
    """Code 1 is the peer saying do not come back. The controller never sees
    `opened()` on this path — the pair is opened in the connector — so without
    adopting the reported pair id every code would decide IGNORE and no stop
    could ever be reached from here."""
    delegate, calls = _counting(_Closed("pair-9", fpp.RESET_ORDERLY))
    s = _Sched(delegate=delegate)
    _run(s._relay_fallback(PEER, "personal", RuntimeError("d")))
    assert s._relay_hold[PEER][0] == fpp.STOP
    _run(s._relay_fallback(PEER, "personal", RuntimeError("d")))
    assert len(calls) == 1


def test_a_retryable_failure_leaves_the_relay_available_next_round():
    """A transport fault and a retryable reset must NOT hold the relay off:
    the peer's own next-attempt envelope already governs when the round comes
    back, and holding here too would double the wait."""
    for exc in (ConnectionError("no tunnel"),
                _Closed("pair-3", fpp.RESET_TUNNEL_LOST)):
        delegate, calls = _counting(exc)
        s = _Sched(delegate=delegate)
        _run(s._relay_fallback(PEER, "personal", RuntimeError("d")))
        assert PEER not in s._relay_hold, exc
        _run(s._relay_fallback(PEER, "personal", RuntimeError("d")))
        assert len(calls) == 2, exc


def test_the_hold_expires_and_a_success_clears_it():
    delegate, calls = _counting()
    s = _Sched(delegate=delegate)
    s._relay_hold[PEER] = (fpp.STOP, 0.0)          # already in the past
    assert _run(s._relay_fallback(PEER, "personal", RuntimeError("d"))) == {
        "outcome": "ok"}
    assert len(calls) == 1
    assert PEER not in s._relay_hold


def test_the_hold_uses_the_schedulers_own_retry_envelope():
    """One envelope, not two: a second formula here would drift away from the
    peer's next attempt the first time either is tuned."""
    s = _Sched()
    assert s._backoff_delay(1) == 0.25
    assert s._backoff_delay(2) == 0.5
    assert s._backoff_delay(50) == 5.0, "saturates at max_backoff"


def test_a_non_mapping_outcome_is_refused_rather_than_trusted():
    async def delegate(**kwargs):
        return "ok"

    s = _Sched(delegate=delegate)
    assert _run(s._relay_fallback(PEER, "personal", RuntimeError("d"))) is None
