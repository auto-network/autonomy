from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tools.network.accounting import (
    BATCH_INTERVAL_SECONDS,
    UsageBatch,
    UsageBatchMalformed,
    UsageBatchSignatureError,
)
from tools.network.idkit import KeyPair, canonical_json

ORG = "11111111-1111-4111-8111-111111111111"
MIXED_CASE_ORG = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
START = 1_756_000_000 - (1_756_000_000 % BATCH_INTERVAL_SECONDS)
END = START + BATCH_INTERVAL_SECONDS
SIGNER = KeyPair.from_private_hex("01" * 32)
FIXTURE = Path(__file__).parent / "fixtures" / "usage_batch_v1.json"


def _batch(**overrides) -> UsageBatch:
    values = {
        "signer": SIGNER,
        "organization_id": ORG,
        "sequence": 7,
        "interval_start": START,
        "interval_end": END,
        "created_at": END + 4,
        "counters": {
            "relay.egress_bytes": 12_345,
            "relay.viewer_seconds": 300,
        },
    }
    values.update(overrides)
    return UsageBatch.create(**values)


def test_deterministic_v1_vector_is_byte_exact():
    vector = json.loads(FIXTURE.read_text())
    batch = UsageBatch.create(
        signer=KeyPair.from_private_hex(vector["private_key_hex"]),
        **vector["input"],
    )
    assert batch.producer == vector["expected"]["producer"]
    assert batch.batch_id == vector["expected"]["batch_id"]
    assert batch.checksum == vector["expected"]["checksum"]
    assert batch.signature == vector["expected"]["signature"]
    assert batch.to_json().decode("ascii") == vector["expected"]["wire"]
    assert UsageBatch.from_json(batch.to_json()) == batch


def test_identical_creation_is_identical_wire_and_retry_parse_is_stable():
    first = _batch()
    second = _batch()
    assert first.to_json() == second.to_json()
    assert UsageBatch.from_json(first.to_json()).to_json() == first.to_json()


def test_same_logical_identity_with_different_body_is_a_sink_conflict_shape():
    first = _batch()
    changed = _batch(counters={"relay.egress_bytes": 12_346})
    assert changed.batch_id == first.batch_id
    assert changed.checksum != first.checksum
    assert changed.to_json() != first.to_json()


def test_counter_tamper_fails_checksum_before_it_can_be_counted():
    wire = _batch().to_json()
    tampered = wire.replace(b"12345", b"12346")
    assert tampered != wire
    with pytest.raises(UsageBatchMalformed, match="checksum"):
        UsageBatch.from_json(tampered)


def test_recomputed_checksum_without_producer_key_fails_signature():
    batch = _batch()
    raw = batch.to_dict()
    replacement = _batch(counters={"relay.egress_bytes": 99})
    raw["counters"] = replacement.to_dict()["counters"]
    raw["checksum"] = replacement.checksum
    # Logical identity is unchanged, so the replacement checksum is valid for
    # this body. The genuine signature still covers the original checksum.
    assert raw["batch_id"] == replacement.batch_id
    with pytest.raises(UsageBatchSignatureError):
        UsageBatch.from_json(canonical_json(raw))


def test_wire_rejects_unknown_or_identity_bearing_top_level_fields():
    raw = _batch().to_dict()
    for field in ("member_id", "session_id", "token", "source_address", "path"):
        changed = dict(raw)
        changed[field] = "forbidden"
        with pytest.raises(UsageBatchMalformed, match="fields do not match"):
            UsageBatch.from_json(canonical_json(changed))


def test_wire_rejects_whitespace_duplicates_non_ascii_and_noncanonical_numbers():
    wire = _batch().to_json()
    with pytest.raises(UsageBatchMalformed, match="not canonical"):
        UsageBatch.from_json(b" " + wire)

    duplicate = b'{"v":1,' + wire[1:]
    with pytest.raises(UsageBatchMalformed, match="duplicate"):
        UsageBatch.from_json(duplicate)

    with pytest.raises(UsageBatchMalformed, match="ASCII"):
        UsageBatch.from_json("{\"x\":\"café\"}")

    changed = wire.replace(b'"sequence":7', b'"sequence":7.0')
    with pytest.raises(UsageBatchMalformed):
        UsageBatch.from_json(changed)


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"organization_id": "ORG"}, "canonical UUID"),
        ({"organization_id": MIXED_CASE_ORG.upper()}, "canonical UUID"),
        ({"sequence": 0}, "sequence"),
        ({"sequence": True}, "sequence"),
        ({"interval_start": START + 1, "interval_end": END + 1}, "epoch-aligned"),
        ({"interval_end": END + 1}, "exactly 300"),
        ({"created_at": END - 1}, "cannot precede"),
        ({"counters": {}}, "1 to 64"),
        ({"counters": {"relay.egress_bytes": 0}}, "positive physical usage"),
        ({"counters": {"Relay Bytes": 1}}, "counter name"),
        ({"counters": {"relay.egress_bytes": -1}}, "counter relay.egress_bytes"),
        ({"counters": {"relay.egress_bytes": True}}, "counter relay.egress_bytes"),
    ],
)
def test_creation_rejects_invalid_identity_interval_and_counters(overrides, match):
    with pytest.raises(UsageBatchMalformed, match=match):
        _batch(**overrides)


def test_version_and_signature_encodings_are_strict():
    raw = _batch().to_dict()
    raw["v"] = 2
    with pytest.raises(UsageBatchMalformed, match="version"):
        UsageBatch.from_json(canonical_json(raw))

    raw = _batch().to_dict()
    raw["signature"] = "A" * 128
    with pytest.raises(UsageBatchMalformed, match="signature"):
        UsageBatch.from_json(canonical_json(raw))


def test_batch_and_counter_mapping_are_immutable():
    batch = _batch()
    with pytest.raises(FrozenInstanceError):
        batch.sequence = 8  # type: ignore[misc]
    with pytest.raises(TypeError):
        batch.counters["relay.egress_bytes"] = 1  # type: ignore[index]


def test_at_most_64_physical_counter_families_fit_one_batch():
    counters = {f"relay.metric_{i}": i + 1 for i in range(64)}
    batch = _batch(counters=counters)
    assert len(batch.counters) == 64
    with pytest.raises(UsageBatchMalformed, match="1 to 64"):
        _batch(counters={**counters, "relay.one_more": 65})


def test_batch_contains_only_organization_level_physical_usage():
    wire = _batch().to_json()
    decoded = json.loads(wire)
    assert set(decoded) == {
        "v",
        "batch_id",
        "producer",
        "organization_id",
        "sequence",
        "interval_start",
        "interval_end",
        "created_at",
        "counters",
        "checksum",
        "signature",
    }
    for forbidden in (
        b"member",
        b"session",
        b"token",
        b"source",
        b"address",
        b"allocation",
        b"candidate",
        b"link",
        b"path",
    ):
        assert forbidden not in wire
