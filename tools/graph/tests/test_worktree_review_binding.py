"""Tests for ``autonomy.worktree.review_binding#1``.

Covers schema validation (required ``base_sha``, hex format, extra-field
rejection), key parser, and registry round-trip.
"""

from __future__ import annotations

import pytest

from tools.graph.schemas.registry import (
    SchemaValidationError,
    get_schema,
    validate_payload,
)
from tools.graph.schemas.worktree_review_binding import (
    SCHEMA_REVISION,
    SET_ID,
    WorktreeReviewBindingV1,
    parse_binding_key,
)


def _payload() -> dict:
    return {"base_sha": "abc1234567def890"}


def test_accepts_valid_payload():
    WorktreeReviewBindingV1.validate(_payload())


def test_registry_round_trip():
    cls = get_schema(SET_ID, SCHEMA_REVISION)
    assert cls is WorktreeReviewBindingV1
    validate_payload(SET_ID, SCHEMA_REVISION, _payload())


def test_rejects_missing_base_sha():
    with pytest.raises(SchemaValidationError, match="base_sha"):
        WorktreeReviewBindingV1.validate({})


def test_rejects_empty_base_sha():
    with pytest.raises(SchemaValidationError, match="non-empty"):
        WorktreeReviewBindingV1.validate({"base_sha": ""})


def test_rejects_non_hex_base_sha():
    with pytest.raises(SchemaValidationError, match="hex"):
        WorktreeReviewBindingV1.validate({"base_sha": "not-a-sha"})


def test_rejects_extra_fields():
    with pytest.raises(SchemaValidationError, match="unknown"):
        WorktreeReviewBindingV1.validate({
            "base_sha": "abc123",
            "head_sha": "def456",  # head_sha lives on the cache, not here
        })


def test_keyed_per_entity_decorator_applied():
    assert WorktreeReviewBindingV1._access_pattern == "keyed_per_entity"


def test_parse_binding_key_basic():
    session, repo, branch, review_id = parse_binding_key(
        "auto-x:autonomy:session/auto-x:1234"
    )
    assert session == "auto-x"
    assert repo == "autonomy"
    assert branch == "session/auto-x"
    assert review_id == "1234"


def test_parse_binding_key_branch_with_slash():
    """Branches like ``session/auto-x`` must round-trip correctly."""
    _, _, branch, review_id = parse_binding_key(
        "s:r:feature/foo/bar:42"
    )
    assert branch == "feature/foo/bar"
    assert review_id == "42"


def test_parse_binding_key_branch_with_colons():
    """Branch names may contain ``:``; review_id remains the final segment."""
    session, repo, branch, review_id = parse_binding_key(
        "s:r:feature:stack:topic:42"
    )
    assert session == "s"
    assert repo == "r"
    assert branch == "feature:stack:topic"
    assert review_id == "42"


def test_parse_binding_key_string_review_id():
    """``review_id`` is a string — Jira keys / Linear ids must work."""
    *_, review_id = parse_binding_key(
        "s:r:branch:LIN-123"
    )
    assert review_id == "LIN-123"


def test_parse_binding_key_rejects_missing_segments():
    with pytest.raises(ValueError):
        parse_binding_key("s:r:branch")  # missing review_id
    with pytest.raises(ValueError):
        parse_binding_key("s:r")  # missing branch and review_id


def test_parse_binding_key_rejects_empty_segment():
    with pytest.raises(ValueError):
        parse_binding_key("s::branch:1")  # empty repo
