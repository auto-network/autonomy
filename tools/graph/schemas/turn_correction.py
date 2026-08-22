"""``autonomy.workspace.turn_correction#1`` — turn-correction behavior Setting.

Per-workspace knobs that drive whether and how user-facing agents are
reminded to consider emitting ``graph turn-correction suggest`` when a
user message is garbled enough to risk a perception gap.

The Setting is keyed by ``workspace.id`` so every workspace can opt in or
out independently. The runtime workspace primer reads the resolved
payload and renders mode-appropriate guidance into the agent's context;
the agent itself never reads this Setting directly.

Spec: graph://0d3f750f-f9c (Setting Primitive). Companion bead trail:
``auto-edec1.5`` (this productization), ``auto-edec1.1`` (the underlying
``graph turn-correction suggest`` CLI), and ``auto-edec1.2`` (sparse
overlay state on the dashboard side).

Defaults are intentionally action-biased: if no workspace-specific
Setting is written, the primer should still push agents to use turn
corrections aggressively whenever they add even slight value.
"""

from __future__ import annotations

from typing import Any

from .registry import (
    publication_band,
    home,
    home,
    SchemaValidationError,
    SettingSchema,
    keyed_per_entity,
)


SET_ID = "autonomy.workspace.turn_correction"
SCHEMA_REVISION = 1


VALID_AGGRESSIVENESS = ("off", "conservative", "balanced", "aggressive")


# Defaults applied when a workspace has no Setting row, or when the
# render layer wants to know the "absent setting" baseline. Kept as a
# module-level dict so callers (primer renderer, dashboard, tests) all
# read the same baseline.
DEFAULT_PAYLOAD: dict[str, Any] = {
    "enabled": True,
    "aggressiveness": "aggressive",
    "persist_accepts_to_graph": False,
}


SYNOPSIS = {
    "summary": (
        "Per-workspace turn-correction behavior knobs (enabled, "
        "aggressiveness, persist accepts) that drive runtime primer "
        "guidance about ``graph turn-correction suggest``."
    ),
    "nouns": [
        "turn correction", "turn-correction", "perception gap",
        "user message", "garbled message", "correction overlay",
    ],
    "related_set_ids": [
        "autonomy.workspace#1",
    ],
}


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@publication_band(min="raw", max="curated")
@home("organization")
@keyed_per_entity(key_strategy="workspace_id")
class TurnCorrectionSettingsV1(SettingSchema):
    """Shape of an ``autonomy.workspace.turn_correction#1`` payload.

    All fields optional — the primer renderer applies
    :data:`DEFAULT_PAYLOAD` for any field the Setting omits, so an
    operator can write a partial Setting (e.g. ``{"aggressiveness":
    "off"}``) without restating the rest.
    """

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "enabled": {
            "type": "boolean",
            "description": (
                "Master switch. When false the primer replaces the "
                "detailed guidance with a short disabled note."
            ),
            "default": DEFAULT_PAYLOAD["enabled"],
        },
        "aggressiveness": {
            "type": "string",
            "description": (
                "How insistently the primer should encourage emitting "
                "``graph turn-correction suggest``. ``off`` shows the "
                "command for reference but tells the agent not to "
                "volunteer corrections; ``conservative`` reserves it "
                "for clearly garbled messages; ``balanced`` applies "
                "whenever a perception gap is plausible; "
                "``aggressive`` (default) instructs the agent to err "
                "on the side of suggesting a correction."
            ),
            "enum": list(VALID_AGGRESSIVENESS),
            "default": DEFAULT_PAYLOAD["aggressiveness"],
        },
        "persist_accepts_to_graph": {
            "type": "boolean",
            "description": (
                "When true, the primer instructs the agent that "
                "operator-accepted corrections will be persisted into "
                "the knowledge graph (in addition to the dashboard's "
                "sparse overlay state)."
            ),
            "default": DEFAULT_PAYLOAD["persist_accepts_to_graph"],
        },
        "instruction_template": {
            "type": "string",
            "description": (
                "Optional override for the lead-in sentence the primer "
                "renders above the canonical command. Empty/missing "
                "uses the renderer's mode-specific default."
            ),
        },
    }

    @classmethod
    def validate(cls, payload: Any) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        extra = set(payload) - set(cls._field_metadata)
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )
        if "enabled" in payload \
                and not isinstance(payload["enabled"], bool):
            raise SchemaValidationError(
                f"{cls.__name__}: 'enabled' must be a bool"
            )
        if "aggressiveness" in payload:
            val = payload["aggressiveness"]
            if not isinstance(val, str) or val not in VALID_AGGRESSIVENESS:
                raise SchemaValidationError(
                    f"{cls.__name__}: 'aggressiveness' must be one of "
                    f"{VALID_AGGRESSIVENESS}, got {val!r}"
                )
        if "persist_accepts_to_graph" in payload \
                and not isinstance(
                    payload["persist_accepts_to_graph"], bool
                ):
            raise SchemaValidationError(
                f"{cls.__name__}: 'persist_accepts_to_graph' must be a bool"
            )
        if "instruction_template" in payload \
                and payload["instruction_template"] is not None \
                and not isinstance(payload["instruction_template"], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'instruction_template' must be a string or null"
                )


def resolve_payload(payload: dict | None) -> dict:
    """Layer ``payload`` (or ``{}``) over :data:`DEFAULT_PAYLOAD`.

    Used by the primer renderer so a missing or partial Setting still
    yields a fully-populated decision object — the renderer never has to
    repeat ``payload.get(..., default)`` on every field.
    """
    merged = dict(DEFAULT_PAYLOAD)
    if payload:
        for k, v in payload.items():
            if v is None:
                continue
            merged[k] = v
    return merged
