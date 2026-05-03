"""Tests for the dashboard.agent-actions#1 schema (AgentActionV1)."""

from __future__ import annotations

import copy

import pytest

from tools.graph.schemas import agent_actions  # noqa: F401 — registers schema
from tools.graph.schemas.agent_actions import AgentActionV1
from tools.graph.schemas.registry import SchemaValidationError


def _base_payload() -> dict:
    return {
        "asset_type": "bead",
        "label": "Dry-Run Implement",
        "model": "claude-haiku-4-5-20251001",
        "prompt_template": "audit the bead",
    }


def test_minimal_payload_validates():
    AgentActionV1.validate(_base_payload())


def test_card_summary_optional_omitted():
    p = _base_payload()
    AgentActionV1.validate(p)  # no card_summary at all


def test_card_summary_valid_shape():
    p = _base_payload()
    p["card_summary"] = [
        {"label": "Verdict", "path": "primary_outcome", "format": "badge"},
        {"label": "Issue", "path": "categorical.dominant_issue"},
        {"label": "Spec clarity", "path": "quality_scores.spec_clarity",
         "format": "stars"},
        {"label": "Top risk", "path": "summary.top_risk", "format": "text"},
        {"label": "Dump", "path": "facts", "format": "code"},
    ]
    AgentActionV1.validate(p)


def test_card_summary_must_be_list():
    p = _base_payload()
    p["card_summary"] = {"label": "x", "path": "y"}
    with pytest.raises(SchemaValidationError, match="must be a list"):
        AgentActionV1.validate(p)


def test_card_summary_slot_missing_label():
    p = _base_payload()
    p["card_summary"] = [{"path": "primary_outcome"}]
    with pytest.raises(SchemaValidationError, match="missing or empty 'label'"):
        AgentActionV1.validate(p)


def test_card_summary_slot_missing_path():
    p = _base_payload()
    p["card_summary"] = [{"label": "Verdict"}]
    with pytest.raises(SchemaValidationError, match="missing or empty 'path'"):
        AgentActionV1.validate(p)


def test_card_summary_slot_invalid_format():
    p = _base_payload()
    p["card_summary"] = [
        {"label": "Verdict", "path": "primary_outcome", "format": "html"},
    ]
    with pytest.raises(SchemaValidationError, match="format must be one of"):
        AgentActionV1.validate(p)


def test_card_summary_slot_unknown_key():
    p = _base_payload()
    p["card_summary"] = [
        {"label": "Verdict", "path": "primary_outcome", "tooltip": "..."},
    ]
    with pytest.raises(SchemaValidationError, match="unknown keys"):
        AgentActionV1.validate(p)


def test_unknown_top_level_field_still_rejected():
    p = _base_payload()
    p["card_summery"] = []  # typo'd field
    with pytest.raises(SchemaValidationError, match="unknown field"):
        AgentActionV1.validate(p)


def test_dry_run_implement_realistic_payload_round_trip():
    """A realistic shape mirroring the production member must validate."""
    p = {
        "asset_type": "bead",
        "label": "Dry-Run Implement",
        "icon": "⚙",
        "workspace": "autonomy-developer",
        "model": "claude-haiku-4-5-20251001",
        "estimated_seconds": 120,
        "writes": ["bead.comment", "bead.metadata"],
        "prompt_template": "...",
        "card_summary": [
            {"label": "Verdict", "path": "primary_outcome", "format": "badge"},
            {"label": "Issue", "path": "categorical.dominant_issue"},
            {"label": "Architecture fit",
             "path": "quality_scores.architecture_fit", "format": "stars"},
            {"label": "Top risk", "path": "summary.top_risk"},
        ],
    }
    AgentActionV1.validate(p)
    # Defensive copy — confirm validate doesn't mutate.
    snap = copy.deepcopy(p)
    AgentActionV1.validate(p)
    assert p == snap
