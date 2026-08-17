"""``autonomy.workspace.mount#1`` — workspace host-directory mount Setting.

A **mount** declares that a host directory should be bind-mounted into a
workspace container at a specific container path. It mirrors the
Artifact Layering pattern (graph://bc0dda40-f56) but targets *directories*
at arbitrary container paths (not single files at
``/etc/autonomy/artifacts/<name>``).

The Setting is the declaration contract. The directory content lives on
the operator's host at ``host_path`` and never enters the graph. Mounts
typically live in the owning workspace's org DB at ``state=raw`` for
operator-local harnesses (e.g. the ``enterprise-ng:vuln-diff`` harness).

Spec refs: graph://0d3f750f-f9c (Setting Primitive), graph://bcce359d-a1d
(Cross-Org Search), graph://bc0dda40-f56 (Artifact Layering).

Composite key convention (graph://0d3f750f-f9c § Composite keys):
``<workspace-slug>:<mount-name>``. Query via
``ops.read_set("autonomy.workspace.mount", prefix=<workspace-slug>, ...)``
— the ``:`` separator is auto-appended by the ops layer.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


SET_ID = "autonomy.workspace.mount"
SCHEMA_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Workspace mount declarations: bind-mounted host directories at "
        "arbitrary container paths"
    ),
    "nouns": [
        "mount", "bind mount", "directory", "host path", "container path",
        "ro", "rw", "read-only",
    ],
    "related_set_ids": [
        "autonomy.workspace#1",
        "autonomy.workspace.artifact#1",
    ],
}


class WorkspaceMountV1(BaseModel):
    """Host directory bind-mounted into a workspace container.

    Stored as a Setting in ``set_id=autonomy.workspace.mount#1`` with
    composite key ``<workspace-slug>:<mount-name>``
    (e.g. ``enterprise-ng:vuln-diff``). Lives in the org DB of the
    workspace that owns the mount — typically ``state=raw`` for
    operator-local mounts like test harnesses.
    """

    host_path: str = Field(..., description="Absolute host path to directory")
    container_path: str = Field(..., description="Absolute path inside container")
    mode: Literal["ro", "rw"] = "ro"
    description: str | None = None
    required: bool = True

    @field_validator("container_path")
    @classmethod
    def container_must_be_absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"container_path must be absolute: {v!r}")
        return v

    @field_validator("host_path")
    @classmethod
    def host_must_be_absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"host_path must be absolute: {v!r}")
        return v


# Registry bridge — the schema registry speaks ``SettingSchema`` (a plain
# class with a classmethod ``validate``). Pydantic BaseModel validation
# goes through ``model_validate``; wrap it so ``add_setting`` /
# dashboard validation call sites keep working unchanged.
from .registry import SettingSchema, SchemaValidationError, keyed_per_entity
from .registry import home


@keyed_per_entity(
    key_strategy="workspace_id:mount_name",
    # The first segment identifies the workspace this mount belongs to.
    # Declared, every mount of a given workspace is reachable from it.
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
class _WorkspaceMountSchemaAdapter(SettingSchema):
    """Adapts :class:`WorkspaceMountV1` (Pydantic) to the registry contract.

    Payload authors keep writing dicts (``graph set add`` reads YAML /
    JSON into a dict); this adapter runs them through Pydantic's
    validation at write time, translating ``ValidationError`` into the
    :class:`SchemaValidationError` the rest of the ops layer expects.
    """

    set_id = SET_ID
    schema_revision = SCHEMA_REVISION
    model = WorkspaceMountV1

    _field_metadata: dict[str, dict] = {
        "host_path": {
            "type": "string",
            "required": True,
            "description": "Absolute host path to the directory to mount",
        },
        "container_path": {
            "type": "string",
            "required": True,
            "description": "Absolute path inside the container to mount at",
        },
        "mode": {
            "type": "string",
            "description": "Mount mode — 'ro' for read-only, 'rw' for writable",
            "enum": ["ro", "rw"],
            "default": "ro",
        },
        "description": {
            "type": "string",
            "description": "Operator-facing description of what this mount provides",
        },
        "required": {
            "type": "boolean",
            "description": "Whether the mount is required at workspace launch",
            "default": True,
        },
    }

    @classmethod
    def validate(cls, payload) -> None:
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        try:
            cls.model.model_validate(payload)
        except Exception as exc:
            raise SchemaValidationError(
                f"{cls.__name__}: {exc}"
            ) from exc
