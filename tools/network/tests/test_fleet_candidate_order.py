"""Ordering a peer's dial candidates from locally recorded evidence.

Every test asserts on attempt ORDER, never on timing, and every test
asserts that the returned set of addresses is unchanged: ordering must
never be able to make a peer unreachable.
"""

from __future__ import annotations

from tools.network import fleet_candidate_order as fco

PEER = "a" * 64
OTHER_PEER = "b" * 64
TAILNET = "ws://100.122.70.30:9410"
PRIVATE = "ws://172.16.0.2:9410"
LAN = "ws://192.168.1.50:9410"
NOW = 10_000_000_000_000


def _row(peer, address, at_ns, **extra):
    payload = {"last_success_address": address, "last_success_at_ns": at_ns}
    payload.update(extra)
    return {"peer": peer, "channel": "c", "direction": "pull",
            "scope": "personal", "payload": payload}


def _order(candidates, rows, **kwargs):
    return fco.order_candidates(
        PEER, candidates, now_ns=NOW, read_rows=lambda **_: rows, **kwargs
    )


def test_recorded_address_is_tried_first():
    """Fixture 1: a fresh record for a current candidate moves it first."""
    result = _order([TAILNET, PRIVATE], [_row(PEER, PRIVATE, NOW - 1000)])
    assert result == [PRIVATE, TAILNET]


def test_remaining_order_is_preserved():
    """Fixture 6: promoting one candidate does not disturb the rest.

    The existing rank puts tailnet before private, and that must still
    hold among the candidates that were not promoted.
    """
    result = _order(
        [TAILNET, PRIVATE, LAN], [_row(PEER, LAN, NOW - 1000)]
    )
    assert result == [LAN, TAILNET, PRIVATE]


def test_recorded_address_no_longer_a_candidate_changes_nothing():
    """Fixture 2: the peer's address changed since we last reached it."""
    candidates = [TAILNET, PRIVATE]
    result = _order(candidates, [_row(PEER, "ws://10.0.0.9:9410", NOW - 1000)])
    assert result == candidates


def test_no_row_for_this_peer_changes_nothing():
    """Fixture 3: absence of a record is not a signal, positive or negative."""
    candidates = [TAILNET, PRIVATE]
    result = _order(candidates, [_row(OTHER_PEER, PRIVATE, NOW - 1000)])
    assert result == candidates


def test_empty_recorded_address_changes_nothing():
    """Fixture 4: an empty address string is not a record."""
    candidates = [TAILNET, PRIVATE]
    assert _order(candidates, [_row(PEER, "", NOW - 1000)]) == candidates


def test_telemetry_failure_cannot_break_dialing():
    """Fixture 5: a read failure degrades to the caller's order."""
    def boom(**_):
        raise RuntimeError("settings store unavailable")

    candidates = [TAILNET, PRIVATE]
    result = fco.order_candidates(
        PEER, candidates, now_ns=NOW, read_rows=boom
    )
    assert result == candidates


def test_stale_record_is_not_promoted():
    """Fixture 7: an address that stopped working is not tried first forever."""
    candidates = [TAILNET, PRIVATE]
    age = fco.DEFAULT_MAX_AGE_NS + 1
    assert _order(candidates, [_row(PEER, PRIVATE, NOW - age)]) == candidates


def test_record_just_inside_the_window_is_promoted():
    """The decay boundary admits a record exactly at the limit."""
    age = fco.DEFAULT_MAX_AGE_NS
    result = _order([TAILNET, PRIVATE], [_row(PEER, PRIVATE, NOW - age)])
    assert result == [PRIVATE, TAILNET]


def test_most_recent_record_wins_across_rows():
    """A peer has one row per channel and direction; the newest one decides."""
    rows = [
        _row(PEER, TAILNET, NOW - 5000),
        _row(PEER, PRIVATE, NOW - 1000),
    ]
    assert _order([TAILNET, PRIVATE], rows) == [PRIVATE, TAILNET]


def test_zero_timestamp_is_not_a_record():
    """A row that never recorded a success carries no ordering weight."""
    candidates = [TAILNET, PRIVATE]
    assert _order(candidates, [_row(PEER, PRIVATE, 0)]) == candidates


def test_malformed_rows_are_skipped():
    """Unexpected row shapes are ignored rather than raising."""
    rows = ["not a dict", {"peer": PEER}, {"peer": PEER, "payload": None},
            {"peer": PEER, "payload": {"last_success_address": PRIVATE,
                                       "last_success_at_ns": "later"}}]
    candidates = [TAILNET, PRIVATE]
    assert _order(candidates, rows) == candidates


def test_single_candidate_is_returned_unchanged():
    """Nothing to order, and the telemetry store is not consulted."""
    def fail(**_):
        raise AssertionError("telemetry must not be read for one candidate")

    assert fco.order_candidates(PEER, [TAILNET], read_rows=fail) == [TAILNET]
    assert fco.order_candidates(PEER, [], read_rows=fail) == []


def test_no_candidate_is_ever_dropped():
    """The returned set always equals the input set, in every branch."""
    candidates = [TAILNET, PRIVATE, LAN]
    for rows in (
        [_row(PEER, PRIVATE, NOW - 1000)],
        [_row(PEER, "ws://10.0.0.9:9410", NOW - 1000)],
        [_row(PEER, PRIVATE, NOW - fco.DEFAULT_MAX_AGE_NS - 1)],
        [],
    ):
        assert sorted(_order(candidates, rows)) == sorted(candidates)


def test_duplicate_candidate_is_not_dropped():
    """Promotion moves the first occurrence only and preserves length.

    The current caller deduplicates, so this cannot fire today. The
    guarantee that the returned list holds exactly the input entries
    must not depend on the caller.
    """
    candidates = [TAILNET, PRIVATE, TAILNET]
    result = _order(candidates, [_row(PEER, TAILNET, NOW - 1000)])
    assert result == [TAILNET, PRIVATE, TAILNET]
    assert len(result) == len(candidates)
    assert sorted(result) == sorted(candidates)
