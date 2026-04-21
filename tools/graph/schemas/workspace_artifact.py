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
    SchemaValidationError,
    SettingSchema,
    register_schema,
)


SET_ID = "autonomy.workspace.artifact"
SCHEMA_REVISION = 1

VALID_SCOPES = (
    "personal-org",
    "shared-org",
    "personal-workspace",
    "shared-workspace",
)


class WorkspaceArtifactV1(SettingSchema):
    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

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


register_schema(SET_ID, SCHEMA_REVISION, WorkspaceArtifactV1)
