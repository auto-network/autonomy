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
class ProjectConfig:
    """Parsed configuration for one containerized project."""
    id: str                                  # registry key (e.g. "enterprise-ng")
    name: str                                # display name
    description: str
    image: str                               # required
    graph_project: str                       # required — GRAPH_SCOPE
    repos: tuple[RepoMount, ...] = ()
    working_dir: str | None = None
    claude_md: str | None = None
    startup: str | None = None
    dind: bool = False
    default_tags: tuple[str, ...] = ()       # GRAPH_TAGS
    dispatch_labels: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


_lock = threading.Lock()
_cache: dict[str, ProjectConfig] | None = None
_cache_path: Path | None = None
_cache_mtime: float = 0.0


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

    default_tags = tuple(str(t) for t in (raw.get("default_tags") or ()))
    dispatch_labels = tuple(str(l) for l in (raw.get("dispatch_labels") or ()))

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
        claude_md=_opt_str(raw, "claude_md"),
        startup=_opt_str(raw, "startup"),
        dind=bool(raw.get("dind", False)),
        default_tags=default_tags,
        dispatch_labels=dispatch_labels,
        env=env,
    )


def _load(path: Path) -> dict[str, ProjectConfig]:
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

    return {str(pid): _parse_project(str(pid), raw) for pid, raw in projects_raw.items()}


def load_projects(
    path: str | Path | None = None,
    *,
    force: bool = False,
) -> dict[str, ProjectConfig]:
    """Return the project registry keyed by project id.

    The parsed result is cached; reload is triggered when the config file's
    mtime changes. Pass ``force=True`` to bypass the cache (useful in tests).
    """
    global _cache, _cache_path, _cache_mtime

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

        _cache = _load(config_path)
        _cache_path = config_path
        _cache_mtime = mtime
        return _cache


def get_project(name: str, *, path: str | Path | None = None) -> ProjectConfig:
    """Look up a single project by id. Raises KeyError if unknown."""
    projects = load_projects(path=path)
    if name not in projects:
        raise KeyError(f"unknown project: {name!r}")
    return projects[name]


def clear_cache() -> None:
    """Reset the loader cache. Primarily for tests."""
    global _cache, _cache_path, _cache_mtime
    with _lock:
        _cache = None
        _cache_path = None
        _cache_mtime = 0.0
