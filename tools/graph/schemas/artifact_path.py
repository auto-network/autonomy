"""Schema: ``autonomy.artifact-path#1``.

Personal Setting that overrides where an artifact lives on the operator's
host filesystem, replacing the Artifact Layering default
(``data/artifacts/{shared|personal}/{org}[/{workspace}]/{name}``). Lives in
``personal.db`` — this Setting is per-operator, never published.

Composite key: ``<org>:<name>`` — the owning org slug plus the artifact
name. The Setting applies to all workspaces in that org that declare an
artifact with the matching name. File content never lives in the Setting;
only the host path does.

Spec: graph://0d3f750f-f9c (Setting Primitive), graph://bc0dda40-f56
(Artifact Layering), graph://d970d946-f95 (Org Registry).
"""

from __future__ import annotations

from .registry import (
    keyed_per_entity,
    SchemaValidationError,
    SettingSchema,
)


SET_ID = "autonomy.artifact-path"
SCHEMA_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Operator-local override for an artifact's host filesystem path"
    ),
    "nouns": [
        "artifact", "path", "host filesystem",
        "artifact override", "personal override",
    ],
    "related_set_ids": [
        "autonomy.workspace.artifact#1",
    ],
}


@keyed_per_entity(key_strategy="artifact_name")
class ArtifactPathV1(SettingSchema):
    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "path": {
            "type": "string",
            "required": True,
            "description": (
                "Absolute host path where the artifact lives, replacing the "
                "Artifact-Layering default. Setting key is '<org>:<name>'."
            ),
        },
    }

    @classmethod
    def validate(cls, payload: dict) -> None:
        super().validate(payload)

        extra = set(payload) - {"path"}
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown fields {sorted(extra)!r}; "
                f"the org slug and artifact name live in the Setting key, "
                f"not the payload"
            )

        if "path" not in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: 'path' is required"
            )
        path = payload["path"]
        if not isinstance(path, str) or not path:
            raise SchemaValidationError(
                f"{cls.__name__}: 'path' must be a non-empty string"
            )
