from __future__ import annotations

import pytest

from tools.network.accounting import (
    BATCH_INTERVAL_SECONDS,
    UsageProgress,
    UsageProgressMalformed,
    UsageProgressSignatureError,
)
from tools.network.idkit import KeyPair, canonical_json

ORG = "55555555-5555-4555-8555-555555555555"
SIGNER = KeyPair.from_private_hex("05" * 32)
CLOSED = 1_756_000_000 - (1_756_000_000 % BATCH_INTERVAL_SECONDS)


def _progress(**overrides) -> UsageProgress:
    values = {
        "signer": SIGNER,
        "organization_id": ORG,
        "sequence": 0,
        "closed_through": CLOSED,
        "created_at": CLOSED + 3,
    }
    values.update(overrides)
    return UsageProgress.create(**values)


def test_progress_is_deterministic_canonical_and_signed():
    first = _progress()
    second = _progress()
    assert first.to_json() == second.to_json()
    assert UsageProgress.from_json(first.to_json()) == first


def test_idle_progress_allows_zero_sequence_and_requires_aligned_closure():
    assert _progress().sequence == 0
    with pytest.raises(UsageProgressMalformed, match="epoch-aligned"):
        _progress(closed_through=CLOSED + 1)
    with pytest.raises(UsageProgressMalformed, match="cannot precede"):
        _progress(created_at=CLOSED - 1)


def test_progress_mutation_and_signature_forgery_fail_closed():
    progress = _progress()
    raw = progress.to_dict()
    raw["closed_through"] = CLOSED + BATCH_INTERVAL_SECONDS
    raw["created_at"] = raw["closed_through"] + 3
    with pytest.raises(UsageProgressMalformed, match="progress_id"):
        UsageProgress.from_json(canonical_json(raw))

    raw = progress.to_dict()
    raw["signature"] = "0" * 128
    with pytest.raises(UsageProgressSignatureError):
        UsageProgress.from_json(canonical_json(raw))


def test_progress_rejects_noncanonical_and_extra_identity_fields():
    wire = _progress().to_json()
    with pytest.raises(UsageProgressMalformed, match="canonical"):
        UsageProgress.from_json(b" " + wire)
    raw = _progress().to_dict()
    raw["session_id"] = "forbidden"
    with pytest.raises(UsageProgressMalformed, match="fields"):
        UsageProgress.from_json(canonical_json(raw))
