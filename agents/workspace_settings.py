"""Workspace config composed from ``autonomy.workspace#1`` Settings.

Replaces the yaml-reading ``project_config`` module. Callers see typed
dataclasses (:class:`WorkspaceV1`, :class:`RepoMount`, :class:`ArtifactSpec`,
:class:`OrgOverride`) composed from Settings the dispatcher / session
launcher / dashboard need; the actual Setting read goes through
``tools.graph.ops.read_set`` / ``get_setting`` so per-org routing stays in
the ops layer.

Composition rules:

* ``autonomy.workspace#1`` → one WorkspaceV1 per Setting. The owning org
  (``graph_project``) is the org-slug of the DB the Setting lives in —
  implicit in the Setting primitive; surfaced by iterating orgs via
  :func:`tools.graph.org_ops.list_orgs`.
* ``autonomy.workspace.artifact#1`` → :class:`ArtifactSpec` tuple attached
  to the matching workspace. Composite Setting key
  ``<workspace-id>:<artifact-name>`` tells us the binding.
* ``autonomy.org#1`` → :class:`OrgOverride` for the identity cascade in
  ``tools.dashboard.org_identity``.

Artifact host paths come from the default layering rule
(``data/artifacts/{shared|personal}/{org}[/{workspace}]/{name}``); a
future ``autonomy.artifact-path#1`` Personal Setting can override per host
(lookup is best-effort — absence of that schema is not an error).

Spec refs: graph://0d3f750f-f9c (Setting primitive), graph://bcce359d-a1d
(cross-org search architecture), graph://bc0dda40-f56 (artifact layering).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.graph import ops, org_ops
from tools.graph.schemas.registry import get_schema
from tools.graph.schemas.workspace import WORKSPACE_SET_ID, WORKSPACE_REVISION
from tools.graph.schemas.workspace_artifact import (
    SET_ID as ARTIFACT_SET_ID,
    SCHEMA_REVISION as ARTIFACT_REVISION,
    VALID_SCOPES as VALID_ARTIFACT_SCOPES,
)
from tools.graph.schemas.org import ORG_SET_ID, ORG_REVISION

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARTIFACTS_ROOT = REPO_ROOT / "data" / "artifacts"
ARTIFACTS_MOUNT_DIR = "/etc/autonomy/artifacts"

ARTIFACT_PATH_SET_ID = "autonomy.artifact-path"
ARTIFACT_PATH_REVISION = 1

logger = logging.getLogger(__name__)


class WorkspaceSettingsError(ValueError):
    """Raised when Setting-derived workspace data is missing or malformed."""


# ── Typed composition models ────────────────────────────────


@dataclass(frozen=True)
class RepoMount:
    """Git repo mount spec from the workspace Setting payload."""
    url: str
    mount: str
    writable: bool = False


@dataclass(frozen=True)
class ArtifactSpec:
    """A file the workspace expects inside its container.

    Constructed from an ``autonomy.workspace.artifact#1`` Setting. ``name``
    comes from the composite key; ``scope`` / ``required`` / ``description``
    / ``help`` come from the payload. Resolved to a host path by the
    artifact layering rule.
    """
    name: str
    scope: str
    required: bool = False
    description: str = ""
    help: str = ""


@dataclass(frozen=True)
class MissingArtifact:
    """A required artifact whose resolved host path does not exist on disk."""
    artifact: ArtifactSpec
    path: Path
    project_id: str


@dataclass(frozen=True)
class OrgOverride:
    """Operator-local override of an org's published canonical identity.

    Per-field — any field left ``None`` falls through to the canonical
    identity and then to a deterministic generated fallback. See
    ``graph://497cdc20-d43``.
    """
    slug: str
    name: str | None = None
    byline: str | None = None
    color: str | None = None
    favicon: str | None = None


@dataclass(frozen=True)
class WorkspaceV1:
    """Composed workspace config — one ``autonomy.workspace#1`` Setting
    plus its attached artifact Settings plus the owning org slug.

    Field names match the legacy ``ProjectConfig`` surface so existing
    consumers (dispatcher, session launcher, primer renderer, dashboard)
    read the same attributes.
    """
    id: str
    name: str
    description: str
    image: str
    graph_project: str
    repos: tuple[RepoMount, ...] = ()
    working_dir: str | None = None
    startup: str | None = None
    dind: bool = False
    network_host: bool = True
    default_tags: tuple[str, ...] = ()
    dispatch_labels: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    env_from_host: tuple[str, ...] = ()
    artifacts: tuple[ArtifactSpec, ...] = ()


# ── Setting payload → typed model helpers ──────────────────


def _parse_repo(raw: Any, workspace_id: str, idx: int) -> RepoMount:
    if not isinstance(raw, dict):
        raise WorkspaceSettingsError(
            f"workspace {workspace_id!r}: repos[{idx}] must be a mapping"
        )
    for key in ("url", "mount"):
        if key not in raw:
            raise WorkspaceSettingsError(
                f"workspace {workspace_id!r}: repos[{idx}] missing {key!r}"
            )
    return RepoMount(
        url=str(raw["url"]),
        mount=str(raw["mount"]),
        writable=bool(raw.get("writable", False)),
    )


def _workspace_from_setting(
    setting_payload: dict,
    workspace_id: str,
    graph_project: str,
    artifacts: tuple[ArtifactSpec, ...],
) -> WorkspaceV1:
    """Compose a :class:`WorkspaceV1` from a resolved Setting payload.

    ``graph_project`` is determined by which org DB carried the Setting
    (see :func:`load_workspaces`), since the ``autonomy.workspace#1`` schema
    does not replicate that field in the payload.
    """
    image = setting_payload.get("image")
    if not isinstance(image, str) or not image:
        raise WorkspaceSettingsError(
            f"workspace {workspace_id!r}: payload missing 'image'"
        )
    repos_raw = setting_payload.get("repos") or []
    repos = tuple(
        _parse_repo(r, workspace_id, i) for i, r in enumerate(repos_raw)
    )
    env_raw = setting_payload.get("env") or {}
    env = {str(k): str(v) for k, v in env_raw.items()}
    return WorkspaceV1(
        id=workspace_id,
        name=str(setting_payload.get("name") or workspace_id),
        description=str(setting_payload.get("description") or ""),
        image=image,
        graph_project=graph_project,
        repos=repos,
        working_dir=(setting_payload.get("working_dir") or None),
        startup=(setting_payload.get("startup") or None),
        dind=bool(setting_payload.get("dind", False)),
        network_host=bool(setting_payload.get("network_host", True)),
        default_tags=tuple(str(t) for t in (setting_payload.get("tags") or ())),
        dispatch_labels=tuple(
            str(l) for l in (setting_payload.get("dispatch_labels") or ())
        ),
        env=env,
        env_from_host=tuple(
            str(v) for v in (setting_payload.get("env_from_host") or ())
        ),
        artifacts=artifacts,
    )


def _artifact_from_setting(
    key: str, payload: dict, workspace_id: str,
) -> ArtifactSpec:
    """Construct an :class:`ArtifactSpec` from a
    ``<workspace-id>:<artifact-name>`` keyed Setting.
    """
    prefix = f"{workspace_id}:"
    if not key.startswith(prefix):
        raise WorkspaceSettingsError(
            f"artifact Setting key {key!r} does not start with "
            f"expected prefix {prefix!r}"
        )
    name = key[len(prefix):]
    scope = payload.get("scope")
    if scope not in VALID_ARTIFACT_SCOPES:
        raise WorkspaceSettingsError(
            f"workspace {workspace_id!r}: artifact {name!r} has "
            f"invalid scope {scope!r}"
        )
    return ArtifactSpec(
        name=name,
        scope=str(scope),
        required=bool(payload.get("required", False)),
        description=str(payload.get("description") or ""),
        help=str(payload.get("help") or ""),
    )


# ── Public read path ───────────────────────────────────────


def _artifacts_for_workspace(
    workspace_id: str, *, caller_org: str | None,
) -> tuple[ArtifactSpec, ...]:
    """Read the ``autonomy.workspace.artifact#1`` Set and filter by
    composite-key prefix ``<workspace-id>:``.
    """
    members = ops.read_set(
        ARTIFACT_SET_ID, caller_org=caller_org,
    ).members
    prefix = f"{workspace_id}:"
    out: list[ArtifactSpec] = []
    for m in members:
        if not m.key.startswith(prefix):
            continue
        out.append(_artifact_from_setting(m.key, m.payload, workspace_id))
    out.sort(key=lambda a: a.name)
    return tuple(out)


def _workspaces_in_org(slug: str) -> dict[str, WorkspaceV1]:
    """Read every ``autonomy.workspace#1`` visible to *slug* + attach artifacts."""
    members = ops.read_set(WORKSPACE_SET_ID, caller_org=slug).members
    out: dict[str, WorkspaceV1] = {}
    for m in members:
        artifacts = _artifacts_for_workspace(m.key, caller_org=slug)
        out[m.key] = _workspace_from_setting(
            m.payload, workspace_id=m.key, graph_project=slug,
            artifacts=artifacts,
        )
    return out


