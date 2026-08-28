"""``autonomy.workspace.provision#1`` — workspace runtime provisioning.

Graph-native home for the content previously at
``agents/projects/<workspace-id>/{startup.sh,Dockerfile}`` — host-local
and gitignored because that repo is public (org content was purged from
its history on 2026-08-01 and must not return). A row lives in the owning
org's database, is read with ``peers=[]``, and never crosses an org
boundary.

Personal override: a personal-store row under the same key shadows the
org row PER FIELD (present fields win, absent fields fall through) via
the consumer's deliberate second read of the personal store — never via
federation, which the publication band makes structurally impossible: a
personal or machine row can never reach ``published``, so it can never
enter any federated resolution. The only path into a launch or a build
is an explicit read by the host-side consumer.

The image name is never stored: the builder derives ``<org>/<workspace-id>``
from the org and the key. Build state lives in
``autonomy.workspace.image-build#1`` (machine-homed), never here.
"""

from __future__ import annotations

from .registry import (
    SchemaValidationError,
    SettingSchema,
    home,
    keyed_per_entity,
    publication_band,
)


SET_ID = "autonomy.workspace.provision"
SCHEMA_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Workspace runtime provisioning content: the startup script "
        "materialized to /startup.sh and the Dockerfile the host builder "
        "turns into the <org>/<workspace-id> image."
    ),
    "nouns": [
        "startup script", "startup.sh", "Dockerfile",
        "workspace image", "provisioning",
    ],
    "related_set_ids": [
        "autonomy.workspace#1",
        "autonomy.workspace.primer#1",
        "autonomy.workspace.image-build#1",
    ],
}


#: Not forced into any one store. This records that the question was
#: ASKED -- must this live in the operator's own database, or on
#: this machine alone? -- and answered no, which is different
#: from nobody having considered it.
#:
#: It is not a prohibition. The operator owns workspaces, so
#: their database is the organizational home of their own
#: things; a personal-store row is legal and is the override
#: mechanism for that operator's own launches and builds.
@publication_band(min="raw", max="curated")
@home("organization")
@keyed_per_entity(
    key_strategy="workspace_id",
    key_references={"workspace_id": "autonomy.workspace"},
)
class WorkspaceProvisionV1(SettingSchema):
    """Shape of an ``autonomy.workspace.provision#1`` payload."""

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "startup_script": {
            "type": "string",
            "description": (
                "Shell script materialized to run_dir/startup.sh and "
                "mounted read-only at /startup.sh; the dind entrypoint "
                "backgrounds it. Whole-field: a personal-store row's "
                "value replaces this one for that operator's launches."
            ),
        },
        "dockerfile": {
            "type": "string",
            "description": (
                "Dockerfile content the host builder builds with a "
                "Dockerfile-ONLY context and tags <org>/<workspace-id>. "
                "The image name derives from org + key and is never "
                "stored. Build state lives in "
                "autonomy.workspace.image-build#1 (machine-homed)."
            ),
        },
        "description": {
            "type": "string",
            "description": "Operator-facing note on what this provisions",
        },
    }

    @classmethod
    def validate(cls, payload: dict) -> None:
        super().validate(payload)
        if not (payload.get("startup_script") or payload.get("dockerfile")):
            raise SchemaValidationError(
                f"{cls.__name__}: at least one of 'startup_script' or "
                f"'dockerfile' must be present and non-empty"
            )
        dockerfile = payload.get("dockerfile")
        if dockerfile is not None and "FROM" not in dockerfile:
            raise SchemaValidationError(
                f"{cls.__name__}: 'dockerfile' has no FROM instruction"
            )
