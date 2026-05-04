"""Setting schemas owned by the Settings Nexus plugin (bead auto-ct3ey).

Two Settings drive the Nexus surface:

* ``dashboard.nexus.scene`` — singleton (key=``active``) holding the
  page banner: title, subtitle, anchor sentence, presenter pointer,
  optional focused tile id, and overall layout. Latest-write-wins.
* ``dashboard.nexus.tile`` — keyed-per-entity timeline entries. Each
  tile carries a ``kind`` discriminator (markdown / status / code /
  …), common fields (``title`` / ``body`` / ``ts`` / ``order`` /
  ``width``) and an open-ended ``data`` dict for kind-specific fields
  (per the Design Studio fixture's per-kind shapes). The key is the
  caller-supplied tile id (uuid or natural slug like ``intro``).

Validation is imperative — the typed-field declarations carry
metadata for codegen / introspection, and ``validate()`` enforces
the cross-field shape rules the metadata cannot yet describe
(non-empty kind, enum membership, dict-typed ``data``,
extra-field rejection).

Eager-import note: this module is referenced from ``server.py`` so
that auto-registration via ``SettingSchema.__init_subclass__`` runs
before ``GraphDB._init_schema`` calls ``flush_schema_meta``. See
pitfall ``graph://3fe60c25-fab``.
"""
from __future__ import annotations

from typing import Any

from tools.graph.schemas.registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    keyed_per_entity,
    singleton,
)


NEXUS_SCENE_SET_ID = "dashboard.nexus.scene"
NEXUS_TILE_SET_ID = "dashboard.nexus.tile"

SCHEMA_REVISION = 1

VALID_SCENE_LAYOUTS = ("spotlight", "grid", "stream")

# The kind enum mirrors the Design Studio fixture's per-tile shapes
# (graph://9b0bdb8e-a9c4). Adding a new kind here is the substrate-side
# contract; the page renders unknown kinds as a generic markdown body.
VALID_TILE_KINDS = (
    "thinking", "quote", "code", "findings",
    "status", "action", "link", "session-card",
    "merge", "phases", "markdown", "image",
    "table", "bead-card", "progress",
)

VALID_TILE_WIDTHS = ("full", "half", "third")


# SYNOPSIS must precede ``register_schema`` so ``flush_schema_meta`` can
# read it on the first writable connection (pitfall graph://4f142305-6fb).
SYNOPSIS = {
    "summary": (
        "Settings Nexus surface: a singleton scene row (banner / anchor / "
        "presenter) plus keyed timeline tiles (kind-discriminated journal "
        "entries with kind-specific data payloads)"
    ),
    "nouns": [
        "nexus", "settings nexus", "nexus scene", "nexus tile",
        "anchor sentence", "presenter", "timeline", "journal",
        "tile kind", "focus tile",
    ],
    "related_set_ids": [],
}


# ── Scene (singleton) ────────────────────────────────────────────────


