"""Tests for ``autonomy.source_control.review_state#1``.

Covers schema validation, the ``is_terminal`` derivation, per-check
enum enforcement, and atomic-write round-trip via ``add_setting``.
"""

from __future__ import annotations

import os

import pytest

from tools.graph.schemas.registry import (
    SchemaValidationError,
    cache_expires_at,
    get_schema,
    validate_payload,
)
from tools.graph.schemas.source_control_review_state import (
    SCHEMA_REVISION,
    SET_ID,
    SourceControlReviewStateV1,
    parse_state_key,
)


def _payload(**overrides) -> dict:
    base = {
        "title": "Add operator-declared review bindings",
        "body": "See the bead description.",
        "state": "open",
        "head_sha": "abc123",
        "base_sha": "def456",
        "base_branch": "main",
        "is_draft": False,
        "provider": "github",
    }
    base.update(overrides)
    return base


def test_accepts_valid_payload():
    SourceControlReviewStateV1.validate(_payload())


def test_registry_round_trip():
    cls = get_schema(SET_ID, SCHEMA_REVISION)
    assert cls is SourceControlReviewStateV1
    validate_payload(SET_ID, SCHEMA_REVISION, _payload())


def test_rejects_unknown_fields():
    with pytest.raises(SchemaValidationError, match="unknown"):
        SourceControlReviewStateV1.validate(_payload(number=42))


def test_rejects_all_derivable_top_level_fields():
    """Cache rows store source-of-truth provider data, not projections."""
    derivable = {
        "terminal": True,
        "running": False,
        "aggregate_state": "green",
        "number": 42,
        "commit_shas": ["abc123"],
        "fetched_at": "2026-05-02T12:00:00Z",
        "refresh_after": "2026-05-02T12:01:00Z",
    }
    for key, value in derivable.items():
        with pytest.raises(SchemaValidationError, match="unknown"):
            SourceControlReviewStateV1.validate(_payload(**{key: value}))


def test_rejects_missing_required():
    payload = _payload()
    del payload["title"]
    with pytest.raises(SchemaValidationError, match="title"):
        SourceControlReviewStateV1.validate(payload)


def test_rejects_invalid_state_enum():
    with pytest.raises(SchemaValidationError, match="state"):
        SourceControlReviewStateV1.validate(_payload(state="abandoned"))


def test_accepts_etag_and_url():
    SourceControlReviewStateV1.validate(_payload(
        node_id="PR_kwD123",
        etag='W/"abc123"',
        url="https://github.com/o/r/pull/1",
    ))


def test_rejects_non_string_node_id():
    with pytest.raises(SchemaValidationError, match="node_id"):
        SourceControlReviewStateV1.validate(_payload(node_id=42))


def test_rejects_non_string_etag():
    with pytest.raises(SchemaValidationError, match="etag"):
        SourceControlReviewStateV1.validate(_payload(etag=42))


def test_accepts_checks_with_pass_status():
    SourceControlReviewStateV1.validate(_payload(checks=[
        {"id": "build", "label": "build", "status": "pass"},
        {"id": "test", "label": "test", "status": "fail",
         "detail": "1 of 12 failed"},
    ]))


def test_rejects_check_with_invalid_status():
    with pytest.raises(SchemaValidationError, match="status"):
        SourceControlReviewStateV1.validate(_payload(checks=[
            {"id": "build", "label": "build", "status": "halted"},
        ]))


def test_rejects_check_missing_label():
    with pytest.raises(SchemaValidationError, match="label"):
        SourceControlReviewStateV1.validate(_payload(checks=[
            {"id": "build", "status": "pass"},
        ]))


def test_rejects_check_with_unknown_field():
    with pytest.raises(SchemaValidationError, match="unknown"):
        SourceControlReviewStateV1.validate(_payload(checks=[
            {"id": "build", "label": "build", "status": "pass", "icon": "BL"},
        ]))


def test_is_terminal_payload_empty_checks():
    """Vacuously terminal — no checks running means done."""
    assert SourceControlReviewStateV1.is_terminal_payload({"checks": []}) is True
    assert SourceControlReviewStateV1.is_terminal_payload({}) is True


def test_is_terminal_payload_all_pass_or_fail():
    payload = {"checks": [
        {"id": "a", "label": "a", "status": "pass"},
        {"id": "b", "label": "b", "status": "fail"},
    ]}
    assert SourceControlReviewStateV1.is_terminal_payload(payload) is True


def test_is_terminal_payload_running():
    payload = {"checks": [
        {"id": "a", "label": "a", "status": "pass"},
        {"id": "b", "label": "b", "status": "running"},
    ]}
    assert SourceControlReviewStateV1.is_terminal_payload(payload) is False


def test_is_terminal_payload_pending():
    payload = {"checks": [
        {"id": "a", "label": "a", "status": "pending"},
    ]}
    assert SourceControlReviewStateV1.is_terminal_payload(payload) is False


def test_keyed_per_entity_decorator_applied():
    assert SourceControlReviewStateV1._access_pattern == "cache"
    assert SourceControlReviewStateV1._key_strategy == "natural"
    assert SourceControlReviewStateV1._cache_ttl_seconds == 30 * 24 * 3600


def test_cache_expires_at_uses_schema_ttl():
    assert cache_expires_at(
        SET_ID, SCHEMA_REVISION, "2026-05-02T00:00:00Z"
    ) == "2026-06-01T00:00:00Z"


def test_parse_state_key_basic():
    repo_slug, review_id = parse_state_key("owner/repo:1234")
    assert repo_slug == "owner/repo"
    assert review_id == "1234"


def test_parse_state_key_string_review_id():
    repo_slug, review_id = parse_state_key("acme/widgets:LIN-99")
    assert repo_slug == "acme/widgets"
    assert review_id == "LIN-99"


def test_export_json_schema_surfaces_cache_ttl():
    js = SourceControlReviewStateV1.export_json_schema()
    assert js["access_pattern"] == "cache"
    assert js["cache_ttl_seconds"] == 30 * 24 * 3600


def test_parse_state_key_rejects_missing_review_id():
    with pytest.raises(ValueError):
        parse_state_key("owner/repo")
    with pytest.raises(ValueError):
        parse_state_key("owner/repo:")


def test_atomic_write_via_add_setting(tmp_path, monkeypatch):
    """Every refresh overwrites the row atomically via ``add_setting``."""
    from tools.graph import settings_ops

    db_path = tmp_path / "settings.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))

    payload = _payload(checks=[
        {"id": "build", "label": "build", "status": "pass"},
    ])
    sid = settings_ops.add_setting(
        SET_ID, SCHEMA_REVISION, "owner/repo:1", payload,
     org=settings_ops.CALLER_ORG)
    assert sid

    members = settings_ops.read_set(SET_ID, org=settings_ops.CALLER_ORG).to_dict()
    assert "owner/repo:1" in members
    stored = members["owner/repo:1"].payload
    assert stored["title"] == payload["title"]
    assert stored["checks"][0]["status"] == "pass"
    monkeypatch.delenv("GRAPH_DB", raising=False)
