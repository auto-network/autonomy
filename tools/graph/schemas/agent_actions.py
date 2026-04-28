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
    "note", "session", "agent-run", "conversation",
    "docs", "musing", "status", "*",
)

_ALLOWED_FIELDS = {
    "asset_type", "label", "icon", "model", "prompt_template",
    "estimated_seconds", "writes", "universal",
}


class AgentActionV1(SettingSchema):
    """Shape of a ``dashboard.agent-actions#1`` member payload.

    Required: ``asset_type``, ``label``.
    Required-unless-universal: ``model``, ``prompt_template``.
    Optional: ``icon``, ``estimated_seconds``, ``writes``, ``universal``.
    """

    set_id = AGENT_ACTIONS_SET_ID
    schema_revision = AGENT_ACTIONS_REVISION

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

        for key in ("icon", "model", "prompt_template"):
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

        extra = set(payload) - _ALLOWED_FIELDS
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )


register_schema(AGENT_ACTIONS_SET_ID, AGENT_ACTIONS_REVISION, AgentActionV1)
