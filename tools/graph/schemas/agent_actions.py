"""``dashboard.agent-actions#2`` — per-(org, asset_type) agentic action registry.

Each Setting member declares one action available in the dashboard's
agentic-actions dropdown for the asset's owning org. The dropdown is
strictly own-org-of-asset: an action defined in anchore.db only renders
on anchore notes; cross-org adoption is via canonical promotion of the
member into the receiving org's DB.

Revision history:

* ``#1`` — initial shape.
* ``#2`` — adds optional ``input_prompt``: when set, the dashboard pops
  a small input modal seeded with this label before dispatching, and
  the operator's input lands as ``custom_input`` on the dispatch payload
  (available as ``{custom_input}`` inside the action's ``prompt_template``).
"""

from __future__ import annotations

import string
from typing import Any

from .registry import SchemaValidationError, SettingSchema, keyed_per_entity, publication_band
from .registry import home


AGENT_ACTIONS_SET_ID = "dashboard.agent-actions"
AGENT_ACTIONS_REVISION = 2

VALID_ASSET_TYPES = (
    "note", "bead", "session", "agent-run", "conversation",
    "docs", "musing", "status", "design", "*",
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
    "card_summary", "input_prompt",
}

_CARD_SUMMARY_FORMATS = ("text", "badge", "stars", "code")


AGENT_ACTION_TEMPLATE_ROOTS = (
    "asset",
    "source",
    "bead",
    "design",
    "tags",
    "dispatched_by_session",
    "member_key",
    "custom_input",
)


def _template_field_root(field_name: str) -> str:
    root = field_name.split("[", 1)[0]
    return root.split(".", 1)[0]


def validate_prompt_template(template: str) -> None:
    """Validate prompt-template syntax and allowed placeholder roots.

    Agent action prompts render through Python ``str.format`` at dispatch
    time. Literal braces in shell/Python examples must therefore be escaped as
    ``{{`` and ``}}``. This schema-level check rejects malformed templates
    before they can be installed as graph Settings.
    """
    try:
        referenced = {
            field for _, field, _, _ in string.Formatter().parse(template)
            if field and not field[0].isdigit()
        }
    except ValueError as exc:
        raise SchemaValidationError(
            f"AgentAction: invalid prompt_template format syntax: {exc}"
        ) from exc

    unknown = {
        field for field in referenced
        if _template_field_root(field) not in AGENT_ACTION_TEMPLATE_ROOTS
    }
    if unknown:
        raise SchemaValidationError(
            "AgentAction: prompt_template references undefined placeholder(s) "
            f"{sorted(unknown)}"
        )


@publication_band(min="raw", max="curated")
@keyed_per_entity(key_strategy="action_name")
class AgentActionV2(SettingSchema):
    """Shape of a ``dashboard.agent-actions#2`` member payload.

    Required: ``asset_type``, ``label``.
    Required-unless-universal: ``model``, ``prompt_template``.
    Optional: ``icon``, ``estimated_seconds``, ``writes``, ``universal``,
    ``workspace``, ``card_summary``, ``input_prompt``.

    ``input_prompt`` (added in #2) makes the action operator-input-aware:
    when set, the dashboard renders an input modal seeded with this label
    and forwards the operator's text as ``custom_input`` on the dispatch
    payload, which the prompt template can interpolate via
    ``{custom_input}``.
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
        # NOT unconditionally required: universal actions run without a
        # model/prompt_template. The "required unless universal" rule is
        # conditional, which declared-field metadata cannot express, so it is
        # enforced in validate() below. Marking these required here would
        # (via enforce_declared_fields) wrongly reject every universal member.
        "model": {
            "type": "string",
            "description": "Anthropic model id for the action (required unless universal)",
        },
        "prompt_template": {
            "type": "string",
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
        "input_prompt": {
            "type": "string",
            "description": (
                "When present, the dashboard pops an input modal seeded "
                "with this label before dispatching the action. The "
                "operator's input lands as ``custom_input`` on the "
                "dispatch payload and is available as ``{custom_input}`` "
                "inside the action's prompt_template."
            ),
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

        for key in ("icon", "model", "prompt_template", "workspace",
                    "input_prompt"):
            if key in payload and payload[key] is not None:
                if not isinstance(payload[key], str):
                    raise SchemaValidationError(
                        f"{cls.__name__}: {key!r} must be a string or null"
                    )
        if isinstance(payload.get("prompt_template"), str):
            validate_prompt_template(payload["prompt_template"])

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

    @classmethod
    def upconvert_from_prev(cls, payload: dict) -> dict:
        """Identity upconvert: ``#1`` payloads pass through unchanged.

        ``input_prompt`` is optional in ``#2``, so a ``#1`` row without it is
        already a valid ``#2`` payload.
        """
        return dict(payload)


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
@keyed_per_entity(key_strategy="action_name")
class AgentActionV1(SettingSchema):
    """Legacy ``dashboard.agent-actions#1`` shape — identical to ``#2``
    minus the optional ``input_prompt`` field.

    Kept registered so stored rows at revision 1 still resolve to a known
    schema; new writes should target ``#2``. The ``#1 → #2`` upconverter
    is the identity (omitted ``input_prompt`` is just absent), so a
    ``graph set migrate dashboard.agent-actions --target 2`` rewrites
    every legacy row at the new revision without changing payloads.
    """

    set_id = AGENT_ACTIONS_SET_ID
    schema_revision = 1

    _field_metadata: dict[str, dict] = {
        k: v for k, v in AgentActionV2._field_metadata.items()
        if k != "input_prompt"
    }

    @classmethod
    def validate(cls, payload: Any) -> None:
        if isinstance(payload, dict) and "input_prompt" in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: 'input_prompt' was added in #2; reject "
                "at #1 so storage stamps the right revision"
            )
        AgentActionV2.validate(payload)