def load_workspaces() -> dict[str, WorkspaceV1]:
    """Return every visible workspace, keyed by workspace id.

    Iterates per-org DBs via :func:`tools.graph.org_ops.list_orgs` and reads
    ``autonomy.workspace#1`` from each, attaching its artifact Settings. In
    the single-DB fallback (no ``data/orgs/*.db`` yet), reads from the
    default DB and uses the caller's default org slug for every workspace.
    Ops owns DB routing; consumers do not enumerate peers themselves.
    """
    refs = org_ops.list_orgs()
    if not refs:
        # Pre-migration fallback: no per-org DBs exist. All Settings live in
        # the default DB; graph_project is unknown from Setting data alone.
        members = ops.read_set(WORKSPACE_SET_ID).members
        out: dict[str, WorkspaceV1] = {}
        for m in members:
            artifacts = _artifacts_for_workspace(m.key, caller_org=None)
            graph_project = m.org or ""
            out[m.key] = _workspace_from_setting(
                m.payload, workspace_id=m.key, graph_project=graph_project,
                artifacts=artifacts,
            )
        return out
    out = {}
    for ref in refs:
        for wid, workspace in _workspaces_in_org(ref.slug).items():
            # First org registration wins on id collision (registry order
            # mirrors :func:`list_orgs` alphabetic sort).
            out.setdefault(wid, workspace)
    return out


