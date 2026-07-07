"""D5-12 tests — force-with-lease pre-flight fails closed on any divergence."""

from __future__ import annotations

import pytest

from tools.dashboard.commit_broker.lease import (
    LEASE_MATCHES,
    NEW_REF_EXISTS,
    NEW_REF_OK,
    REF_ADVANCED,
    REF_UNEXPECTEDLY_ABSENT,
    check_new_ref,
    check_update_lease,
)

LEASE = "a" * 40
OTHER = "b" * 40


def _tip(value):
    return lambda: value


def test_lease_matches_proceeds():
    r = check_update_lease(expected_ref_sha=LEASE, read_remote_tip=_tip(LEASE))
    assert r.ok is True
    assert r.reason == LEASE_MATCHES
    assert r.observed_tip == LEASE
    assert r.routes_to_reapproval is False


def test_ref_advanced_fails_closed_and_reapproves():
    r = check_update_lease(expected_ref_sha=LEASE, read_remote_tip=_tip(OTHER))
    assert r.ok is False
    assert r.reason == REF_ADVANCED
    assert r.observed_tip == OTHER
    assert r.routes_to_reapproval is True


def test_ref_vanished_fails_closed_not_recreated():
    r = check_update_lease(expected_ref_sha=LEASE, read_remote_tip=_tip(None))
    assert r.ok is False
    assert r.reason == REF_UNEXPECTEDLY_ABSENT
    assert r.routes_to_reapproval is True


def test_update_lease_requires_expected_sha():
    with pytest.raises(ValueError):
        check_update_lease(expected_ref_sha="", read_remote_tip=_tip(LEASE))


def test_new_ref_absent_proceeds():
    r = check_new_ref(read_remote_tip=_tip(None))
    assert r.ok is True
    assert r.reason == NEW_REF_OK
    assert r.observed_tip is None


def test_new_ref_already_exists_fails_closed():
    r = check_new_ref(read_remote_tip=_tip(OTHER))
    assert r.ok is False
    assert r.reason == NEW_REF_EXISTS
    assert r.observed_tip == OTHER


def test_read_remote_tip_is_the_only_source_of_observed_state():
    # The decision must come from the injected read, not any ambient state.
    calls = []

    def reader():
        calls.append(1)
        return LEASE

    check_update_lease(expected_ref_sha=LEASE, read_remote_tip=reader)
    assert calls == [1]  # consulted exactly once
