"""``autonomy.workspace.capability.enable#1`` — workspace-level enablement.

A *workspace enable* row decides what a specific workspace actually turns
on for a given capability contract. It can:

* enable the contract (the default when ``enabled`` is omitted)
* explicitly disable it — this lets a workspace opt out of a capability
  the org has installed (see graph://86e04207-a25)
* pin to a specific ``contract_version`` or leave it unpinned to follow
  the current working version
* disable just the agentic projection (skill/primer) or just the
  deterministic projection (Dashboard/Worktrees API)
* override workspace-local settings via ``workspace_overrides``

Versioning semantics: ``contract_version`` may be ``None`` (or omitted),
which means *unpinned* — the resolver uses the current working version
of the contract. A concrete integer pins to that canonical revision.
The release/pin workflow is out of scope for this bead.

The workspace launcher resolves this row through the same versioned-chain
validator used by the on-demand readiness check. A broken enabled chain is
reported as not ready and refused at launch instead of being silently omitted.
"""

from __future__ import annotations

from typing import Any

from .registry import SchemaValidationError, SettingSchema, keyed_per_entity, publication_band
from .registry import home


SET_ID = "autonomy.workspace.capability.enable"
SCHEMA_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Per-workspace capability enable: opt in/out of the org's installed "
        "capability for a contract, with optional version pin and overrides"
    ),
    "nouns": [
        "capability", "enable", "workspace capability",
        "opt out", "disable", "projection",
    ],
    "related_set_ids": [
        "autonomy.org.capability.install#1",
        "autonomy.capability.contract#1",
        "autonomy.workspace#1",
    ],
}


_ALLOWED_TOP_LEVEL = {
    "contract",
    "contract_version",
    "enabled",
    "disable_agentic_projection",
    "disable_deterministic_projection",
    "workspace_overrides",
    "notes",
}

_BOOL_FIELDS = (
    "enabled",
    "disable_agentic_projection",
    "disable_deterministic_projection",
)


@publication_band(min="raw", max="curated")
@keyed_per_entity(
    key_strategy="workspace_id:contract_name",
    # Each key segment identifies a row in another set: the first is a
    # workspace, the second a capability contract. Declared, a walker can
    # find every enable row belonging to a given workspace, and report one
    # whose workspace or contract no longer exists.
    key_references={
        "workspace_id": "autonomy.workspace",
        "contract_name": "autonomy.capability.contract",
    },
)
#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; reading this as "anywhere but personal" refuses
#: writes that are correct.
@home("organization")
class WorkspaceCapabilityEnableV1(SettingSchema):
    """Shape of an ``autonomy.workspace.capability.enable#1`` payload.

    Required: ``contract`` (string).

    Optional: ``contract_version`` (int or None — None means unpinned and
    resolves to the current working version), ``enabled`` (bool, default
    true), ``disable_agentic_projection`` (bool),
    ``disable_deterministic_projection`` (bool), ``workspace_overrides``
    (object), ``notes`` (string).
    """

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "contract": {
            "type": "string",
            "required": True,
            "description": "Contract identifier this workspace enables/disables",
            # The value IS another set's key. Declaring it makes the edge
            # walkable: a check can follow it without knowing what a
            # capability is.
            "references": "autonomy.capability.contract",
        },
        "contract_version": {
            "type": "integer",
            "description": (
                "Pinned canonical contract version. Null/omitted = unpinned, "
                "follows the current working version."
            ),
        },
        "enabled": {
            "type": "boolean",
            "description": "Whether the contract is enabled for this workspace",
            "default": True,
        },
        "disable_agentic_projection": {
            "type": "boolean",
            "description": "Disable just the agentic projection (skill / primer)",
            "default": False,
        },
        "disable_deterministic_projection": {
            "type": "boolean",
            "description": (
                "Disable just the deterministic projection "
                "(Dashboard / Worktrees API)"
            ),
            "default": False,
        },
        "workspace_overrides": {
            "type": "object",
            "description": "Workspace-local overrides applied on top of the org install",
        },
        "notes": {
            "type": "string",
            "description": "Free-form notes",
        },
    }

    @classmethod
    def readiness_findings(cls, *, key, payload, org, read):
        """Validate the conventional and versioned capability-chain edges.

        ``check_setting`` supplies the reader, keeping storage and visibility
        policy in the Settings layer.  The actual edge rules are the same pure
        primitive the workspace launcher consumes.
        """
        from tools.graph.capability_chain import validate_capability_chain

        _workspace, separator, contract_key = str(key).partition(":")
        if not separator or not contract_key or payload.get("enabled", True) is False:
            return ()

        contract_row = read(
            "autonomy.capability.contract", contract_key, org=org, peers=None,
        )
        install_row = read(
            "autonomy.org.capability.install", contract_key, org=org, peers=[],
        )
        install = (install_row or {}).get("payload")
        impl_row = None
        if isinstance(install, dict) and isinstance(install.get("implementation"), str):
            impl_row = read(
                "autonomy.capability.impl", install["implementation"],
                org=org, peers=None,
            )
        _chain, issues = validate_capability_chain(
            contract_key=contract_key,
            enable=payload,
            contract=(contract_row or {}).get("payload"),
            install=install,
            implementation=(impl_row or {}).get("payload"),
        )
        return issues

    @classmethod
    def validate(cls, payload: Any) -> None:  # noqa: C901 — flat checks
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )

        extra = set(payload) - _ALLOWED_TOP_LEVEL
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )

        contract = payload.get("contract")
        if not isinstance(contract, str) or not contract:
            raise SchemaValidationError(
                f"{cls.__name__}: 'contract' is required and must be a "
                f"non-empty string"
            )

        if "contract_version" in payload:
            cv = payload["contract_version"]
            if cv is not None:
                if isinstance(cv, bool) or not isinstance(cv, int):
                    raise SchemaValidationError(
                        f"{cls.__name__}: 'contract_version' must be an "
                        f"integer or null, got {type(cv).__name__}"
                    )
                if cv < 1:
                    raise SchemaValidationError(
                        f"{cls.__name__}: 'contract_version' must be >= 1, "
                        f"got {cv}"
                    )

        for key in _BOOL_FIELDS:
            if key in payload and not isinstance(payload[key], bool):
                raise SchemaValidationError(
                    f"{cls.__name__}: {key!r} must be a bool, "
                    f"got {type(payload[key]).__name__}"
                )

        if "workspace_overrides" in payload:
            wo = payload["workspace_overrides"]
            if not isinstance(wo, dict):
                raise SchemaValidationError(
                    f"{cls.__name__}: 'workspace_overrides' must be an "
                    f"object, got {type(wo).__name__}"
                )

        if "notes" in payload and not isinstance(payload["notes"], str):
            raise SchemaValidationError(
                f"{cls.__name__}: 'notes' must be a string"
            )
