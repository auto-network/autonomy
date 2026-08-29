"""``autonomy.workspace.mount#1`` — workspace host-directory mount Setting.

A **mount** declares that a host directory should be bind-mounted into a
workspace container at a specific container path. It mirrors the
Artifact Layering pattern (graph://bc0dda40-f56) but targets *directories*
at arbitrary container paths (not single files at
``/etc/autonomy/artifacts/<name>``).

**An org mount is presumed to be a remote share: no exclusive locking and
no SQLite, at any scope.** This is global, not a per-mount flag — scope
says who may SEE a mount, not where its bytes live, so a machine-scoped
folder can still sit on the NAS with several writers. SQLite's WAL mode
cannot work on a network share at all (WAL coordinates through a
shared-memory file network filesystems do not provide), and the pool is
exported ``vers=3, nolock, local_lock=all``, so the server performs no
lock coordination whatsoever. Operator decision, 2026-08-29
(graph://89535205-2b6).

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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
from .registry import SettingSchema, SchemaValidationError, keyed_per_entity, publication_band
from .registry import home, readiness_gated_by


@publication_band(min="raw", max="raw")
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
            "remediation": {
                "id": "workspace.declared-path.v1",
                "params": {},
            },
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


# ── Revision 2 — org-free subpath, WITH a deprecated host_path fallback ────────
#
# `subpath` (relative, org-free) is ADDED as the guarded path; `host_path` is kept
# as a DEPRECATED optional field so existing rev-1 rows keep working through the
# transition (b20f2468-b12). Exactly one is set per row. For a subpath row the
# resolver prepends `orgs/<authenticated-session-org>/` at launch, realpath-
# resolves, and refuses anything escaping that org's tree — so a subpath naming
# another org just resolves inside the caller's OWN org, and the guard stops any
# `..`/symlink escape. The resolved target may be a FILE or a directory (folds in
# what the retired artifact mechanism did).
#
# Because host_path is KEPT, a rev-1 payload is already a valid rev-2 payload, so
# the 1->2 upconverter is the IDENTITY (registered below) — NOT the impossible
# host_path->subpath transform. That is what keeps existing rows from dropping on
# a rev-2 read. A DEPRECATED FALLBACK FIELD KEEPS ITS ORIGINAL VALIDATION: legacy
# host_path/container_path stay rev-1 (absolute only, exact spelling incl. a
# trailing slash), so their argv is byte-identical; the stricter canonical
# container_path spelling applies to subpath rows only.

MOUNT_SCHEMA_REVISION_2 = 2


class WorkspaceMountV2(BaseModel):
    """A workspace mount: EITHER a `subpath` in the org-partitioned autonomy-orgs
    volume (the guarded path), OR a deprecated absolute `host_path` (the rev-1
    fallback the design kept "so existing rows keep working through the
    transition", b20f2468-b12). Exactly one is set.

    `subpath` is relative and org-free (e.g. ``personal/scale-harness/license.yaml``
    or ``vuln-diff-validation``); the launcher resolves it under
    ``orgs/<authenticated-org>/`` in autonomy-orgs, realpath-refusing an escape.
    The shared|personal x org|workspace scope the old artifact layering encoded is
    now just a leading subpath convention, never a payload field.

    `host_path` is the legacy absolute machine path. A rev-1 payload is a valid
    rev-2 payload by construction (host_path present, subpath/kind absent), so the
    1->2 upconverter is the IDENTITY and no row has to be rewritten to survive.
    The consumer dual-dispatches: host_path rows keep the old HOST-origin behavior,
    subpath rows get the guarded resolver. Migration host_path -> subpath is
    voluntary and per-row.

    `visibility` (machine|personal|organization) was ADDED to this revision
    rather than minting a rev-3, deliberately. The registry does not
    downconvert — ``upconvert_chain`` returns None when from_rev > to_rev — so
    a rev-3 row would DROP for every consumer pinned at rev 2, and
    ``workspace_settings.load_mounts`` is pinned at rev 2. anchore.db already
    carries rev-1 and rev-2 rows for the SAME keys plus deprecated canonical
    rows; a third revision lands on that mixed set for no gain, since an
    optional field with a default is a compatible addition in both directions:
    an old payload validates (default applies), and a new payload only reaches
    code that already knows the field.

    NOTE for anyone adding the NEXT field here: that reasoning holds for an
    OPTIONAL field with a safe default. A required field, a narrowed type, or
    a changed meaning is a real revision and must bump — the compatibility
    above comes from the default, not from the practice of editing in place.
    """

    model_config = ConfigDict(extra="forbid")

    host_path: str | None = Field(
        default=None,
        description="DEPRECATED absolute machine path (rev-1 fallback). Exactly one "
                    "of host_path / subpath is set; new rows use subpath.",
    )
    subpath: str | None = Field(
        default=None,
        description="Relative, org-free path under orgs/<org>/ in autonomy-orgs",
    )
    container_path: str = Field(..., description="Absolute path inside container")
    # What the declaration expects to find. REQUIRED and undefaulted: there is no
    # safe guess (defaulting 'dir' reproduces the old file bug; 'file' breaks
    # directory mounts). Replaces the presence+type double-duty of the removed
    # exists="dir": the resolver refuses a PRESENT-BUT-WRONG-TYPE target (a dir
    # where a file is expected binds a dir where the app opens a file — the
    # fabrication failure mode: succeeds by exit code, wrong by content).
    kind: Literal["file", "dir"] | None = Field(
        default=None,
        description="Expected target type; REQUIRED with subpath, forbidden with "
                    "host_path (legacy rows carry no kind). The resolver refuses a "
                    "present-but-wrong-type target.",
    )
    mode: Literal["ro", "rw"] = "ro"
    # ACCESS SCOPE — who may see this mount — and deliberately NOT a path
    # convention: the system has to act on this value, and a path segment
    # cannot be queried, validated or enforced. Distinct from the Setting
    # row's publication_state, which is the visibility of the DECLARATION,
    # not of the data (publishing a row does not serve a directory), and
    # which this set pins to `raw` anyway — a mount declaration never
    # leaves the database that owns it.
    #
    # DEFAULT IS THE NARROWEST SCOPE, and it is what every pre-existing row
    # means: a mount declared before this field existed is presumed visible
    # only on the machine holding it, until someone widens it deliberately.
    # Because read_set(model=WorkspaceMountV2) returns the VALIDATED model,
    # this default materializes on legacy rows automatically — consumers
    # reading through the model never need a fallback. Consumers reading raw
    # dicts (no model=) must use .get("visibility", "machine").
    #
    # There is no cross-org value: a mount visible to two organizations is a
    # contradiction in terms, not a scope (operator ruling, 2026-08-29).
    visibility: Literal["machine", "personal", "organization"] = Field(
        default="machine",
        description="Access scope: who may see this mount. Not a path "
                    "convention. Absent on legacy rows means 'machine'.",
    )
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
    def container_absolute(cls, v: str) -> str:
        # Absolute is required for BOTH row types (it is rev-1's ONLY constraint).
        # The stricter CANONICAL spelling — normalized, no `..`/`.`/trailing/doubled
        # `/` — is applied to SUBPATH rows only, in the model validator: a legacy
        # host_path row must keep its EXACT V1 container_path (a live row carries a
        # trailing slash, `/etc/autonomy/artifacts/scale-harness/`) so its deprecated
        # mount argv stays byte-identical to today. An arbitrary absolute
        # destination is deliberate either way (coordinator ruling, 2026-08-19).
        if not v.startswith("/"):
            raise ValueError(f"container_path must be absolute: {v!r}")
        return v

    @field_validator("host_path")
    @classmethod
    def host_path_absolute(cls, v):
        # The legacy fallback keeps rev-1's only constraint — absolute — and no
        # more (it is the un-migrated shape, resolved by the old HOST behavior).
        if v is not None and not v.startswith("/"):
            raise ValueError(f"host_path must be absolute: {v!r}")
        return v

    @model_validator(mode="after")
    def exactly_one_source_with_kind_rule(self):
        has_host = self.host_path is not None
        has_sub = self.subpath is not None
        if has_host == has_sub:
            raise ValueError(
                "exactly one of host_path (deprecated) or subpath must be set")
        if has_host:
            # Deprecated legacy row: kind forbidden, and NO canonical-spelling check
            # on container_path — rev-1 semantics preserved verbatim so a valid V1
            # row (incl. a trailing-slash container_path) stays valid and its argv
            # unchanged. This is what makes the 1->2 upconverter identity-COMPATIBLE
            # in effect, not just in name.
            if self.kind is not None:
                raise ValueError("host_path (legacy) must not carry kind")
        else:
            # Guarded subpath row: kind required, and container_path must be
            # CANONICAL (the point-4 destination guard) — new rows are held to the
            # strict spelling that legacy rows are grandfathered out of.
            if self.kind is None:
                raise ValueError("subpath requires kind (file|dir)")
            cp = self.container_path
            _reject_traversal_segments(cp, label="container_path")
            if posixpath.normpath(cp) != cp:
                raise ValueError(
                    f"container_path for a subpath mount must be already-normalized "
                    f"(no '.', '..', trailing or doubled '/'): {cp!r}")
        return self


@keyed_per_entity(
    key_strategy="workspace_id:mount_name",
    key_references={"workspace_id": "autonomy.workspace"},
)
@publication_band(min="raw", max="raw")
@home("organization")
@readiness_gated_by("required")
class _WorkspaceMountV2SchemaAdapter(SettingSchema):
    """Registers ``autonomy.workspace.mount#2`` alongside #1. An IDENTITY 1->2
    upconverter is registered (see the note above) so rev-1 rows survive a rev-2
    read as deprecated host_path rows."""

    set_id = SET_ID
    schema_revision = MOUNT_SCHEMA_REVISION_2
    model = WorkspaceMountV2

    _field_metadata: dict[str, dict] = {
        # host_path/subpath/kind are each OPTIONAL at the field level; the
        # exactly-one + kind-iff-subpath invariant is enforced by the model
        # validator, not by per-field `required` (which enforce_declared_fields
        # reads independently). A legacy host_path row must pass here.
        "host_path": {
            "type": "string",
            "description": "DEPRECATED absolute machine path (rev-1 fallback); exactly "
                           "one of host_path/subpath is set",
        },
        "subpath": {
            "type": "string",
            "remediation": {
                "id": "workspace.declared-path.v1",
                "params": {},
            },
            "description": (
                "Relative, org-free path under orgs/<org>/ in autonomy-orgs; "
                "resolved and refused-if-escaping at launch (may be a file or dir)"
            ),
        },
        "container_path": {
            "type": "string",
            "required": True,
            "description": "Absolute container path to mount at. For a subpath row it "
                           "must also be CANONICAL (normalized, no '..'/trailing slash); "
                           "a legacy host_path row keeps rev-1 semantics (absolute only, "
                           "exact spelling preserved).",
        },
        "kind": {
            "type": "string",
            "enum": ["file", "dir"],
            "description": "Expected target type (required with subpath); the resolver "
                           "refuses a present-but-wrong-type mismatch",
        },
        "mode": {
            "type": "string",
            "description": "Mount mode — 'ro' for read-only, 'rw' for writable",
            "enum": ["ro", "rw"],
            "default": "ro",
        },
        "visibility": {
            "type": "string",
            "enum": ["machine", "personal", "organization"],
            "default": "machine",
            "description": (
                "Access scope: who may see this mount. Records intent only — "
                "serving a mount across the fleet or an org is separate, later "
                "work. Legacy rows carry no value and mean 'machine' (the "
                "narrowest scope). Not a path convention: a path segment "
                "cannot be queried, validated or enforced."
            ),
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
    def readiness_findings(cls, *, key, payload, org, read):
        """Ask the launch resolver about VOLUME-origin mount readiness."""
        from agents.workspace_manager import check_org_mount_readiness

        return check_org_mount_readiness(key=key, payload=payload, org=org)

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


# The 1->2 upconverter is the IDENTITY. This is NOT the host_path -> subpath
# transform the design rejected as impossible (a machine path cannot become an
# org-relative subpath by pure function). Because rev 2 KEEPS host_path as a
# valid field, a rev-1 payload {host_path, container_path, mode, required, ...}
# is already a valid rev-2 payload unchanged — so the pure function that carries
# it forward is identity. Registering it is what keeps existing rev-1 rows from
# dropping when a consumer requests rev 2 (without it, `--as-rev 2` drops them
# all — the exact measurement that caught the first merge). Migration
# host_path -> subpath stays a voluntary, per-row rewrite; nothing is rewritten
# merely to survive the revision bump.
from .registry import register_upconverter as _register_upconverter

_register_upconverter(
    SET_ID, SCHEMA_REVISION, MOUNT_SCHEMA_REVISION_2, lambda payload: dict(payload)
)
