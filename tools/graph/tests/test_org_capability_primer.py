"""Schema contract for organization capability-primer supplements."""

from __future__ import annotations

import pytest

from tools.graph.schemas.org_capability_primer import (
    SCHEMA_REVISION,
    SET_ID,
    resolve_markdown,
)
from tools.graph.schemas.registry import SchemaValidationError, get_schema, validate_payload


def test_org_capability_primer_schema_registered_and_validates():
    assert get_schema(SET_ID, SCHEMA_REVISION) is not None
    validate_payload(
        SET_ID,
        SCHEMA_REVISION,
        {"markdown": "Use the exact field id.", "enabled": True, "order": 50},
    )


def test_org_capability_primer_rejects_unknown_fields():
    with pytest.raises(SchemaValidationError, match="unknown field"):
        validate_payload(
            SET_ID,
            SCHEMA_REVISION,
            {"markdown": "x", "implementation": "autonomy/jira"},
        )


def test_org_capability_primer_disabled_block_resolves_empty():
    assert resolve_markdown({"markdown": "parked", "enabled": False}) == ""
