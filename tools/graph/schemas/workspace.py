"""``autonomy.workspace#1`` — workspace declaration Setting.

A workspace is a containerized project environment. Each workspace lives
as a Setting in its owning org's DB, keyed by the workspace id (e.g.
``enterprise-ng``). Consumers — the dispatcher, session launcher, and
workspace manager — read these Settings to decide how to build and launch
the container.

Spec: graph://0d3f750f-f9c (Setting Primitive), graph://e2c81892-0fb
(Workspace Container Lifecycle), graph://eabec73c-baa (Workspaces & Orgs).

Notes on shape:

* No ``org`` field — the owning org is implicit from which DB the Setting
  lives in. Writing an ``org`` field would also trip the cross-DB
  reference scanner in ``org_ops.find_references``.
* No ``graph_project`` field — renamed to ``graph_org`` and then dropped
  entirely in auto-0wj9 (the setting *is* the org).
* No ``artifacts`` field — workspace artifacts are a separate Setting
  shape (``autonomy.workspace.artifact#1``) migrated by auto-S3.
"""

from __future__ import annotations

import re
from typing import Any

from .registry import (
    SettingSchema,
    SchemaValidationError,
    field,
    keyed_per_entity,
)


WORKSPACE_SET_ID = "autonomy.workspace"
WORKSPACE_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Workspace declarations: container image, harness, repos, "
        "mounts, env, dispatch labels"
    ),
    "nouns": [
        "workspace", "session container", "rename workspace",
        "image", "harness", "dispatch labels", "repos",
    ],
    "related_set_ids": [
        "autonomy.workspace.artifact#1",
        "autonomy.workspace.mount#1",
        "autonomy.workspace.capability.enable#1",
    ],
}


# ── Repo mount entry ────────────────────────────────────────

_VALID_HARNESSES = {"claude", "codex"}


class WorkspaceRepoV1(SettingSchema):
    """One repository mounted into a workspace.

    A repository is identified one of two ways, and exactly one: by ``host``
    and ``repo`` on a git host, or by ``local_path`` for a local-first
    repository that has no remote at all. The clone URL is composed from
    whichever form is present.

    The host is stored rather than parsed back out of a clone URL, because
    it is what a credential is keyed by: stating it makes the reference a
    plain value and the check a plain lookup. A local-first repository
    needs no credential, and says so by having no host rather than by
    omitting a field.

    Declared rather than checked imperatively, so the entry's shape is
    metadata: enforcement reads it, and so does the reference check that
    reports an unprovisioned credential. A shape that lives only in a
    validate() body is invisible to both.
    """

    internal = True

    host: str = field(
        required=False,
        description=(
            "Git host this repository is on, e.g. github.com or an ssh "
            "config alias. Credentials are per host, so this is what "
            "selects one"
        ),
        references="autonomy.secure.setting",
        reference_scope="org",
    )
    repo: str = field(
        required=False,
        description="Owner and name on that host, e.g. anchore/anchorectl",
    )
    local_path: str = field(
        required=False,
        description=(
            "Absolute host path of a local-first repository that has no "
            "remote. Mutually exclusive with host and repo"
        ),
    )
    mount: str = field(
        required=True,
        description="Absolute path the repository is mounted at in the workspace",
    )
    writable: bool = field(
        required=False,
        description="Whether the agent may commit to it",
    )
    base_source: str = field(
        required=False,
        description="Absolute host path to clone from instead of the remote",
    )


