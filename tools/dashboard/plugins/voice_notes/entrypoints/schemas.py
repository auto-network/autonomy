"""Organization-local note records owned by the Voice Notes plugin."""
from __future__ import annotations

from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
)


VOICE_NOTE_SET_ID = "dashboard.voice-notes.note"
SCHEMA_REVISION = 1

SYNOPSIS = {
    "summary": "Editable notes created in the dashboard's voice-first notebook.",
    "nouns": ["voice note", "dictated note", "notebook"],
    "related_set_ids": [],
}


@home("organization")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="note_id")
class VoiceNoteV1(SettingSchema):
    """One private organization-local note. Key: ``note_id``."""

    set_id = VOICE_NOTE_SET_ID
    schema_revision = SCHEMA_REVISION

    note_id: str = field(required=True, description="Stable note identifier")
    title: str = field(default="", description="Human-readable note title")
    body: str = field(default="", description="Editable note body")
    created_at: str = field(required=True, description="UTC creation timestamp")
    updated_at: str = field(required=True, description="UTC last-save timestamp")

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError("VoiceNoteV1 payload must be an object")
        expected = {"note_id", "title", "body", "created_at", "updated_at"}
        extra = set(payload) - expected
        if extra:
            raise SchemaValidationError(f"VoiceNoteV1 unknown fields: {sorted(extra)}")
        for key in expected:
            if not isinstance(payload.get(key), str):
                raise SchemaValidationError(f"VoiceNoteV1 {key!r} must be a string")
        if not payload["note_id"].strip():
            raise SchemaValidationError("VoiceNoteV1 'note_id' must not be empty")
        if len(payload["title"]) > 240:
            raise SchemaValidationError("VoiceNoteV1 title exceeds 240 characters")
        if len(payload["body"]) > 200_000:
            raise SchemaValidationError("VoiceNoteV1 body exceeds 200,000 characters")
