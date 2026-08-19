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

import posixpath
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SET_ID = "autonomy.workspace.mount"
SCHEMA_REVISION = 1


def _reject_traversal_segments(value: str, *, label: str) -> None:
    """Raise if any path segment is empty, ``.`` or ``..``.

    The realpath guard in the resolver is the runtime defence; this is
    defence-in-depth at write time, closer to whoever typed the row.
    """
    for seg in PurePosixPath(value).parts:
        if seg in ("", ".", ".."):
            raise ValueError(
                f"{label} must not contain empty, '.' or '..' segments: {value!r}"
            )


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
from .registry import home, readiness_gated_by


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
#: The row already says whether the container needs it. Named here so a
#: generic check can read it: an optional mount whose directory is absent
#: is worth reporting and does not mean the launch is broken.
@readiness_gated_by("required")
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
            "exists": "dir",
            "exists_frame": "platform-host",
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


# ── Revision 2 — org-free subpath into the autonomy-orgs volume ────────────
#
# `host_path` (absolute, machine-specific, unconstrained) is REPLACED by
# `subpath` (relative, org-free). The org is NEVER in the payload: the resolver
# prepends `orgs/<authenticated-session-org>/` at launch, realpath-resolves the
# result, and refuses anything escaping that org's tree. So a subpath that tries
# to name another org just resolves inside the caller's OWN org and the guard
# stops any `..`/symlink escape. This is a BREAKING change (a host_path string
# does not map to a subpath under different physical storage), so NO 1->2
# upconverter is registered — a caller requesting rev 2 drops any row still at
# rev 1 rather than resolving it wrong (registry.py:11-14 convention).
#
# The resolved target may be a FILE or a directory (folds in what the retired
# artifact mechanism did): readiness is "does orgs/<org>/<subpath> resolve inside
# autonomy-orgs", checked frame-correctly by the resolver, NOT a dir-shaped
# host-frame `exists` — so there is no `exists="dir"` metadata here.

MOUNT_SCHEMA_REVISION_2 = 2


class WorkspaceMountV2(BaseModel):
    """A file or directory inside the org-partitioned autonomy-orgs volume,
    bind-mounted into a workspace container.

    `subpath` is relative and org-free (e.g. ``personal/scale-harness/license.yaml``
    or ``vuln-diff-validation``); the launcher resolves it under
    ``orgs/<authenticated-org>/`` in autonomy-orgs. The shared|personal x
    org|workspace scope the old artifact layering encoded is now just a leading
    subpath convention, never a payload field.
    """

    model_config = ConfigDict(extra="forbid")

    subpath: str = Field(
        ..., description="Relative, org-free path under orgs/<org>/ in autonomy-orgs"
    )
    container_path: str = Field(..., description="Absolute path inside container")
    # What the declaration expects to find. REQUIRED and undefaulted: there is no
    # safe guess (defaulting 'dir' reproduces the old file bug; 'file' breaks
    # directory mounts). Replaces the presence+type double-duty of the removed
    # exists="dir": the resolver refuses a PRESENT-BUT-WRONG-TYPE target (a dir
    # where a file is expected binds a dir where the app opens a file — the
    # fabrication failure mode: succeeds by exit code, wrong by content).
    kind: Literal["file", "dir"] = Field(
        ..., description="Expected target type; the resolver refuses a mismatch"
    )
    mode: Literal["ro", "rw"] = "ro"
    # A SHORT title for a readiness tile — not a sentence. Capped so it can't
    # drift into being used as `description` is today (100-char sentences). The
    # UI puts `name` on top and `description` under it.
    name: str | None = Field(
        default=None, max_length=60,
        description="Short display label, e.g. 'Anchore Enterprise license'",
    )
    description: str | None = None
    # Long-form guidance shown when the mount is MISSING — the only part that
    # tells the operator what to DO (where to get the file). Carried forward from
    # autonomy.workspace.artifact#1's `help`, which the collapse would otherwise
    # silently drop.
    help: str | None = None
    required: bool = True

    @field_validator("subpath")
    @classmethod
    def subpath_relative_and_contained(cls, v: str) -> str:
        if not v:
            raise ValueError("subpath must not be empty")
        if v.startswith("/"):
            raise ValueError(f"subpath must be relative (no leading '/'): {v!r}")
        _reject_traversal_segments(v, label="subpath")
        # PurePosixPath.parts collapses '//' silently, so also require the raw
        # string to be already-normalized — rejects 'a//b', 'a/b/', 'a/./b'.
        if posixpath.normpath(v) != v:
            raise ValueError(
                f"subpath must be already-normalized (no '.', trailing or "
                f"doubled '/'): {v!r}"
            )
        return v

    @field_validator("container_path")
    @classmethod
    def container_absolute_and_normalized(cls, v: str) -> str:
        # An ARBITRARY absolute destination is deliberate — it is the feature that
        # distinguishes a mount from an artifact (fixed /etc/autonomy/artifacts/),
        # and a mount row applies only to its own org's workspaces, so the
        # destination carries no cross-org property (coordinator ruling,
        # 2026-08-19). This validator only requires CANONICAL spelling — absolute,
        # normalized, no `..` — so the stored path is exactly what gets bound and
        # traversal cannot be smuggled in via `.`/`..`/`//`; it does NOT restrict
        # which destination.
        if not v.startswith("/"):
            raise ValueError(f"container_path must be absolute: {v!r}")
        _reject_traversal_segments(v, label="container_path")
        if posixpath.normpath(v) != v:
            raise ValueError(
                f"container_path must be already-normalized (no '.', '..', "
                f"trailing or doubled '/'): {v!r}"
            )
        return v


@keyed_per_entity(
    key_strategy="workspace_id:mount_name",
    key_references={"workspace_id": "autonomy.workspace"},
)
@home("organization")
@readiness_gated_by("required")
class _WorkspaceMountV2SchemaAdapter(SettingSchema):
    """Registers ``autonomy.workspace.mount#2`` alongside #1. No upconverter is
    registered for the 1->2 hop (breaking change — see the note above)."""

    set_id = SET_ID
    schema_revision = MOUNT_SCHEMA_REVISION_2
    model = WorkspaceMountV2

    _field_metadata: dict[str, dict] = {
        "subpath": {
            "type": "string",
            "required": True,
            "description": (
                "Relative, org-free path under orgs/<org>/ in autonomy-orgs; "
                "resolved and refused-if-escaping at launch (may be a file or dir)"
            ),
        },
        "container_path": {
            "type": "string",
            "required": True,
            "description": "Absolute, normalized path inside the container to mount at",
        },
        "kind": {
            "type": "string",
            "required": True,
            "enum": ["file", "dir"],
            "description": "Expected target type; the resolver refuses a present-but-wrong-type mismatch",
        },
        "mode": {
            "type": "string",
            "description": "Mount mode — 'ro' for read-only, 'rw' for writable",
            "enum": ["ro", "rw"],
            "default": "ro",
        },
        "name": {
            "type": "string",
            "description": "Short display label (a title, not a sentence); max 60 chars",
        },
        "description": {
            "type": "string",
            "description": "Operator-facing description of what this mount provides",
        },
        "help": {
            "type": "string",
            "description": "Long-form guidance shown when the mount is missing (what to do)",
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
            raise SchemaValidationError(f"{cls.__name__}: {exc}") from exc
