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

from typing import Any

from .registry import SettingSchema, SchemaValidationError


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


# ── Valid repo mount shape ──────────────────────────────────

_REPO_REQUIRED = ("url", "mount")
_REPO_OPTIONAL = {"writable": bool, "base_source": str}
_VALID_HARNESSES = {"claude", "codex"}


def _validate_repo(repo: Any, idx: int) -> None:
    if not isinstance(repo, dict):
        raise SchemaValidationError(
            f"repos[{idx}] must be a mapping, got {type(repo).__name__}"
        )
    for key in _REPO_REQUIRED:
        if key not in repo:
            raise SchemaValidationError(
                f"repos[{idx}] missing required field {key!r}"
            )
        if not isinstance(repo[key], str) or not repo[key]:
            raise SchemaValidationError(
                f"repos[{idx}].{key} must be a non-empty string"
            )
    for key, want in _REPO_OPTIONAL.items():
        if key in repo and not isinstance(repo[key], want):
            raise SchemaValidationError(
                f"repos[{idx}].{key} must be {want.__name__}"
            )
    if "base_source" in repo:
        bs = repo["base_source"]
        if not bs:
            raise SchemaValidationError(
                f"repos[{idx}].base_source must be a non-empty string"
            )
        if not bs.startswith("/"):
            raise SchemaValidationError(
                f"repos[{idx}].base_source must be an absolute path, "
                f"got {bs!r}"
            )
    allowed = set(_REPO_REQUIRED) | set(_REPO_OPTIONAL)
    extra = set(repo) - allowed
    if extra:
        raise SchemaValidationError(
            f"repos[{idx}] has unknown field(s): {sorted(extra)}"
        )


# ── WorkspaceV1 ─────────────────────────────────────────────


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
        "network_host": bool,
        "repos": list,
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
            "description": "Mount host docker socket (Docker-in-Docker)",
            "default": False,
        },
        "network_host": {
            "type": "boolean",
            "description": "Use --network=host so the container can reach localhost services",
            "default": False,
        },
        "repos": {
            "type": "array",
            "description": "Repos to mount as worktrees inside the container",
            "element": {
                "url": {"type": "string", "required": True,
                        "description": "Git repository URL or path"},
                "mount": {"type": "string", "required": True,
                          "description": "Container path to mount the worktree at"},
                "writable": {"type": "boolean", "default": False,
                             "description": "Whether the worktree mount is writable"},
                "base_source": {
                    "type": "string",
                    "description": (
                        "Absolute host checkout path used as the base for "
                        "fresh session worktrees instead of `origin`. "
                        "Omit to derive fresh worktrees from `origin`."
                    ),
                },
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
        if "harness" in payload:
            harness = payload["harness"]
            if harness not in _VALID_HARNESSES:
                raise SchemaValidationError(
                    f"{cls.__name__}: 'harness' must be one of "
                    f"{sorted(_VALID_HARNESSES)}, got {harness!r}"
                )
