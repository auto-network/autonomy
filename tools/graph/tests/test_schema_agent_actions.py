"""Tests for the dashboard.agent-actions schema (V1 + V2)."""

from __future__ import annotations

import copy

import pytest

from tools.graph.schemas import agent_actions  # noqa: F401 — registers schema
from tools.graph.schemas.agent_actions import (
    AGENT_ACTIONS_SET_ID,
    AgentActionV1,
    AgentActionV2,
)
from tools.graph.schemas.registry import (
    SchemaValidationError,
    upconvert_chain,
)


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


def test_v1_rejects_input_prompt():
    """``input_prompt`` was added in #2; #1 must not accept it."""
    p = _base_payload()
    p["input_prompt"] = "What do you want to ask?"
    with pytest.raises(SchemaValidationError, match="input_prompt"):
        AgentActionV1.validate(p)


# ── #2: input_prompt ──────────────────────────────────────


def test_v2_accepts_input_prompt():
    p = _base_payload()
    p["input_prompt"] = "What do you want to ask about this bead?"
    AgentActionV2.validate(p)


def test_v2_input_prompt_optional():
    """Existing #1 shapes (sans input_prompt) must validate at #2."""
    AgentActionV2.validate(_base_payload())


def test_v2_input_prompt_must_be_string():
    p = _base_payload()
    p["input_prompt"] = 42
    with pytest.raises(SchemaValidationError, match="input_prompt"):
        AgentActionV2.validate(p)

    p["input_prompt"] = ["nope"]
    with pytest.raises(SchemaValidationError, match="input_prompt"):
        AgentActionV2.validate(p)

    p["input_prompt"] = {"label": "x"}
    with pytest.raises(SchemaValidationError, match="input_prompt"):
        AgentActionV2.validate(p)


def test_v2_ask_question_realistic_round_trip():
    """The realistic ``bead.ask-question`` member shape validates at #2."""
    p = {
        "asset_type": "bead",
        "label": "Ask a Question",
        "icon": "?",
        "workspace": "autonomy-developer",
        "model": "claude-haiku-4-5-20251001",
        "estimated_seconds": 60,
        "writes": ["bead.comment"],
        "input_prompt": "What do you want to ask about this bead?",
        "card_summary": [
            {"label": "Answer", "path": "answer", "format": "text"},
            {"label": "Confidence", "path": "confidence", "format": "stars"},
        ],
        "prompt_template": "Answer about {asset[id]}: {custom_input}",
    }
    AgentActionV2.validate(p)
    snap = copy.deepcopy(p)
    AgentActionV2.validate(p)
    assert p == snap


# ── Upconvert #1 → #2 ─────────────────────────────────────


def test_upconvert_v1_to_v2_is_identity_for_legacy_payload():
    """A real-shaped #1 row passes through ``AgentActionV2.upconvert_from_prev``
    unchanged and the result validates against #2.
    """
    legacy = {
        "asset_type": "bead",
        "label": "Dry-Run Implement",
        "icon": "⚙",
        "workspace": "autonomy-developer",
        "model": "claude-haiku-4-5-20251001",
        "estimated_seconds": 120,
        "writes": ["bead.comment", "bead.labels"],
        "prompt_template": "audit",
    }
    upconverted = AgentActionV2.upconvert_from_prev(legacy)
    assert upconverted == legacy
    assert "input_prompt" not in upconverted
    AgentActionV2.validate(upconverted)


def test_upconvert_chain_registered_for_v1_to_v2():
    """The registry must surface the #1 → #2 hop so
    ``graph set migrate dashboard.agent-actions --target 2`` finds it.
    """
    chain = upconvert_chain(AGENT_ACTIONS_SET_ID, 1, 2)
    assert chain is not None
    assert len(chain) == 1
