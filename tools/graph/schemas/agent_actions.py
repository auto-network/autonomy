"""``dashboard.agent-actions#1`` — per-(org, asset_type) agentic action registry.

Each Setting member declares one action available in the dashboard's
agentic-actions dropdown for the asset's owning org. The dropdown is
strictly own-org-of-asset: an action defined in anchore.db only renders
on anchore notes; cross-org adoption is via canonical promotion of the
member into the receiving org's DB.
"""

from __future__ import annotations

from typing import Any

from .registry import SchemaValidationError, SettingSchema, register_schema


AGENT_ACTIONS_SET_ID = "dashboard.agent-actions"
AGENT_ACTIONS_REVISION = 1

VALID_ASSET_TYPES = (
    "note", "bead", "session", "agent-run", "conversation",
    "docs", "musing", "status", "*",
)


SYNOPSIS = {
    "summary": (
        "Per-(org, asset_type) agentic actions exposed in the dashboard's "
        "actions dropdown"
    ),
    "nouns": [
        "agent action", "dashboard action", "action menu",
        "agentic action", "asset action",
    ],
    "related_set_ids": [],
}

_ALLOWED_FIELDS = {
    "asset_type", "label", "icon", "model", "prompt_template",
    "estimated_seconds", "writes", "universal", "workspace",
    "card_summary",
}

_CARD_SUMMARY_FORMATS = ("text", "badge", "stars", "code")


class AgentActionV1(SettingSchema):
    """Shape of a ``dashboard.agent-actions#1`` member payload.

    Required: ``asset_type``, ``label``.
    Required-unless-universal: ``model``, ``prompt_template``.
    Optional: ``icon``, ``estimated_seconds``, ``writes``, ``universal``.
    """

    set_id = AGENT_ACTIONS_SET_ID
    schema_revision = AGENT_ACTIONS_REVISION

    _field_metadata: dict[str, dict] = {
        "asset_type": {
            "type": "string",
            "required": True,
            "description": "Asset kind this action applies to",
            "enum": list(VALID_ASSET_TYPES),
        },
        "label": {
            "type": "string",
            "required": True,
            "description": "Human-readable label shown in the dashboard dropdown",
        },
        "model": {
            "type": "string",
            "required": True,
            "description": "Anthropic model id for the action (required unless universal)",
        },
        "prompt_template": {
            "type": "string",
            "required": True,
            "description": "Prompt template fed to the model (required unless universal)",
        },
        "icon": {
            "type": "string",
            "description": "Icon identifier for the dropdown row",
        },
        "estimated_seconds": {
            "type": "integer",
            "description": "Approximate runtime in seconds, used for the progress UI",
        },
        "writes": {
            "type": "array",
            "description": "Identifiers of assets this action writes to (e.g. for cache invalidation)",
            "element": {"type": "string"},
        },
        "universal": {
            "type": "boolean",
            "description": (
                "When true, the action runs without a model/prompt_template "
                "(universal harness handles it)"
            ),
            "default": False,
        },
        "workspace": {
            "type": "string",
            "description": (
                "Optional explicit workspace id to materialize for this action. "
                "When omitted, the runtime uses the lightweight default "
                "workspace behavior."
            ),
        },
        "card_summary": {
            "type": "array",
            "description": (
                "Per-action timeline/trace card slots. Each slot is "
                "{label, path, format?} where path is a dotted accessor "
                "into the agent's decision dict (e.g. 'primary_outcome' "
                "or 'quality_scores.architecture_fit') and format is one "
                f"of {list(_CARD_SUMMARY_FORMATS)}. The dashboard renders "
                "these as a definition list under the agentic card."
            ),
            "element": {"type": "object"},
        },
    }

    @classmethod
    def validate(cls, payload: Any) -> None:  # noqa: C901 — flat checks
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )

        for key in ("asset_type", "label"):
            v = payload.get(key)
            if not isinstance(v, str) or not v:
                raise SchemaValidationError(
                    f"{cls.__name__}: missing or empty required field {key!r}"
                )

        if payload["asset_type"] not in VALID_ASSET_TYPES:
            raise SchemaValidationError(
                f"{cls.__name__}: asset_type must be one of "
                f"{VALID_ASSET_TYPES}, got {payload['asset_type']!r}"
            )

        universal = bool(payload.get("universal", False))
        if not universal:
            for key in ("model", "prompt_template"):
                v = payload.get(key)
                if not isinstance(v, str) or not v:
                    raise SchemaValidationError(
                        f"{cls.__name__}: non-universal members require {key!r}"
                    )

        for key in ("icon", "model", "prompt_template", "workspace"):
            if key in payload and payload[key] is not None:
                if not isinstance(payload[key], str):
                    raise SchemaValidationError(
                        f"{cls.__name__}: {key!r} must be a string or null"
                    )

        if "estimated_seconds" in payload:
            v = payload["estimated_seconds"]
            # Accept int but reject bool (bool is a subclass of int in Python).
            if isinstance(v, bool) or not isinstance(v, int):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'estimated_seconds' must be an integer"
                )

        if "writes" in payload:
            ws = payload["writes"]
            if not isinstance(ws, list) or not all(isinstance(s, str) for s in ws):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'writes' must be a list of strings"
                )

        if "universal" in payload and not isinstance(payload["universal"], bool):
            raise SchemaValidationError(
                f"{cls.__name__}: 'universal' must be a bool"
            )

        if "card_summary" in payload:
            cs = payload["card_summary"]
            if not isinstance(cs, list):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'card_summary' must be a list of slot dicts"
                )
            for i, slot in enumerate(cs):
                if not isinstance(slot, dict):
                    raise SchemaValidationError(
                        f"{cls.__name__}: card_summary[{i}] must be a dict"
                    )
                for key in ("label", "path"):
                    v = slot.get(key)
                    if not isinstance(v, str) or not v:
                        raise SchemaValidationError(
                            f"{cls.__name__}: card_summary[{i}] missing or empty {key!r}"
                        )
                fmt = slot.get("format")
                if fmt is not None and fmt not in _CARD_SUMMARY_FORMATS:
                    raise SchemaValidationError(
                        f"{cls.__name__}: card_summary[{i}] format must be one of "
                        f"{_CARD_SUMMARY_FORMATS}, got {fmt!r}"
                    )
                extra_keys = set(slot) - {"label", "path", "format"}
                if extra_keys:
                    raise SchemaValidationError(
                        f"{cls.__name__}: card_summary[{i}] has unknown keys: "
                        f"{sorted(extra_keys)}"
                    )

        extra = set(payload) - _ALLOWED_FIELDS
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


register_schema(AGENT_ACTIONS_SET_ID, AGENT_ACTIONS_REVISION, AgentActionV1)
