"""Project registry loader for agents/projects.yaml.

Parses the per-project container configuration and exposes it as frozen
dataclasses. Parsed results are cached and automatically reloaded when the
config file's mtime changes.

Every entry in the registry is a containerized project. Host sessions are a
separate, pre-existing concept and are not modelled here.

Design refs:
    graph://e9448254-18f  Pluggable project-specific container sessions
    graph://9df4dd6d-b34  Hard project + soft tags scoping model
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "agents" / "projects.yaml"
DEFAULT_ARTIFACTS_ROOT = REPO_ROOT / "data" / "artifacts"
ARTIFACTS_MOUNT_DIR = "/etc/autonomy/artifacts"

VALID_ARTIFACT_SCOPES = (
    "personal-org",
    "shared-org",
    "personal-workspace",
    "shared-workspace",
)


class ProjectConfigError(ValueError):
    """Raised when the project config is missing required fields or malformed."""


@dataclass(frozen=True)
class RepoMount:
    """A git repo mount spec. Autonomy clones the URL into a managed location
    (e.g. data/repos/) and either mounts it read-only or creates a worktree
    that gets mounted at `mount` inside the container."""
    url: str
    mount: str
    writable: bool = False


@dataclass(frozen=True)
class ArtifactSpec:
    """A file the project expects to find inside the container.

    Resolved from a layered filesystem under ``data/artifacts/`` based on
    ``scope``. The resolved file is bind-mounted read-only at
    ``/etc/autonomy/artifacts/{name}`` inside the container.
    """
    name: str
    scope: str                               # personal-org | shared-org | personal-workspace | shared-workspace
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
    identity (when the federation read layer ships, ``auto-hoi4``) and
    then to a deterministic generated fallback. See ``graph://497cdc20-d43``.
    """
    slug: str
    name: str | None = None
    byline: str | None = None
    color: str | None = None
    favicon: str | None = None


@dataclass(frozen=True)
class ProjectConfig:
    """Parsed configuration for one containerized project."""
    id: str                                  # registry key (e.g. "enterprise-ng")
    name: str                                # display name
    description: str
    image: str                               # required
    graph_project: str                       # required — GRAPH_SCOPE
    repos: tuple[RepoMount, ...] = ()
    working_dir: str | None = None
    startup: str | None = None
    dind: bool = False
    network_host: bool = True                # --network=host or default bridge
    default_tags: tuple[str, ...] = ()       # GRAPH_TAGS
    dispatch_labels: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    env_from_host: tuple[str, ...] = ()      # forwarded from launcher's os.environ
    artifacts: tuple[ArtifactSpec, ...] = ()


_lock = threading.Lock()
_cache: dict[str, ProjectConfig] | None = None
_cache_path: Path | None = None
_cache_mtime: float = 0.0
_orgs_cache: dict[str, OrgOverride] | None = None


def _parse_repo(raw: Any, project_id: str, idx: int) -> RepoMount:
    if not isinstance(raw, dict):
        raise ProjectConfigError(
            f"project {project_id!r}: repos[{idx}] must be a mapping, "
            f"got {type(raw).__name__}"
        )
    for required in ("url", "mount"):
        if required not in raw:
            raise ProjectConfigError(
                f"project {project_id!r}: repos[{idx}] missing required field {required!r}"
            )
    return RepoMount(
        url=str(raw["url"]),
        mount=str(raw["mount"]),
        writable=bool(raw.get("writable", False)),
    )


def _opt_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    return str(value)


def _parse_artifact(raw: Any, project_id: str, idx: int) -> ArtifactSpec:
    if not isinstance(raw, dict):
        raise ProjectConfigError(
            f"project {project_id!r}: artifacts[{idx}] must be a mapping, "
            f"got {type(raw).__name__}"
        )
    for required in ("name", "scope"):
        if required not in raw:
            raise ProjectConfigError(
                f"project {project_id!r}: artifacts[{idx}] missing required field {required!r}"
            )
    scope = str(raw["scope"])
    if scope not in VALID_ARTIFACT_SCOPES:
        raise ProjectConfigError(
            f"project {project_id!r}: artifacts[{idx}] has invalid scope {scope!r}; "
            f"must be one of {', '.join(VALID_ARTIFACT_SCOPES)}"
        )
    return ArtifactSpec(
        name=str(raw["name"]),
        scope=scope,
        required=bool(raw.get("required", False)),
        description=str(raw.get("description", "")),
        help=str(raw.get("help", "")),
    )