def get_workspace(workspace_id: str) -> WorkspaceV1:
    """Look up a single workspace by id. Raises :class:`KeyError` if unknown."""
    workspaces = load_workspaces()
    if workspace_id not in workspaces:
        raise KeyError(f"unknown workspace: {workspace_id!r}")
    return workspaces[workspace_id]


def load_org_overrides() -> dict[str, OrgOverride]:
    """Return per-slug :class:`OrgOverride` built from ``autonomy.org#1``
    Settings across all org DBs.

    Each org's identity Setting lives in its own DB, keyed by the org slug.
    Fields not present in the Setting payload remain ``None`` so the
    :mod:`tools.dashboard.org_identity` cascade can fall through per-field.
    """
    refs = org_ops.list_orgs()
    out: dict[str, OrgOverride] = {}
    if not refs:
        members = ops.read_set(ORG_SET_ID).members
        for m in members:
            out[m.key] = _org_override_from_payload(m.key, m.payload)
        return out
    for ref in refs:
        members = ops.read_set(ORG_SET_ID, caller_org=ref.slug).members
        for m in members:
            out.setdefault(m.key, _org_override_from_payload(m.key, m.payload))
    return out


def _org_override_from_payload(slug: str, payload: dict) -> OrgOverride:
    def _opt(key: str) -> str | None:
        value = payload.get(key)
        if value is None:
            return None
        s = str(value)
        return s if s else None

    return OrgOverride(
        slug=slug,
        name=_opt("name"),
        byline=_opt("byline"),
        color=_opt("color"),
        favicon=_opt("favicon"),
    )