def _validate_repo(repo: Any, idx: int) -> None:
    """The rules no declaration expresses: absolute paths, and one form.

    Field names, types and unknown-field rejection are declared on
    :class:`WorkspaceRepoV1` and enforced from its metadata. What is left
    is a cross-field rule -- a repository is on a git host or it is local,
    never both and never neither -- and the requirement that host paths be
    absolute.
    """
    if not isinstance(repo, dict):
        return
    remote = bool(repo.get("host")) or bool(repo.get("repo"))
    local = bool(repo.get("local_path"))
    if remote and local:
        raise SchemaValidationError(
            f"repos[{idx}] sets both a git host and a local path; "
            f"a repository is one or the other"
        )
    if remote and not (repo.get("host") and repo.get("repo")):
        raise SchemaValidationError(
            f"repos[{idx}] needs both 'host' and 'repo' to name a "
            f"repository on a git host"
        )
    if not remote and not local:
        raise SchemaValidationError(
            f"repos[{idx}] names no repository: give 'host' and 'repo', "
            f"or 'local_path'"
        )
    if local and not str(repo["local_path"]).startswith("/"):
        raise SchemaValidationError(
            f"repos[{idx}].local_path must be an absolute path, "
            f"got {repo['local_path']!r}"
        )
    bs = repo.get("base_source")
    if bs is not None and not str(bs).startswith("/"):
        raise SchemaValidationError(
            f"repos[{idx}].base_source must be an absolute path, got {bs!r}"
        )


# ── WorkspaceV1 ─────────────────────────────────────────────


