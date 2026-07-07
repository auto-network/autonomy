"""D5-13 tests — a retried publish short-circuits to a no-op, no second event."""

from __future__ import annotations

import pytest

from tools.dashboard.commit_broker.idempotency import (
    NOOP_KEY_ALREADY_FINALIZED,
    NOOP_REF_ALREADY_PUBLISHED,
    PROCEED,
    decide_publish,
)

SIGNED = "s" * 40
KEY = "idem-key-1"


def _decide(*, finalized=False, tip=None):
    return decide_publish(
        idempotency_key=KEY,
        signed_commit_sha=SIGNED,
        key_already_finalized=lambda k: finalized,
        read_remote_tip=lambda: tip,
    )


def test_first_publish_proceeds():
    d = _decide(finalized=False, tip=None)
    assert d.proceed is True
    assert d.reason == PROCEED
    assert d.is_noop is False


def test_retry_with_finalized_key_is_noop():
    d = _decide(finalized=True, tip=None)
    assert d.proceed is False
    assert d.reason == NOOP_KEY_ALREADY_FINALIZED
    assert d.is_noop is True


def test_ref_already_at_signed_sha_is_noop_even_if_event_not_recorded():
    # key NOT finalized (first attempt's event not durable yet) but the remote
    # ref already shows signed_commit_sha -> the push already happened.
    d = _decide(finalized=False, tip=SIGNED)
    assert d.proceed is False
    assert d.reason == NOOP_REF_ALREADY_PUBLISHED


def test_ref_at_different_sha_still_proceeds():
    d = _decide(finalized=False, tip="d" * 40)
    assert d.proceed is True
    assert d.reason == PROCEED


def test_key_guard_takes_precedence_over_ref_read():
    # If the key is finalized we must not even need the remote read to decide.
    read_calls = []

    def reader():
        read_calls.append(1)
        return SIGNED

    d = decide_publish(
        idempotency_key=KEY,
        signed_commit_sha=SIGNED,
        key_already_finalized=lambda k: True,
        read_remote_tip=reader,
    )
    assert d.reason == NOOP_KEY_ALREADY_FINALIZED
    assert read_calls == []  # short-circuited before the remote read


def test_requires_key_and_signed_sha():
    with pytest.raises(ValueError):
        decide_publish(
            idempotency_key="", signed_commit_sha=SIGNED,
            key_already_finalized=lambda k: False, read_remote_tip=lambda: None,
        )
    with pytest.raises(ValueError):
        decide_publish(
            idempotency_key=KEY, signed_commit_sha="",
            key_already_finalized=lambda k: False, read_remote_tip=lambda: None,
        )
