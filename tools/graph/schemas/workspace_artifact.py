"""Schema: ``autonomy.workspace.artifact#1``.

Declares the Setting payload shape for a workspace artifact — a file the
project expects to find under ``/etc/autonomy/artifacts/`` inside its
container. The Setting is the contract; the file itself lives on the
operator's host filesystem and is resolved by the artifact layering rule
(see graph://bc0dda40-f56). The Setting **never** carries file content.

Composite key: ``<workspace-id>:<artifact-name>`` (e.g.
``enterprise-ng:license.yaml``). Both segments are redundant with the key
and therefore dropped from the payload.
"""

from __future__ import annotations

from .registry import (
    publication_band,
    home,
    home,
    keyed_per_entity,
    SchemaValidationError,
    SettingSchema,
)


SET_ID = "autonomy.workspace.artifact"
SCHEMA_REVISION = 1

VALID_SCOPES = (
    "personal-org",
    "shared-org",
    "personal-workspace",
    "shared-workspace",
)


SYNOPSIS = {
    "summary": (
        "Workspace artifact contracts: files the workspace expects under "
        "/etc/autonomy/artifacts/, scoped per org/workspace"
    ),
    "nouns": [
        "artifact", "workspace artifact", "file mount",
        "artifact scope", "license file",
    ],
    "related_set_ids": [
        "autonomy.workspace#1",
        "autonomy.artifact-path#1",
        "autonomy.workspace.mount#1",
    ],
}


@publication_band(min="raw", max="curated")
@keyed_per_entity(
    key_strategy="workspace_id:artifact_name",
    # The first segment identifies the workspace that declares this
    # artifact, so every artifact of a workspace is reachable from it.
    key_references={"workspace_id": "autonomy.workspace"},
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
class WorkspaceArtifactV1(SettingSchema):
    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "scope": {
            "type": "string",
            "required": True,
            "description": (
                "Resolution scope for the artifact's host file. Setting key "
                "is '<workspace-id>:<artifact-name>'."
            ),
            "enum": list(VALID_SCOPES),
        },
        "required": {
            "type": "boolean",
            "description": "Whether the artifact is required at workspace launch",
            "default": True,
        },
        "description": {
            "type": "string",
            "description": "Operator-facing description of what this artifact contains",
        },
        "help": {
            "type": "string",
            "description": "Long-form help shown when the artifact is missing",
        },
    }

    @classmethod
    def validate(cls, payload: dict) -> None:
        super().validate(payload)

        extra = set(payload) - {"scope", "required", "description", "help"}
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown fields {sorted(extra)!r}; "
                f"the artifact name and workspace id live in the Setting key, "
                f"not the payload"
            )

        if "scope" not in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: 'scope' is required"
            )
        scope = payload["scope"]
        if scope not in VALID_SCOPES:
            raise SchemaValidationError(
                f"{cls.__name__}: invalid scope {scope!r}; "
                f"must be one of {VALID_SCOPES}"
            )

        if "required" in payload and not isinstance(payload["required"], bool):
            raise SchemaValidationError(
                f"{cls.__name__}: 'required' must be bool, "
                f"got {type(payload['required']).__name__}"
            )

        for opt in ("description", "help"):
            if opt in payload and payload[opt] is not None \
                    and not isinstance(payload[opt], str):
                raise SchemaValidationError(
                    f"{cls.__name__}: {opt!r} must be a string or null, "
                    f"got {type(payload[opt]).__name__}"
                )