@singleton(key="active")
class NexusSceneV1(SettingSchema):
    """The Nexus page's banner + anchor row. Key: fixed ``active``.

    Carries title, subtitle, anchor sentence, presenter pointer
    (driving session source id + display label), optional focused
    tile id, and overall layout enum. Latest-write-wins via
    @singleton's ``set(payload)`` convenience method.
    """

    set_id = NEXUS_SCENE_SET_ID
    schema_revision = SCHEMA_REVISION

    title: str = field(
        required=True,
        description="Page banner headline (e.g. 'Settings').",
    )
    subtitle: str = field(
        default="",
        description="Sub-headline beneath the title.",
    )
    anchor_sentence: str = field(
        default="",
        description=(
            "One-paragraph anchor quote rendered as the indigo-bordered "
            "callout under the banner."
        ),
    )
    presenter: str = field(
        default="",
        description=(
            "Source id of the session driving the scene; surfaced "
            "verbatim in the banner's 'Presenter' line."
        ),
    )
    presenter_label: str = field(
        default="",
        description="Human-readable label for the presenter session.",
    )
    focus_tile_id: str = field(
        default="",
        description=(
            "Optional id of the tile the scene wants to spotlight; "
            "the page may render this tile larger or pin it on top."
        ),
    )
    layout: str = field(
        default="grid",
        enum=list(VALID_SCENE_LAYOUTS),
        description=(
            "Overall layout hint: ``spotlight`` (focused tile dominates), "
            "``grid`` (equal cards), ``stream`` (vertical journal)."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        title = payload.get("title")
        if not isinstance(title, str) or not title.strip():
            raise SchemaValidationError(
                f"{cls.__name__}: 'title' must be a non-empty string"
            )
        for str_field in (
            "subtitle", "anchor_sentence", "presenter",
            "presenter_label", "focus_tile_id",
        ):
            if str_field in payload and payload[str_field] is not None \
                    and not isinstance(payload[str_field], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {str_field!r} must be a string or null"
                )
        if "layout" in payload:
            v = payload["layout"]
            if v is not None and v not in VALID_SCENE_LAYOUTS:
                raise SchemaValidationError(
                    f"{cls.__name__}: 'layout' must be one of "
                    f"{VALID_SCENE_LAYOUTS}, got {v!r}"
                )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


# ── Tile (keyed-per-entity) ──────────────────────────────────────────


@keyed_per_entity
class NexusTileV1(SettingSchema):
    """A single timeline entry. Key: caller-supplied tile id (uuid or
    natural slug).

    Tiles render in the journal in descending ``order``. ``kind``
    drives the per-tile renderer (status pill / markdown body / code
    block / merge row / etc.) and selects which keys in ``data`` are
    meaningful. Common fields (``title`` / ``body`` / ``ts``) cover
    the simplest cases; ``data`` is the open-ended bag for variant-
    specific fields per the Design Studio fixture.
    """

    set_id = NEXUS_TILE_SET_ID
    schema_revision = SCHEMA_REVISION

    order: int = field(
        default=0,
        description="Sort key; higher orders render first (descending).",
    )
    width: str = field(
        default="full",
        enum=list(VALID_TILE_WIDTHS),
        description="Width hint for the timeline grid.",
    )
    kind: str = field(
        required=True,
        enum=list(VALID_TILE_KINDS),
        description=(
            "Discriminator selecting the renderer + the keys "
            "consulted from ``data``."
        ),
    )
    title: str = field(
        default="",
        description="Optional headline rendered above ``body``.",
    )
    body: str = field(
        default="",
        description=(
            "Primary text content. For ``markdown`` kind this is "
            "the markdown source; for ``code`` it is the code block."
        ),
    )
    ts: str = field(
        default="",
        description="Optional ISO-8601 timestamp surfaced in the meta-row.",
    )
    data: dict = field(
        default_factory=dict,
        description=(
            "Kind-specific payload. The page reads keys it knows about "
            "for the matching ``kind`` (e.g. ``state`` + ``label`` + "
            "``detail`` for ``status`` tiles, ``language`` + ``caption`` "
            "for ``code`` tiles, etc.)."
        ),
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        kind = payload.get("kind")
        if not isinstance(kind, str) or not kind:
            raise SchemaValidationError(
                f"{cls.__name__}: missing or empty required field 'kind'"
            )
        if kind not in VALID_TILE_KINDS:
            raise SchemaValidationError(
                f"{cls.__name__}: 'kind' must be one of {VALID_TILE_KINDS}, "
                f"got {kind!r}"
            )
        if "order" in payload and not isinstance(payload["order"], int):
            raise SchemaValidationError(
                f"{cls.__name__}: 'order' must be an int"
            )
        if "width" in payload:
            v = payload["width"]
            if v is not None and v not in VALID_TILE_WIDTHS:
                raise SchemaValidationError(
                    f"{cls.__name__}: 'width' must be one of "
                    f"{VALID_TILE_WIDTHS}, got {v!r}"
                )
        for str_field in ("title", "body", "ts"):
            if str_field in payload and payload[str_field] is not None \
                    and not isinstance(payload[str_field], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {str_field!r} must be a string or null"
                )
        if "data" in payload and payload["data"] is not None \
                and not isinstance(payload["data"], dict):
            raise SchemaValidationError(
                f"{cls.__name__}: 'data' must be a dict or null"
            )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


# Schemas auto-register via ``SettingSchema.__init_subclass__``.