# ── Artifact resolution ────────────────────────────────────


def _artifact_path_override(
    workspace: WorkspaceV1, artifact: ArtifactSpec,
) -> Path | None:
    """Look up an ``autonomy.artifact-path#1`` Personal Setting override.

    The override Setting is keyed by ``<workspace-id>:<artifact-name>`` and
    carries a ``path`` payload field. Absent schema (auto-S1 did not ship
    this one yet) → no override; absent Setting → no override. The schema
    is optional today; a best-effort lookup keeps the consumer correct
    either way.
    """
    if get_schema(ARTIFACT_PATH_SET_ID, ARTIFACT_PATH_REVISION) is None:
        return None
    key = f"{workspace.id}:{artifact.name}"
    try:
        members = ops.read_set(
            ARTIFACT_PATH_SET_ID, caller_org="personal",
        ).members
    except Exception:
        # Personal DB absent is fine — fall through to the default rule.
        return None
    for m in members:
        if m.key == key:
            path = m.payload.get("path")
            if isinstance(path, str) and path:
                return Path(path)
    return None


def artifact_host_path(
    artifact: ArtifactSpec,
    workspace: WorkspaceV1,
    *,
    artifacts_root: Path | str = DEFAULT_ARTIFACTS_ROOT,
) -> Path:
    """Resolve *artifact* to its host filesystem path.

    Checks for an ``autonomy.artifact-path#1`` override (Personal Settings)
    first, then falls back to the default layering rule::

        {root}/{shared|personal}/{org}[/{workspace}]/{name}
    """
    override = _artifact_path_override(workspace, artifact)
    if override is not None:
        return override
    root = Path(artifacts_root)
    share = "shared" if artifact.scope.startswith("shared-") else "personal"
    base = root / share / workspace.graph_project
    if artifact.scope.endswith("-workspace"):
        base = base / workspace.id
    return base / artifact.name


def validate_artifacts(
    workspace: WorkspaceV1,
    *,
    artifacts_root: Path | str = DEFAULT_ARTIFACTS_ROOT,
) -> list[MissingArtifact]:
    """Return every required artifact whose resolved host path does not exist."""
    missing: list[MissingArtifact] = []
    for art in workspace.artifacts:
        if not art.required:
            continue
        path = artifact_host_path(art, workspace, artifacts_root=artifacts_root)
        if not path.exists():
            missing.append(
                MissingArtifact(
                    artifact=art, path=path, project_id=workspace.id,
                )
            )
    return missing


def artifact_mounts(
    workspace: WorkspaceV1,
    *,
    artifacts_root: Path | str = DEFAULT_ARTIFACTS_ROOT,
) -> dict[str, str]:
    """Return ``{host_path: container_spec}`` for every artifact that exists.

    Each artifact resolves to a read-only bind mount at
    ``/etc/autonomy/artifacts/{name}``. Missing optional artifacts are
    silently skipped — call :func:`validate_artifacts` first to enforce
    required ones.
    """
    mounts: dict[str, str] = {}
    for art in workspace.artifacts:
        host = artifact_host_path(art, workspace, artifacts_root=artifacts_root)
        if host.exists():
            mounts[str(host)] = f"{ARTIFACTS_MOUNT_DIR}/{art.name}:ro"
    return mounts


def format_missing_artifact_error(
    missing: MissingArtifact, workspace: WorkspaceV1,
) -> str:
    """Human-friendly error string for a missing required artifact."""
    try:
        shown = missing.path.relative_to(REPO_ROOT)
    except ValueError:
        shown = missing.path
    label = missing.artifact.description or missing.artifact.name
    lines = [
        f'Cannot launch {workspace.name}: missing required artifact "{label}"',
        f"  Expected at: {shown}",
    ]
    if missing.artifact.help:
        lines.append(f"  Help: {missing.artifact.help}")
    return "\n".join(lines)