def _parse_project(project_id: str, raw: Any) -> ProjectConfig:
    if not isinstance(raw, dict):
        raise ProjectConfigError(f"project {project_id!r} must be a mapping")

    image = raw.get("image")
    if not isinstance(image, str) or not image:
        raise ProjectConfigError(
            f"project {project_id!r}: 'image' is required and must be a non-empty string"
        )

    graph_project = raw.get("graph_project")
    if not isinstance(graph_project, str) or not graph_project:
        raise ProjectConfigError(
            f"project {project_id!r}: 'graph_project' is required and must be a non-empty string"
        )

    repos_raw = raw.get("repos") or []
    if not isinstance(repos_raw, list):
        raise ProjectConfigError(f"project {project_id!r}: 'repos' must be a list")
    repos = tuple(_parse_repo(r, project_id, i) for i, r in enumerate(repos_raw))

    env_raw = raw.get("env") or {}
    if not isinstance(env_raw, dict):
        raise ProjectConfigError(f"project {project_id!r}: 'env' must be a mapping")
    env = {str(k): str(v) for k, v in env_raw.items()}

    env_from_host_raw = raw.get("env_from_host") or ()
    if not isinstance(env_from_host_raw, (list, tuple)):
        raise ProjectConfigError(
            f"project {project_id!r}: 'env_from_host' must be a list of variable names"
        )
    env_from_host = tuple(str(v) for v in env_from_host_raw)

    default_tags = tuple(str(t) for t in (raw.get("default_tags") or ()))
    dispatch_labels = tuple(str(l) for l in (raw.get("dispatch_labels") or ()))

    artifacts_raw = raw.get("artifacts") or []
    if not isinstance(artifacts_raw, list):
        raise ProjectConfigError(f"project {project_id!r}: 'artifacts' must be a list")
    artifacts = tuple(
        _parse_artifact(a, project_id, i) for i, a in enumerate(artifacts_raw)
    )

    name = str(raw.get("name") or project_id)
    description = str(raw.get("description", "")).strip()

    return ProjectConfig(
        id=project_id,
        name=name,
        description=description,
        image=image,
        graph_project=graph_project,
        repos=repos,
        working_dir=_opt_str(raw, "working_dir"),
        startup=_opt_str(raw, "startup"),
        dind=bool(raw.get("dind", False)),
        network_host=bool(raw.get("network_host", True)),
        default_tags=default_tags,
        dispatch_labels=dispatch_labels,
        env=env,
        env_from_host=env_from_host,
        artifacts=artifacts,
    )


def _parse_org(slug: str, raw: Any) -> OrgOverride:
    if raw is None:
        return OrgOverride(slug=slug)
    if not isinstance(raw, dict):
        raise ProjectConfigError(f"orgs[{slug!r}] must be a mapping")
    return OrgOverride(
        slug=slug,
        name=_opt_str(raw, "name"),
        byline=_opt_str(raw, "byline"),
        color=_opt_str(raw, "color"),
        favicon=_opt_str(raw, "favicon"),
    )


def _load(path: Path) -> tuple[dict[str, ProjectConfig], dict[str, OrgOverride]]:
    try:
        text = path.read_text()
    except OSError as e:
        raise ProjectConfigError(f"cannot read project config {path}: {e}")

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ProjectConfigError(f"invalid YAML in {path}: {e}")

    if not isinstance(data, dict):
        raise ProjectConfigError(f"{path}: top-level must be a mapping")

    projects_raw = data.get("projects")
    if not isinstance(projects_raw, dict):
        raise ProjectConfigError(f"{path}: must contain a 'projects' mapping")

    orgs_raw = data.get("orgs") or {}
    if not isinstance(orgs_raw, dict):
        raise ProjectConfigError(f"{path}: 'orgs' must be a mapping")

    projects = {str(pid): _parse_project(str(pid), raw) for pid, raw in projects_raw.items()}
    orgs = {str(slug): _parse_org(str(slug), raw) for slug, raw in orgs_raw.items()}
    return projects, orgs


