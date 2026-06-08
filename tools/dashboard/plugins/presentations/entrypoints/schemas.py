"""Setting schemas owned by the Present plugin."""
from __future__ import annotations

from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    keyed_per_entity,
)


PRESENTATION_DECK_SET_ID = "dashboard.presentation.deck"
SCHEMA_REVISION = 1

SYNOPSIS = {
    "summary": (
        "Presentation deck library records keyed by stable Design Studio design "
        "id, with creator/session provenance and slide metadata."
    ),
    "nouns": [
        "presentation", "presentation deck", "deck library", "present",
        "slide", "design deck", "deck provenance",
    ],
    "related_set_ids": [],
}


@keyed_per_entity
class PresentationDeckV1(SettingSchema):
    """One deck shown through the Present app. Key: stable ``design_id``."""

    set_id = PRESENTATION_DECK_SET_ID
    schema_revision = SCHEMA_REVISION

    design_id: str = field(required=True, description="Stable Design Studio design id")
    latest_revision_id: str = field(default="", description="Deprecated cached revision id; API responses compute the latest revision from design_id")
    name: str = field(required=True, description="Deck display name")
    subtitle: str = field(default="", description="Deck subtitle/description")
    created_at: str = field(default="", description="Original design creation timestamp")
    modified_at: str = field(default="", description="Latest Design Studio revision timestamp")
    last_shown_at: str = field(default="", description="Most recent presentation timestamp")
    creator_session_id: str = field(default="", description="Session that created the deck")
    creator_session_label: str = field(default="", description="Human label for creator session")
    author_session_id: str = field(default="", description="Future multi-author owner session")
    author_session_label: str = field(default="", description="Future multi-author owner label")
    slide_count: int = field(default=1, description="Number of slides detected by viewer")
    slide_ids: list = field(default_factory=list, element=str, description="Stable slide ids")

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, got {type(payload).__name__}"
            )
        for required in ("design_id", "name"):
            value = payload.get(required)
            if not isinstance(value, str) or not value.strip():
                raise SchemaValidationError(
                    f"{cls.__name__}: {required!r} must be a non-empty string"
                )
        for key in (
            "subtitle", "created_at", "modified_at", "last_shown_at",
            "creator_session_id", "creator_session_label",
            "author_session_id", "author_session_label",
        ):
            value = payload.get(key)
            if value is not None and not isinstance(value, str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {key!r} must be a string or null"
                )
        slide_count = payload.get("slide_count", 1)
        if not isinstance(slide_count, int) or slide_count < 1:
            raise SchemaValidationError(
                f"{cls.__name__}: 'slide_count' must be a positive integer"
            )
        slide_ids = payload.get("slide_ids", [])
        if not isinstance(slide_ids, list) or not all(isinstance(x, str) for x in slide_ids):
            raise SchemaValidationError(
                f"{cls.__name__}: 'slide_ids' must be a list of strings"
            )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )
