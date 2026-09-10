"""An old connection attempt must never cancel a live one (auto-ieh3l §10.8).

Contract graph://7ed8a519-356 section 9: "a stale accept, a late descriptor, or
an old disconnect closes only its captured generation: it cannot unregister a
replacement slot, replace a newer descriptor, or cancel the peer's current
work."

Fencing rules agreed with auto-fh2nv 2026-09-10: pair ids are fresh 128-bit
random values, never reused and not monotonic, so connections fence by
EQUALITY; descriptor generations are monotonic, so they fence by ORDERING.
"""

from __future__ import annotations

import pytest

from tools.network.fleet_peer_path import (
    IGNORE,
    RESET_OPEN_TIMEOUT,
    RESET_PEER_CLOSED,
    RESET_PROTOCOL,
    RESET_TUNNEL_LOST,
    RETRY,
    STAND_DOWN,
    STOP,
    PeerPathController,
)

PEER = "ab" * 32
MINE = "pair-live"
DEAD = "pair-dead"


def _controller():
    return PeerPathController(authority_domain="personal", peer_machine_pub=PEER)


def test_a_reset_for_a_dead_pair_cannot_clear_the_live_one():
    """THE defect this exists to prevent: a slow callback from a replaced
    attempt must not take down the connection that replaced it."""
    c = _controller()
    c.opened(MINE)
    assert c.reset(DEAD, RESET_TUNNEL_LOST) == IGNORE
    assert c.pair_id == MINE, "the live pair survived a stale reset"


def test_a_reset_for_a_pair_we_never_opened_is_ignored():
    c = _controller()
    assert c.reset("someone-elses", RESET_TUNNEL_LOST) == IGNORE
    assert c.pair_id is None


def test_our_own_reset_clears_the_pair():
    c = _controller()
    c.opened(MINE)
    assert c.reset(MINE, RESET_TUNNEL_LOST) == RETRY
    assert c.pair_id is None, "we are no longer holding a connection"


@pytest.mark.parametrize("code", [RESET_TUNNEL_LOST, RESET_OPEN_TIMEOUT])
def test_transport_failures_retry(code):
    """6 and 2 are the transport saying 'not now'. Not retrying turns a
    recoverable outage into a permanent one."""
    c = _controller()
    c.opened(MINE)
    assert c.reset(MINE, code) == RETRY


@pytest.mark.parametrize("code", [RESET_PEER_CLOSED, RESET_PROTOCOL])
def test_deliberate_and_protocol_closes_do_not_retry(code):
    """1 and 7 are the peer or the protocol saying 'stop'. Retrying hammers a
    peer that meant to hang up, or repeats a malformed exchange."""
    c = _controller()
    c.opened(MINE)
    assert c.reset(MINE, code) == STOP


def test_losing_the_race_stands_down_rather_than_cancelling():
    """Another attempt already holds this peer. By the one-controller-per-peer
    guarantee it is not ours, so we must not reach into it."""
    c = _controller()
    assert c.open_refused("operation-already-open") == STAND_DOWN
    assert c.pair_id is None


def test_any_other_refusal_is_our_failure_and_retries():
    c = _controller()
    assert c.open_refused("no-such-destination") == RETRY


def test_a_late_descriptor_cannot_replace_a_newer_one():
    c = _controller()
    assert c.descriptor(7) is True
    assert c.descriptor(4) is False, "an older generation arrived late"
    assert c.descriptor_generation == 7


def test_the_same_descriptor_generation_is_not_newer():
    c = _controller()
    assert c.descriptor(7) is True
    assert c.descriptor(7) is False
    assert c.descriptor(8) is True


def test_connections_fence_by_equality_not_ordering():
    """Pair ids are random, so 'later' is meaningless — only 'the one I hold'
    counts. A lexically larger id from a dead pair must still be ignored."""
    c = _controller()
    c.opened("aaaa")
    assert c.reset("zzzz", RESET_TUNNEL_LOST) == IGNORE
    assert c.pair_id == "aaaa"


def test_reopening_after_a_reset_fences_on_the_new_pair():
    c = _controller()
    c.opened(MINE)
    c.reset(MINE, RESET_TUNNEL_LOST)
    c.opened("pair-second")
    assert c.reset(MINE, RESET_TUNNEL_LOST) == IGNORE, "the first pair is dead"
    assert c.pair_id == "pair-second"


def test_a_pair_id_is_required():
    with pytest.raises(ValueError):
        _controller().opened("")