@keyed_per_entity(key_strategy="workspace_id")
class WorkspaceV1(SettingSchema):
    """Shape of an ``autonomy.workspace#1`` Setting payload.

    Required: ``name``, ``image``.
    All other fields optional with defaults applied at the consumer layer.
    """

    set_id = WORKSPACE_SET_ID
    schema_revision = WORKSPACE_REVISION

    _required = ("name", "image")
    _optional_types: dict[str, type | tuple[type, ...]] = {
        "description": str,
        "harness": str,
        "model": str,
        "working_dir": str,
        "startup": str,
        "dind": bool,
        "needs_nested_docker": bool,
        "session_runtime": str,
        "network_host": bool,
        "repos": list,
        "host_root_mount": dict,
        "env": dict,
        "env_from_host": list,
        "tags": list,
        "dispatch_labels": list,
    }

    _field_metadata: dict[str, dict] = {
        "name": {
            "type": "string",
            "required": True,
            "description": "Workspace identifier (matches the Setting key)",
        },
        "image": {
            "type": "string",
            "required": True,
            "description": "Container image to launch (e.g. autonomy-base:latest)",
        },
        "description": {
            "type": "string",
            "description": "Human-readable description of the workspace",
        },
        "harness": {
            "type": "string",
            "description": "Agent CLI to launch inside the container",
            "enum": sorted(_VALID_HARNESSES),
            "default": "claude",
        },
        "model": {
            "type": "string",
            "description": (
                "Harness model id (e.g. 'claude-opus-4-8[1m]'). When unset, "
                "the launcher falls back to its hardcoded default."
            ),
        },
        "working_dir": {
            "type": "string",
            "description": "Working directory inside the container at agent start",
        },
        "startup": {
            "type": "string",
            "description": "Shell command(s) to run before launching the harness",
        },
        "dind": {
            "type": "boolean",
            "description": (
                "Deprecated alias for needs_nested_docker. Runs a nested "
                "daemon; it never mounts the host Docker socket."
            ),
            "default": False,
            "deprecated_alias_of": "needs_nested_docker",
        },
        "needs_nested_docker": {
            "type": "boolean",
            "description": (
                "Preserve the image entrypoint that starts its own nested "
                "Docker daemon. Independent of session_runtime."
            ),
            "default": False,
        },
        "session_runtime": {
            "type": "string",
            "description": (
                "Docker isolation selector: standard, privileged, sysbox, "
                "or an installed OCI runtime name. Nested-Docker workspaces "
                "default to privileged when this is omitted."
            ),
        },
        "network_host": {
            "type": "boolean",
            "description": "Use --network=host so the container can reach localhost services",
            "default": False,
        },
        "repos": {
            "type": "array",
            "description": "Repos to mount as worktrees inside the container",
            "element": WorkspaceRepoV1,
        },
        "host_root_mount": {
            "type": "object",
            "description": (
                "Explicit opt-in to mount the LIVE host platform checkout "
                "(including data/ — org DBs, keys, secrets) read-only at "
                "/workspace/repo instead of the default git snapshot. "
                "Requires a stated reason; never inferred from workspace "
                "name or org. Almost no workspace should set this."
            ),
            "element": {
                "reason": {"type": "string", "required": True,
                           "description": "Why this workspace needs live host state"},
            },
        },
        "env": {
            "type": "object",
            "description": "Environment variables (string -> string) passed into the container",
        },
        "env_from_host": {
            "type": "array",
            "description": "Names of host env vars to forward into the container",
            "element": {"type": "string"},
        },
        "tags": {
            "type": "array",
            "description": "Free-form tags (e.g. dashboard filters)",
            "element": {"type": "string"},
        },
        "dispatch_labels": {
            "type": "array",
            "description": "Dispatch routing labels — beads matching any label dispatch here",
            "element": {"type": "string"},
        },
    }

    @classmethod
    def validate(cls, payload: Any) -> None:  # noqa: C901 — flat checks
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                f"{cls.__name__}: payload must be a dict, "
                f"got {type(payload).__name__}"
            )
        for key in cls._required:
            if key not in payload:
                raise SchemaValidationError(
                    f"{cls.__name__}: missing required field {key!r}"
                )
            val = payload[key]
            if not isinstance(val, str) or not val:
                raise SchemaValidationError(
                    f"{cls.__name__}: {key!r} must be a non-empty string"
                )
        allowed = set(cls._required) | set(cls._optional_types)
        extra = set(payload) - allowed
        if extra:
            raise SchemaValidationError(
                f"{cls.__name__}: unknown field(s): {sorted(extra)}"
            )
        for key, want in cls._optional_types.items():
            if key not in payload:
                continue
            val = payload[key]
            if not isinstance(val, want):
                raise SchemaValidationError(
                    f"{cls.__name__}: {key!r} must be "
                    f"{want.__name__ if isinstance(want, type) else want}, "
                    f"got {type(val).__name__}"
                )
        # List element types.
        for key in ("env_from_host", "tags", "dispatch_labels"):
            if key in payload:
                for i, v in enumerate(payload[key]):
                    if not isinstance(v, str):
                        raise SchemaValidationError(
                            f"{cls.__name__}: {key}[{i}] must be str"
                        )
        if "env" in payload:
            for k, v in payload["env"].items():
                if not isinstance(k, str) or not isinstance(v, str):
                    raise SchemaValidationError(
                        f"{cls.__name__}: env must map str -> str; "
                        f"bad entry {k!r}={v!r}"
                    )
        if "repos" in payload:
            for i, repo in enumerate(payload["repos"]):
                _validate_repo(repo, i)
        if "host_root_mount" in payload:
            hrm = payload["host_root_mount"]
            reason = hrm.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise SchemaValidationError(
                    f"{cls.__name__}: host_root_mount requires a non-empty "
                    "'reason' string stating why live host state is needed"
                )
            extra_hrm = set(hrm) - {"reason"}
            if extra_hrm:
                raise SchemaValidationError(
                    f"{cls.__name__}: host_root_mount has unknown field(s): "
                    f"{sorted(extra_hrm)}"
                )
        if "harness" in payload:
            harness = payload["harness"]
            if harness not in _VALID_HARNESSES:
                raise SchemaValidationError(
                    f"{cls.__name__}: 'harness' must be one of "
                    f"{sorted(_VALID_HARNESSES)}, got {harness!r}"
                )
        if "dind" in payload and "needs_nested_docker" in payload:
            if payload["dind"] != payload["needs_nested_docker"]:
                raise SchemaValidationError(
                    f"{cls.__name__}: 'dind' and 'needs_nested_docker' conflict"
                )
        if "session_runtime" in payload:
            runtime = payload["session_runtime"]
            if not runtime or re.fullmatch(r"[A-Za-z0-9_.-]+", runtime) is None:
                raise SchemaValidationError(
                    f"{cls.__name__}: 'session_runtime' must be a non-empty "
                    "OCI runtime selector"
                )