def load_projects(
    path: str | Path | None = None,
    *,
    force: bool = False,
) -> dict[str, ProjectConfig]:
    """Return the project registry keyed by project id.

    The parsed result is cached; reload is triggered when the config file's
    mtime changes. Pass ``force=True`` to bypass the cache (useful in tests).
    """
    global _cache, _cache_path, _cache_mtime, _orgs_cache

    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    config_path = config_path.resolve()

    with _lock:
        try:
            mtime = config_path.stat().st_mtime
        except OSError as e:
            raise ProjectConfigError(f"cannot stat project config {config_path}: {e}")

        if (
            not force
            and _cache is not None
            and _cache_path == config_path
            and _cache_mtime == mtime
        ):
            return _cache

        projects, orgs = _load(config_path)
        _cache = projects
        _orgs_cache = orgs
        _cache_path = config_path
        _cache_mtime = mtime
        return _cache


def load_org_overrides(
    path: str | Path | None = None,
    *,
    force: bool = False,
) -> dict[str, OrgOverride]:
    """Return operator-local org overrides keyed by slug.

    Stage 1 source for org identity per ``graph://497cdc20-d43`` —
    operator overrides take priority over canonical-from-federation
    (``auto-hoi4``, not yet shipped) and over generated fallback.
    """
    load_projects(path=path, force=force)  # populates _orgs_cache
    return _orgs_cache or {}


def get_project(name: str, *, path: str | Path | None = None) -> ProjectConfig:
    """Look up a single project by id. Raises KeyError if unknown."""
    projects = load_projects(path=path)
    if name not in projects:
        raise KeyError(f"unknown project: {name!r}")
    return projects[name]


def clear_cache() -> None:
    """Reset the loader cache. Primarily for tests."""
    global _cache, _cache_path, _cache_mtime, _orgs_cache
    with _lock:
        _cache = None
        _cache_path = None
        _cache_mtime = 0.0
        _orgs_cache = None


# ── Artifact resolution ────────────────────────────────────────────────

def artifact_host_path(
    artifact: ArtifactSpec,
    project: ProjectConfig,
    *,
    artifacts_root: Path | str = DEFAULT_ARTIFACTS_ROOT,
) -> Path:
    """Resolve an artifact's host filesystem path under ``artifacts_root``.

    Layering maps ``scope`` to a path of the form::

        {root}/{shared|personal}/{org}[/{workspace}]/{name}

    where ``org`` is ``project.graph_project`` and ``workspace`` is
    ``project.id`` (included only for ``-workspace`` scopes).
    """
    root = Path(artifacts_root)
    share = "shared" if artifact.scope.startswith("shared-") else "personal"
    base = root / share / project.graph_project
    if artifact.scope.endswith("-workspace"):
        base = base / project.id
    return base / artifact.name


def validate_artifacts(
    project: ProjectConfig,
    *,
    artifacts_root: Path | str = DEFAULT_ARTIFACTS_ROOT,
) -> list[MissingArtifact]:
    """Return required artifacts that are missing on disk."""
    missing: list[MissingArtifact] = []
    for art in project.artifacts:
        if not art.required:
            continue
        path = artifact_host_path(art, project, artifacts_root=artifacts_root)
        if not path.exists():
            missing.append(MissingArtifact(artifact=art, path=path, project_id=project.id))
    return missing


def artifact_mounts(
    project: ProjectConfig,
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
    for art in project.artifacts:
        host = artifact_host_path(art, project, artifacts_root=artifacts_root)
        if host.exists():
            mounts[str(host)] = f"{ARTIFACTS_MOUNT_DIR}/{art.name}:ro"
    return mounts


def format_missing_artifact_error(missing: MissingArtifact, project: ProjectConfig) -> str:
    """Human-friendly error string for a missing required artifact.

    Used by the WebSocket path (rendered as red ANSI) and as the ``error``
    field in the REST API response. The ``path`` shown is relative to
    ``REPO_ROOT`` when possible, otherwise the absolute host path.
    """
    try:
        shown = missing.path.relative_to(REPO_ROOT)
    except ValueError:
        shown = missing.path
    label = missing.artifact.description or missing.artifact.name
    lines = [
        f'Cannot launch {project.name}: missing required artifact "{label}"',
        f"  Expected at: {shown}",
    ]
    if missing.artifact.help:
        lines.append(f"  Help: {missing.artifact.help}")
    return "\n".join(lines)
