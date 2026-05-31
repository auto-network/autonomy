"""Tests for the ``dashboard.feature_flags#1`` schema.

Pins the validation contract: required fields, type checks, rejection of
unknown fields. Companion to S0 (graph://40dd9d7a-23a).
"""

from __future__ import annotations

import pytest

from tools.graph.schemas import feature_flags
from tools.graph.schemas.registry import SchemaValidationError, validate_payload


# ── Fixtures ─────────────────────────────────────────────────


def _valid_payload() -> dict:
    return {
        "enabled": True,
        "description": "Gates the voice pipe canary at the WS endpoint.",
        "owner": "S3 voice pipe canary",
    }


# ── Schema registration ──────────────────────────────────────


def test_schema_registers_at_import():
    assert feature_flags.FEATURE_FLAGS_SET_ID == "dashboard.feature_flags"
    assert feature_flags.FEATURE_FLAGS_REVISION == 1


def test_synopsis_present():
    syn = feature_flags.SYNOPSIS
    assert "summary" in syn and syn["summary"]
    assert "nouns" in syn and isinstance(syn["nouns"], list) and syn["nouns"]
    assert "related_set_ids" in syn


# ── Valid payloads ───────────────────────────────────────────


def test_valid_enabled_true():
    feature_flags.FeatureFlagV1.validate(_valid_payload())


def test_valid_enabled_false():
    payload = _valid_payload() | {"enabled": False}
    feature_flags.FeatureFlagV1.validate(payload)


def test_valid_via_registry_validate_payload():
    validate_payload(
        feature_flags.FEATURE_FLAGS_SET_ID,
        feature_flags.FEATURE_FLAGS_REVISION,
        _valid_payload(),
    )


# ── Type rejection ───────────────────────────────────────────


def test_rejects_non_dict_payload():
    with pytest.raises(SchemaValidationError, match="payload must be a dict"):
        feature_flags.FeatureFlagV1.validate("not a dict")


def test_rejects_non_bool_enabled():
    with pytest.raises(SchemaValidationError, match="'enabled' must be a bool"):
        feature_flags.FeatureFlagV1.validate(_valid_payload() | {"enabled": "true"})


def test_rejects_int_enabled():
    """int 0/1 must not slip through as truthy/falsey bools."""
    with pytest.raises(SchemaValidationError, match="'enabled' must be a bool"):
        feature_flags.FeatureFlagV1.validate(_valid_payload() | {"enabled": 1})


# ── Required-field rejection ─────────────────────────────────


def test_rejects_missing_enabled():
    payload = _valid_payload()
    payload.pop("enabled")
    with pytest.raises(SchemaValidationError, match="'enabled' must be a bool"):
        feature_flags.FeatureFlagV1.validate(payload)


def test_rejects_missing_description():
    payload = _valid_payload()
    payload.pop("description")
    with pytest.raises(SchemaValidationError, match="missing or empty required field 'description'"):
        feature_flags.FeatureFlagV1.validate(payload)


def test_rejects_empty_description():
    with pytest.raises(SchemaValidationError, match="missing or empty required field 'description'"):
        feature_flags.FeatureFlagV1.validate(_valid_payload() | {"description": ""})


def test_rejects_missing_owner():
    payload = _valid_payload()
    payload.pop("owner")
    with pytest.raises(SchemaValidationError, match="missing or empty required field 'owner'"):
        feature_flags.FeatureFlagV1.validate(payload)


def test_rejects_empty_owner():
    with pytest.raises(SchemaValidationError, match="missing or empty required field 'owner'"):
        feature_flags.FeatureFlagV1.validate(_valid_payload() | {"owner": ""})


# ── Unknown-field rejection ──────────────────────────────────


def test_rejects_unknown_field():
    payload = _valid_payload() | {"undocumented_extra": "nope"}
    with pytest.raises(SchemaValidationError, match=r"unknown field\(s\): \['undocumented_extra'\]"):
        feature_flags.FeatureFlagV1.validate(payload)
