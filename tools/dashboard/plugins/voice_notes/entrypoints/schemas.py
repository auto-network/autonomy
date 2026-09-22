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
    register_upconverter,
)


VOICE_NOTE_SET_ID = "dashboard.voice-notes.note"
#: Revision 2 drops ``note_id`` from the payload: the key already carries it
#: (operator ruling 2026-09-22; a repeated key invites drift). Revision 1 rows
#: upconvert by dropping the field.
SCHEMA_REVISION = 2
VOICE_NOTE_REVISION_1 = 1

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
    # Frozen: this class is revision 1 forever (auto-j1y0z).
    schema_revision = VOICE_NOTE_REVISION_1

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


@home("organization")
@publication_band(max="raw")
@keyed_per_entity(key_strategy="note_id")
class VoiceNoteV2(SettingSchema):
    """One private organization-local note. Key: ``note_id``; the payload no
    longer repeats it (the key comes back with the row)."""

    set_id = VOICE_NOTE_SET_ID
    schema_revision = SCHEMA_REVISION

    title: str = field(default="", description="Human-readable note title")
    body: str = field(default="", description="Editable note body")
    created_at: str = field(required=True, description="UTC creation timestamp")
    updated_at: str = field(required=True, description="UTC last-save timestamp")

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError("VoiceNoteV2 payload must be an object")
        expected = {"title", "body", "created_at", "updated_at"}
        extra = set(payload) - expected
        if extra:
            raise SchemaValidationError(f"VoiceNoteV2 unknown fields: {sorted(extra)}")
        for key in expected:
            if not isinstance(payload.get(key), str):
                raise SchemaValidationError(f"VoiceNoteV2 {key!r} must be a string")
        if len(payload["title"]) > 240:
            raise SchemaValidationError("VoiceNoteV2 title exceeds 240 characters")
        if len(payload["body"]) > 200_000:
            raise SchemaValidationError("VoiceNoteV2 body exceeds 200,000 characters")


def _drop_note_id(payload: dict) -> dict:
    return {k: v for k, v in dict(payload).items() if k != "note_id"}


register_upconverter(VOICE_NOTE_SET_ID, VOICE_NOTE_REVISION_1, SCHEMA_REVISION, _drop_note_id)
