"""Tests for ``dashboard.session.worktree.rebase_status#1``.

Covers schema validation (state enum, allowed empty payload, error-string
shape, extra-field rejection), registry round-trip, and decorator wiring.
"""

from __future__ import annotations

import pytest

from tools.graph.schemas.registry import (
    SchemaValidationError,
    get_schema,
    validate_payload,
)
from tools.dashboard.worktree_directives import (
    VALID_REBASE_STATES,
    WORKTREE_REBASE_STATUS_REVISION,
    WORKTREE_REBASE_STATUS_SET_ID,
    WorktreeRebaseStatusV1,
)


def test_registry_round_trip():
    cls = get_schema(
        WORKTREE_REBASE_STATUS_SET_ID,
        WORKTREE_REBASE_STATUS_REVISION,
    )
    assert cls is WorktreeRebaseStatusV1
    validate_payload(
        WORKTREE_REBASE_STATUS_SET_ID,
        WORKTREE_REBASE_STATUS_REVISION,
        {"state": "in_progress", "error": ""},
    )


def test_keyed_per_entity_decorator_applied():
    assert WorktreeRebaseStatusV1._access_pattern == "keyed_per_entity"


def test_set_id_is_under_dashboard_namespace():
    """The schema lives under the dashboard.session.worktree.* namespace."""
    assert WorktreeRebaseStatusV1.set_id.startswith(
        "dashboard.session.worktree."
    )
    assert WorktreeRebaseStatusV1.set_id.endswith("rebase_status")


@pytest.mark.parametrize("state", list(VALID_REBASE_STATES))
def test_accepts_each_valid_state(state):
    WorktreeRebaseStatusV1.validate({"state": state, "error": ""})


def test_accepts_empty_payload():
    """All fields default to empty — an empty payload is valid (initial seed)."""
    WorktreeRebaseStatusV1.validate({})


def test_accepts_failed_with_error_text():
    WorktreeRebaseStatusV1.validate({
        "state": "failed",
        "error": "conflict in tools/dashboard/server.py at line 42",
    })


def test_rejects_unknown_state():
    with pytest.raises(SchemaValidationError, match="state"):
        WorktreeRebaseStatusV1.validate({"state": "completed"})


def test_rejects_non_string_state():
    with pytest.raises(SchemaValidationError, match="state"):
        WorktreeRebaseStatusV1.validate({"state": 1})


def test_rejects_non_string_error():
    with pytest.raises(SchemaValidationError, match="error"):
        WorktreeRebaseStatusV1.validate({"state": "failed", "error": 5})


def test_rejects_extra_fields():
    """Forward-compat: unknown fields are rejected so payload schema drift
    is loud, not silent."""
    with pytest.raises(SchemaValidationError, match="unknown"):
        WorktreeRebaseStatusV1.validate({
            "state": "in_progress",
            "error": "",
            "agent_pid": 12345,
        })
