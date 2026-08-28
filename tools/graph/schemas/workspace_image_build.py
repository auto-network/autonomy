"""``autonomy.workspace.image-build#1`` — last build of a workspace image
ON THIS MACHINE.

Machine-homed: "what was built here" is true on this computer and false
on the next (docker image stores are per-machine). One machine store
serves every org on the box, so the org is in the key — a bare
``workspace_id`` could not distinguish two orgs' workspaces of the same
name. Written ONLY by the host builder; read by the dashboard so a
session can see whether its provision edit built.

The image name is deliberately NOT a field: the tag is derived from the
key (``<org>/<workspace-id>``), so storing it would duplicate the key.
The ``digest`` identifies what was actually produced.
"""

from __future__ import annotations

from .registry import (
    SchemaValidationError,
    SettingSchema,
    home,
    keyed_per_entity,
    publication_band,
)


SET_ID = "autonomy.workspace.image-build"
SCHEMA_REVISION = 1

_HEX = set("0123456789abcdef")


SYNOPSIS = {
    "summary": (
        "Per-machine build status for <org>/<workspace-id> images built "
        "from autonomy.workspace.provision dockerfiles"
    ),
    "nouns": [
        "image build", "build status", "docker build", "workspace image",
    ],
    "related_set_ids": [
        "autonomy.workspace.provision#1",
        "autonomy.workspace#1",
    ],
}


@publication_band(min="raw", max="curated")
@home("machine")
@keyed_per_entity(
    key_strategy="org:workspace_id",
    key_references={"workspace_id": "autonomy.workspace"},
)
class WorkspaceImageBuildV1(SettingSchema):
    """Shape of an ``autonomy.workspace.image-build#1`` payload."""

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION

    _field_metadata: dict[str, dict] = {
        "content_hash": {
            "type": "string",
            "required": True,
            "description": (
                "sha256 hex of the dockerfile text this build consumed "
                "AFTER personal-shadow resolution on this machine. The "
                "builder rebuilds when the current resolved content "
                "hashes differently."
            ),
        },
        "built_at": {
            "type": "string",
            "required": True,
            "description": "UTC ISO-8601 time the build finished",
        },
        "digest": {
            "type": "string",
            "description": (
                "Image content digest (sha256:...) on success; absent on "
                "failure"
            ),
        },
        "error": {
            "type": "string",
            "description": (
                "Failure message on a failed build; absent on success"
            ),
        },
    }

    @classmethod
    def validate(cls, payload: dict) -> None:
        super().validate(payload)
        if bool(payload.get("digest")) == bool(payload.get("error")):
            raise SchemaValidationError(
                f"{cls.__name__}: exactly one of 'digest' (success) or "
                f"'error' (failure) must be present"
            )
        content_hash = payload.get("content_hash", "")
        if len(content_hash) != 64 or not set(content_hash) <= _HEX:
            raise SchemaValidationError(
                f"{cls.__name__}: 'content_hash' must be 64 lowercase hex "
                f"chars (sha256)"
            )
