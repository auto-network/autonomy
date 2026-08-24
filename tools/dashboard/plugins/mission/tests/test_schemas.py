"""The mission plugin's settings contracts (schema v2, greenfield).

The substrate enforces declarations (undeclared fields, required
fields, enums) on every write; these tests pin the cross-field rules
the declarations cannot express — the per-kind vocabularies — plus the
scaffold facts the rest of the build leans on: four registered sets,
one revision, and a manifest that loads dormant without disturbing the
legacy plugin.
"""
from __future__ import annotations

import pytest

# Importing the module registers all four schemas with the registry.
from tools.dashboard.plugins.mission.entrypoints import schemas as S
from tools.graph.schemas.registry import (
    SchemaValidationError,
    validate_payload,
)


def _item(**kw) -> dict:
    base = {"kind": "scope", "title": "t"}
    base.update(kw)
    return base


def _ok(payload: dict) -> None:
    validate_payload(S.ITEM_SET_ID, S.SCHEMA_REVISION, payload)


def _refused(payload: dict, fragment: str) -> None:
    with pytest.raises(SchemaValidationError) as exc:
        validate_payload(S.ITEM_SET_ID, S.SCHEMA_REVISION, payload)
    assert fragment in str(exc.value)


class TestItemVocabulary:
    def test_five_kinds_exactly(self):
        assert S.ITEM_KINDS == (
            "scope", "status", "checkpoint", "decision", "question")

    def test_checkpoint_states(self):
        for st in S.CHECKPOINT_STATES:
            _ok(_item(kind="checkpoint", state=st))
        _refused(_item(kind="checkpoint", state=""), "checkpoint state")
        _refused(_item(kind="checkpoint", state="open"), "checkpoint state")

    def test_question_states(self):
        for st in S.QUESTION_STATES:
            _ok(_item(kind="question", state=st))
        _refused(_item(kind="question", state="confirmed"), "question state")

    def test_stateless_kinds_refuse_states(self):
        for kind in ("scope", "status", "decision"):
            _ok(_item(kind=kind))
            _refused(_item(kind=kind, state="confirmed"), "stateless")

    def test_blocking_is_a_question_property(self):
        _ok(_item(kind="question", state="open", blocking=True))
        _refused(_item(kind="status", blocking=True), "blocking")

    def test_faq_marks_decisions_only(self):
        _ok(_item(kind="decision", chosen="x", faq=True))
        _refused(_item(kind="question", state="open", faq=True), "faq")

    def test_answer_forces_answered_state(self):
        _ok(_item(kind="question", state="answered",
                  answer={"text": "a", "by": "s", "at": "2026-08-24T00:00:00Z"}))
        _refused(
            _item(kind="question", state="open",
                  answer={"text": "a", "by": "s", "at": "t"}),
            "answered")

    def test_streams_belong_to_checkpoints(self):
        entry = {"by": "s", "at": "2026-08-24T00:00:00Z", "text": "w"}
        hist = {"from": "pending", "to": "in_progress",
                "at": "2026-08-24T00:00:00Z", "by": "s"}
        _ok(_item(kind="checkpoint", state="in_progress",
                  work=[entry], history=[hist]))
        _refused(_item(kind="decision", work=[entry]), "checkpoints")

    def test_declarations_enforced_by_substrate(self):
        _refused(_item(bogus=1), "undeclared")
        with pytest.raises(SchemaValidationError):
            validate_payload(S.ITEM_SET_ID, S.SCHEMA_REVISION,
                             {"kind": "scope"})  # missing title


class TestCompanionSets:
    def test_registry_pillar_chat_validate(self):
        validate_payload(S.MISSION_SET_ID, S.SCHEMA_REVISION,
                         {"name": "Multi-User Autonomy"})
        validate_payload(S.PILLAR_SET_ID, S.SCHEMA_REVISION,
                         {"name": "Relay",
                          "bead_labels": ["pillar:relay-network"]})
        validate_payload(S.CHAT_SET_ID, S.SCHEMA_REVISION,
                         {"entries": [{"by": "Jeremy",
                                       "at": "2026-08-24T00:00:00Z",
                                       "text": "hi"}]})

    def test_key_segments_not_repeated_in_payload(self):
        with pytest.raises(SchemaValidationError):
            validate_payload(S.ITEM_SET_ID, S.SCHEMA_REVISION,
                             _item(surface_id="x"))
        with pytest.raises(SchemaValidationError):
            validate_payload(S.PILLAR_SET_ID, S.SCHEMA_REVISION,
                             {"name": "n", "pillar_id": "x"})


class TestManifest:
    def test_plugin_discovers_dormant_with_legacy_intact(self):
        from tools.dashboard.plugin_api import loader
        found = {p.manifest.id: p for p in loader.discover() if p.manifest}
        assert "mission" in found
        m = found["mission"].manifest
        assert m.default_enabled is False
        assert len(m.entrypoints.schemas or []) == 4
        assert "mission_control" in found  # legacy untouched
