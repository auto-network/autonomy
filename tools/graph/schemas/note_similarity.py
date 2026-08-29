"""``autonomy.graph.note-similarity#1`` — duplicate-note guard tuning.

``graph note`` (create) refuses when the new body is near-identical to a
recently created note in the same org, naming the existing id and the
``graph note update`` invocation instead. ``--force`` bypasses. This is
the backstop behind the deterministic quoted-subcommand guard in
``cmd_note_router``: it catches the author who never reached for
``update`` at all.

The default threshold is MEASURED, not guessed (auto-0828-134703,
2026-08-29, on the four-id incident corpus): consecutive revisions of
one document scored 74.2% / 92.9% / 82.5% whitespace-normalized difflib
ratio, while genuinely distinct notes peaked at 7.3% — so 0.70 catches
all observed revisions with ~9x margin over the worst false positive.
Do not raise above 0.80: that is inside the noise of a substantive
rewrite and the measured incident's largest edit (74.2%) would pass.
Re-measure against a larger corpus before tuning; that is a settings
write, not a code change.

Machine-homed like ``autonomy.dispatch.limits``: the guard runs where
notes are created (this machine's dashboard/host processes).
"""

from __future__ import annotations

from .registry import (
    SchemaValidationError,
    SettingSchema,
    home,
    publication_band,
    singleton,
)


SET_ID = "autonomy.graph.note-similarity"
SCHEMA_REVISION = 1

DEFAULT_THRESHOLD = 0.70
DEFAULT_WINDOW_HOURS = 48
DEFAULT_MIN_CHARS = 280


SYNOPSIS = {
    "summary": (
        "Duplicate-note guard: refuse a graph note create whose body is "
        "near-identical to a recent note, pointing at note update instead"
    ),
    "nouns": [
        "note similarity", "duplicate note", "note revision",
        "near-duplicate", "note churn",
    ],
    "related_set_ids": [],
}


@publication_band(min="raw", max="curated")
@home("machine")
@singleton(key="default")
class NoteSimilarityV1(SettingSchema):
    """Shape of an ``autonomy.graph.note-similarity#1`` payload."""

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "enabled": {
            "type": "boolean",
            "description": "Master switch for the duplicate-note guard.",
            "default": True,
        },
        "threshold": {
            "type": "number",
            "description": (
                "Whitespace-normalized difflib ratio at or above which a "
                "create is refused. Measured revision cluster: 0.74-0.93; "
                "measured distinct-note ceiling: 0.073. Keep in 0.5..0.95."
            ),
            "default": DEFAULT_THRESHOLD,
        },
        "window_hours": {
            "type": "integer",
            "description": (
                "How far back to look for near-duplicates. The incident "
                "cluster spanned ten minutes; 48h covers a work session "
                "resumed next day without scanning deep history."
            ),
            "default": DEFAULT_WINDOW_HOURS,
        },
        "min_chars": {
            "type": "integer",
            "description": (
                "Both bodies must be at least this long (normalized) for "
                "the guard to apply — short status notes legitimately "
                "resemble each other."
            ),
            "default": DEFAULT_MIN_CHARS,
        },
    }

    @classmethod
    def validate(cls, payload: dict) -> None:
        super().validate(payload)
        threshold = payload.get("threshold")
        if threshold is not None:
            if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) \
                    or not (0.5 <= float(threshold) <= 0.95):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'threshold' must be a number in "
                    f"0.5..0.95, got {threshold!r}"
                )
        for name, ceiling in (("window_hours", 24 * 30), ("min_chars", 100_000)):
            value = payload.get(name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) \
                    or not (1 <= value <= ceiling):
                raise SchemaValidationError(
                    f"{cls.__name__}: {name!r} must be an integer in "
                    f"1..{ceiling}, got {value!r}"
                )
