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
    publication_band,
    RemediationRef,
    SettingSchema,
    SchemaValidationError,
    field,
    home,
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


#: Components of a derived local-repository path (org slug, workspace id).
#: Must mirror ``_LOCAL_WORKSPACE_COMPONENT_RE`` in
#: ``agents.workspace_manager`` — the launch-side store refuses anything
#: else (this layer cannot import agents; a test asserts the two patterns
#: stay identical).
LOCAL_REPO_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class WorkspaceRepoV1(SettingSchema):
    """One repository mounted into a workspace.

    A repository is identified one of three ways, and exactly one: by
    ``host`` and ``repo`` on a git host; by ``local: true`` for THE
    workspace's dashboard-managed local repository (no remote — location
    derived on the consuming node, never stored); or by the DEPRECATED
    absolute ``local_path``. The clone URL is composed from whichever form
    is present.

    The host is stored rather than parsed back out of a clone URL because it
    is part of the remote's address and may be an SSH config alias.  It is
    not a reference to ``autonomy.secure.setting``: repository preparation
    uses the dashboard host's SSH configuration/agent, and pretending it
    consumes an unrelated sealed connector secret makes a viable repository
    fail readiness without changing what launch does.

    Declared rather than checked imperatively, so the entry's shape is
    metadata. A shape that lives only in a validate() body is invisible to
    generic tooling.
    """

    internal = True

    host: str = field(
        required=False,
        description=(
            "Git host this repository is on, e.g. github.com or an ssh "
            "config alias. Credentials are per host, so this is what "
            "selects one"
        ),
    )
    repo: str = field(
        required=False,
        description="Owner and name on that host, e.g. anchore/anchorectl",
    )
    user: str = field(
        required=False,
        description=(
            "Login user on that host. Defaults to git, which is right for "
            "every hosted forge; a private server may use another"
        ),
    )
    local: bool = field(
        required=False,
        description=(
            "True marks this entry as the workspace's dashboard-managed "
            "local repository (no remote). It stores no location: the "
            "consuming node derives data/workspace-repos/<owning-org>/"
            "<workspace-id> and creates the bare repository at first "
            "launch when missing. The worktree/commit/merge machinery "
            "applies exactly as to a remote repo. Exactly one of "
            "host+repo, local, or the deprecated local_path names a "
            "repository."
        ),
    )
    local_path: str = field(
        required=False,
        exists="dir",
        exists_frame="platform-host",
        remediation=RemediationRef("workspace.declared-path.v1"),
        description=(
            "DEPRECATED absolute host path of a local-first repository — "
            "an org row must not carry a machine path (true on one "
            "computer, false on the next); declare 'local: true' instead. "
            "Kept only until existing rows are rewritten, then deleted. "
            "On the machine where the path is real, semantics are "
            "unchanged: a missing path is created at first launch."
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
        exists="dir",
        exists_frame="platform-host",
        severity="advisory",
        remediation=RemediationRef("workspace.declared-path.v1"),
        description=(
            "DEPRECATED absolute host path to clone from instead of the "
            "remote. Advisory-only (absent means track the remote) and "
            "machine-local by nature; slated for deletion once unused."
        ),
    )


def _validate_repo(repo: Any, idx: int) -> None:
    """The rules no declaration expresses: absolute paths, and one form.

    Field names, types and unknown-field rejection are declared on
    :class:`WorkspaceRepoV1` and enforced from its metadata. What is left
    is a cross-field rule -- a repository is on a git host, the managed
    local one, or a deprecated pathful local one; exactly one form -- and
    the requirement that host paths be absolute.

    ``local: true`` is a compatible in-place addition (optional field,
    safe default) per the rule documented on the mount schema's rev-2
    ``visibility`` field: an old payload validates unchanged, and a new
    payload only reaches code that knows the field. Deleting the
    deprecated fields later is the real revision-worthy change.
    """
    if not isinstance(repo, dict):
        return
    remote = bool(repo.get("host")) or bool(repo.get("repo"))
    pathful = bool(repo.get("local_path"))
    managed = repo.get("local") is True
    if remote + pathful + managed > 1:
        raise SchemaValidationError(
            f"repos[{idx}] mixes repository forms; give 'host'+'repo', "
            f"'local: true', or the deprecated 'local_path' — exactly one"
        )
    if remote and not (repo.get("host") and repo.get("repo")):
        raise SchemaValidationError(
            f"repos[{idx}] needs both 'host' and 'repo' to name a "
            f"repository on a git host"
        )
    if not (remote or pathful or managed):
        raise SchemaValidationError(
            f"repos[{idx}] names no repository: give 'host' and 'repo', "
            f"'local: true', or the deprecated 'local_path'"
        )
    if pathful and not str(repo["local_path"]).startswith("/"):
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


@publication_band(min="raw", max="raw")
@keyed_per_entity(key_strategy="workspace_id")
#: A workspace is one value correct for every member of the org that owns it —
#: its image, harness, repos, tags and container paths do not differ per
#: reader or per machine. Undeclared until now, which asserted nothing: the
#: rubric (graph://4d88c2ad-625) says a schema that predates the decorator is
#: outstanding rather than silently defaulted, and this one was never worked.
#:
#: Declaring it does NOT make the row safe to hold secrets. `env` currently
#: carries the operator's own credentials in several orgs (auto-ehyoh), and
#: those are `personal` by the one-line rule — "if it is MINE on every machine
#: I own, my identity, MY CREDENTIALS, my preferences". They leave for the
#: vault rather than being re-homed with the row; the row itself is genuinely
#: the organization's and stays here.
@home("organization")
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
            "names_host_env": True,
            # Launch starts with ``env`` and then overlays host values that
            # exist. A fixed value therefore satisfies the requirement even
            # when the optional host override is absent.
            "env_fallback_field": "env",
            "remediation": {
                "id": "workspace.env-from-host.legacy.v1",
                "params": {},
            },
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
