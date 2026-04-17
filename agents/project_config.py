"""Project registry loader for agents/projects.yaml.

Parses the per-project container/session configuration and exposes it as
frozen dataclasses. Parsed results are cached and automatically reloaded when
the config file's mtime changes.

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

CONTAINER_MODE = "container"
HOST_MODE = "host"
_VALID_MODES = frozenset({CONTAINER_MODE, HOST_MODE})


class ProjectConfigError(ValueError):
    """Raised when the project config is missing required fields or malformed."""


@dataclass(frozen=True)
class RepoMount:
    """A git repo mount spec. Autonomy clones the URL into a managed location
    (e.g. data/repos/) and mounts it at `mount` inside the container."""
    url: str
    mount: str
    writable: bool = False


@dataclass(frozen=True)
class ProjectConfig:
    """Parsed configuration for one project."""
    name: str
    description: str
    mode: str                       # "container" | "host"
    graph_project: str              # hard graph boundary -> GRAPH_SCOPE
    default_tags: tuple[str, ...] = ()      # soft graph tags -> GRAPH_TAGS
    dispatch_labels: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    # Container-only fields (None/empty for host mode):
    image: str | None = None
    repos: tuple[RepoMount, ...] = ()
    working_dir: str | None = None
    claude_md: str | None = None
    startup: str | None = None
    dind: bool = False


_lock = threading.Lock()
_cache: dict[str, ProjectConfig] | None = None
_cache_path: Path | None = None
_cache_mtime: float = 0.0


def _parse_repo(raw: Any, project_name: str, idx: int) -> RepoMount:
    if not isinstance(raw, dict):
        raise ProjectConfigError(
            f"project {project_name!r}: repos[{idx}] must be a mapping, "
            f"got {type(raw).__name__}"
        )
    for required in ("url", "mount"):
        if required not in raw:
            raise ProjectConfigError(
                f"project {project_name!r}: repos[{idx}] missing required field {required!r}"
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


def _parse_project(name: str, raw: Any) -> ProjectConfig:
    if not isinstance(raw, dict):
        raise ProjectConfigError(f"project {name!r} must be a mapping")

    mode = raw.get("mode")
    if mode not in _VALID_MODES:
        raise ProjectConfigError(
            f"project {name!r}: mode must be one of {sorted(_VALID_MODES)} "
            f"(got {mode!r})"
        )

    graph_project = raw.get("graph_project")
    if not isinstance(graph_project, str) or not graph_project:
        raise ProjectConfigError(
            f"project {name!r}: graph_project is required and must be a non-empty string"
        )

    default_tags = tuple(str(t) for t in (raw.get("default_tags") or ()))
    dispatch_labels = tuple(str(l) for l in (raw.get("dispatch_labels") or ()))

    env_raw = raw.get("env") or {}
    if not isinstance(env_raw, dict):
        raise ProjectConfigError(f"project {name!r}: env must be a mapping")
    env = {str(k): str(v) for k, v in env_raw.items()}

    description = str(raw.get("description", "")).strip()

    if mode == CONTAINER_MODE:
        image = raw.get("image")
        if not isinstance(image, str) or not image:
            raise ProjectConfigError(
                f"project {name!r}: container mode requires a non-empty 'image'"
            )
        repos_raw = raw.get("repos") or []
        if not isinstance(repos_raw, list):
            raise ProjectConfigError(f"project {name!r}: 'repos' must be a list")
        repos = tuple(_parse_repo(r, name, i) for i, r in enumerate(repos_raw))
        return ProjectConfig(
            name=name,
            description=description,
            mode=mode,
            graph_project=graph_project,
            default_tags=default_tags,
            dispatch_labels=dispatch_labels,
            env=env,
            image=image,
            repos=repos,
            working_dir=_opt_str(raw, "working_dir"),
            claude_md=_opt_str(raw, "claude_md"),
            startup=_opt_str(raw, "startup"),
            dind=bool(raw.get("dind", False)),
        )

    # host mode — container-specific fields are ignored
    return ProjectConfig(
        name=name,
        description=description,
        mode=mode,
        graph_project=graph_project,
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

    return {str(name): _parse_project(str(name), raw) for name, raw in projects_raw.items()}


def load_projects(
    path: str | Path | None = None,
    *,
    force: bool = False,
) -> dict[str, ProjectConfig]:
    """Return the project registry keyed by project name.

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
    """Look up a single project by name. Raises KeyError if unknown."""
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
