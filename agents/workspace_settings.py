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
import os
import threading
import re as _re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.graph import ops, org_ops
from tools.graph.schemas.registry import get_schema
from tools.graph.schemas.workspace import (
    LOCAL_REPO_NAME_RE,
    WORKSPACE_SET_ID,
    WORKSPACE_REVISION,
    WORKSPACE_REVISION_2,
)
from tools.graph.schemas.workspace_artifact import (
    SET_ID as ARTIFACT_SET_ID,
    SCHEMA_REVISION as ARTIFACT_REVISION,
    VALID_SCOPES as VALID_ARTIFACT_SCOPES,
)
from tools.graph.schemas.org import ORG_SET_ID, ORG_REVISION
from tools.graph.schemas.mount import (
    SET_ID as MOUNT_SET_ID,
    MOUNT_SCHEMA_REVISION_3 as MOUNT_REVISION_3,
    WorkspaceMountV3,
)
from tools.graph.schemas.workspace_capability_enable import (
    SET_ID as WORKSPACE_CAPABILITY_ENABLE_SET_ID,
    SCHEMA_REVISION as WORKSPACE_CAPABILITY_ENABLE_REVISION,
)
from tools.graph.schemas.org_capability_install import (
    SET_ID as ORG_CAPABILITY_INSTALL_SET_ID,
    SCHEMA_REVISION as ORG_CAPABILITY_INSTALL_REVISION,
)
from tools.graph.schemas.capability_impl import (
    SET_ID as CAPABILITY_IMPL_SET_ID,
    SCHEMA_REVISION as CAPABILITY_IMPL_REVISION,
)
from tools.graph.schemas.capability_contract import (
    SET_ID as CAPABILITY_CONTRACT_SET_ID,
    SCHEMA_REVISION as CAPABILITY_CONTRACT_REVISION,
)
from tools.graph.schemas.org_capability_primer import (
    SET_ID as ORG_CAPABILITY_PRIMER_SET_ID,
    SCHEMA_REVISION as ORG_CAPABILITY_PRIMER_REVISION,
    resolve_markdown as resolve_capability_primer_markdown,
    resolve_order as resolve_capability_primer_order,
)
from tools.graph.settings_ops import ResolvedSetting
from tools.graph.capability_chain import (
    CapabilityChainIssue,
    validate_capability_chain,
)
from tools.data_paths import DATA_ROOT

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARTIFACTS_ROOT = DATA_ROOT / "artifacts"
#: Root of the dashboard-managed local repositories (``<root>/<org>/<id>``).
#: Same value as ``workspace_manager.LOCAL_WORKSPACE_REPOS_DIR`` (a test
#: asserts they cannot drift).
LOCAL_WORKSPACE_REPOS_ROOT = DATA_ROOT / "workspace-repos"
ARTIFACTS_MOUNT_DIR = "/etc/autonomy/artifacts"
CAPABILITIES_MOUNT_DIR = "/opt/autonomy/capabilities"

ARTIFACT_PATH_SET_ID = "autonomy.artifact-path"
ARTIFACT_PATH_REVISION = 1

logger = logging.getLogger(__name__)


# ── Setting-event-invalidated cache ──────────────────────────
#
# load_workspaces() and load_org_overrides() each iterate every per-org
# DB and run a Setting read against each one. On a 3-org installation
# that's ~6 SQL fan-outs costing several seconds per call. They were
# being invoked once per row from dashboard DAOs (get_recent_sessions:
# 1000+ rows; session_monitor: ~50; etc.), turning routine endpoints
# into multi-second hangs.
#
# These functions are pure reads of slow-changing Settings, so they're
# memoised process-wide and invalidated by the post-commit
# ``setting.changed`` hook in the dashboard.  An RLock deliberately stays
# held through a rebuild: concurrent request threads then share one rebuild
# instead of stampeding every org DB after an invalidation.
_cache_lock = threading.RLock()
_cache_context: tuple[str, str] | None = None
_workspaces_cache_value: "dict[str, WorkspaceV1] | None" = None
_overrides_cache_value: "dict[str, OrgOverride] | None" = None
#: Bumped on every invalidation of ``_overrides_cache_value``. Downstream
#: caches derived from the overrides (org_identity's per-slug identity cache)
#: key on this instead of registering their own invalidation callback, so a
#: new invalidation site cannot forget to notify them — the generation moves
#: and their key changes with it.
_overrides_generation: int = 0


def overrides_generation() -> int:
    """Monotonic counter identifying the current org-override snapshot."""
    return _overrides_generation

_WORKSPACE_COMPOSITION_SET_IDS = frozenset({
    WORKSPACE_SET_ID,
    ARTIFACT_SET_ID,
    MOUNT_SET_ID,
    WORKSPACE_CAPABILITY_ENABLE_SET_ID,
    ORG_CAPABILITY_INSTALL_SET_ID,
    CAPABILITY_CONTRACT_SET_ID,
    CAPABILITY_IMPL_SET_ID,
    ORG_CAPABILITY_PRIMER_SET_ID,
})


def invalidate_caches() -> None:
    """Drop the in-process workspace + org-override caches.

    Useful for tests and lifecycle boundaries. Production mutations call
    :func:`invalidate_for_setting` from the post-commit event hook.
    """
    global _workspaces_cache_value, _overrides_cache_value, _overrides_generation
    with _cache_lock:
        _workspaces_cache_value = None
        _overrides_cache_value = None
        _overrides_generation += 1


def _ensure_cache_context() -> None:
    """Clear snapshots when the process is explicitly repointed at another DB."""
    global _cache_context, _workspaces_cache_value, _overrides_cache_value
    global _overrides_generation
    from tools.graph.cross_org import _orgs_root

    context = (str(_orgs_root()), os.environ.get("GRAPH_DB", ""))
    if context != _cache_context:
        _cache_context = context
        _workspaces_cache_value = None
        _overrides_cache_value = None
        _overrides_generation += 1


def invalidate_for_setting(set_id: str) -> None:
    """Invalidate only caches whose composition depends on *set_id*."""
    global _workspaces_cache_value, _overrides_cache_value, _overrides_generation
    with _cache_lock:
        if set_id in _WORKSPACE_COMPOSITION_SET_IDS:
            _workspaces_cache_value = None
        if set_id == ORG_SET_ID:
            _overrides_cache_value = None
            _overrides_generation += 1


class WorkspaceSettingsError(ValueError):
    """Raised when Setting-derived workspace data is missing or malformed."""


class WorkspaceMountError(Exception):
    """Base for mount-resolution failures (missing or unusable host path).

    Both concrete mount errors derive from this so a single ``except
    WorkspaceMountError`` in the session-create handler returns a clean
    error instead of letting the failure escape as an unhandled 500.
    """


class WorkspaceMountMissingError(WorkspaceMountError):
    """A required ``autonomy.workspace.mount#1`` host path is absent.

    Surfaces enough provenance (origin org, state, the exact paths) for
    an operator to find the mount declaration and either populate the
    host path, mark the mount optional, or exclude/override the Setting
    in their own DB.
    """

    def __init__(
        self,
        *,
        mount_key: str,
        origin_org: str | None,
        state: str,
        host_path: str,
        container_path: str,
    ):
        self.mount_key = mount_key
        self.origin_org = origin_org
        self.state = state
        self.host_path = host_path
        self.container_path = container_path
        super().__init__(
            f"Required mount {mount_key!r} from org={origin_org} state={state}: "
            f"host path {host_path!r} does not exist on this machine."
        )


class WorkspaceMountInvalidError(WorkspaceMountError):
    """A mount's declared host path exists but is not usable."""

    def __init__(self, *, mount_key: str, reason: str):
        self.mount_key = mount_key
        self.reason = reason
        super().__init__(f"Invalid mount {mount_key!r}: {reason}")


class WorkspaceMountFrameError(WorkspaceMountInvalidError):
    """This process cannot see/translate the volume needed to answer."""


# ── Typed composition models ────────────────────────────────


_SSH_SCHEME_RE = _re.compile(
    r"^ssh://(?:(?P<user>[^@/]+)@)?(?P<host>[^/:]+)(?::\d+)?(?P<path>/.*)$")
_SCP_RE = _re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^/:]+):(?P<path>.+)$")


def split_remote_url(url: str) -> tuple[str | None, str, str]:
    """Split a git remote into ``(user, host, path)``, losing nothing.

    The path keeps its leading slash when it has one — an absolute path on
    the server is a different location from a path relative to the login
    user's home, and dropping the distinction silently repoints the remote.
    A trailing ``.git`` is dropped and re-added on composition.

    Raises ``WorkspaceSettingsError`` for a form that would not survive the
    round trip, rather than storing something that composes back to a
    different remote.
    """
    # A scheme other than ssh:// must be refused explicitly: the scp-style
    # pattern happily reads "https://host/path" as host "https" with the rest
    # as its path, which composes back into nonsense.
    if "://" in url and not url.startswith("ssh://"):
        raise WorkspaceSettingsError(
            f"unrecognized git remote {url!r}: only ssh remotes decompose "
            f"into a user, host and path"
        )
    m = _SSH_SCHEME_RE.match(url) or _SCP_RE.match(url)
    if not m:
        raise WorkspaceSettingsError(
            f"unrecognized git remote {url!r}: expected user@host:path, "
            f"ssh://user@host/path, or an absolute local path"
        )
    path = m.group("path")
    if path.endswith(".git"):
        path = path[:-4]
    return m.group("user"), m.group("host"), path


@dataclass(frozen=True)
class RepoMount:
    """Git repo mount spec from the workspace Setting payload.

    ``host`` and ``repo`` are stored; the clone URL is composed from them.
    A credential is keyed by host, so storing the host is what lets the
    reference be a plain value instead of something parsed back out of a
    URL at the moment it is needed.

    """
    host: str | None
    repo: str | None
    mount: str
    user: str | None = None
    local_path: str | None = None
    writable: bool = False
    #: True when the row declared ``local: true`` (the workspace's
    #: dashboard-managed local repository). ``local_path`` then carries
    #: the NODE-resolved absolute path
    #: (``LOCAL_WORKSPACE_REPOS_ROOT/<org>/<workspace-id>``) so every
    #: downstream consumer of ``url`` / ``local_path`` works unchanged;
    #: this flag records the portable declaration for provenance.
    local: bool = False

    @property
    def url(self) -> str:
        """The clone URL, composed from whichever form names the repository.

        The login user is part of a remote's address, so it is stored; it is
        ``git`` on every hosted forge and something else on a private server.
        An absolute path is a path on that server and takes the ``ssh://``
        form; a relative one is relative to the login user's home and takes
        the scp-style form. Choosing between them by looking at the path is
        what keeps both round-trips exact.
        """
        if self.local_path:
            return self.local_path
        user = self.user or "git"
        if str(self.repo).startswith("/"):
            return f"ssh://{user}@{self.host}{self.repo}.git"
        return f"{user}@{self.host}:{self.repo}.git"

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> "RepoMount":
        """Build one from a clone URL, splitting it into the stored form.

        For callers that genuinely hold a URL and nothing else. The stored
        shape is host and repo -- or a local path -- so the split happens
        once, here, rather than at every read.
        """
        if url.startswith("/"):
            return cls(host=None, repo=None, local_path=url, **kwargs)
        user, host, path = split_remote_url(url)
        return cls(host=host, repo=path, user=user, **kwargs)


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
class CapabilityToolTarget:
    """Stable runtime mount + command surface for a capability bundle.

    Built from the optional ``tool_target`` field on
    ``autonomy.capability.impl#1``. ``source`` is a repo-local path that
    holds the tool subtree; ``target`` is the absolute container path
    where the launcher mounts that subtree (e.g. ``/opt/jira-tools``).
    ``expose_commands`` lists bare command names the launcher should make
    available on PATH via shim scripts so ``jira-read``, ``jira-comment``,
    etc. resolve without the agent guessing the install layout.
    """
    source: str
    target: str
    expose_commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class MaterializedCapability:
    """A capability resolved for a workspace and ready to materialize at launch.

    Composed from the ``autonomy.workspace.capability.enable#1`` →
    ``autonomy.org.capability.install#1`` → ``autonomy.capability.impl#1``
    chain established in ``auto-0ase8`` (see graph://86e04207-a25). Carries
    every field the launcher and primer renderer need so the runtime does
    not have to walk the chain a second time.

    Path fields are repo-local (validated at the schema layer); the
    launcher resolves them against ``REPO_ROOT`` to produce host paths.
    ``mount_target`` is the canonical container path for the package root
    (``/opt/autonomy/capabilities/<impl-slug>``).

    ``tool_target`` (when present) carries a ``CapabilityToolTarget``
    describing where the implementation's tool subtree should land inside
    the container plus which commands the launcher should expose on PATH.
    """
    contract: str
    contract_version: int
    implementation: str
    implementation_version: int
    delivery_mode: str
    package_root: str
    mount_target: str
    required_env: tuple[str, ...] = ()
    required_secret_files: tuple[str, ...] = ()
    tool_paths: tuple[str, ...] = ()
    tool_target: CapabilityToolTarget | None = None
    primer_path: str | None = None
    skill_path: str | None = None
    env_bindings: dict[str, str] = field(default_factory=dict)
    secret_file_bindings: dict[str, str] = field(default_factory=dict)
    # ``workspace_overrides`` from the enable Setting — workspace-local
    # data the projections layer on top of the org install (e.g. the
    # issue_tracker named queries the primer renders).
    workspace_overrides: dict = field(default_factory=dict)
    # Additive organization-owned guidance composed from
    # ``autonomy.org.capability.primer#1``. The implementation's checked-in
    # primer/skill remains the portable base; this carries provider-instance
    # field ids, workflow rules, and similar org-local context.
    org_primer: str = ""


@dataclass(frozen=True)
class WorkspaceV1:
    """Composed workspace config — one ``autonomy.workspace#1`` Setting
    plus its attached artifact + mount Settings plus the owning org slug.

    Field names match the legacy ``ProjectConfig`` surface so existing
    consumers (dispatcher, session launcher, primer renderer, dashboard)
    read the same attributes. ``mounts`` carries resolved
    ``autonomy.workspace.mount#1`` Settings keyed by composite key
    (``<workspace-id>:<mount-name>``) so consumers can inspect
    origin/state for error reporting without re-querying.

    ``capabilities`` carries every enabled capability resolved through
    the workspace-enable / org-install / impl chain (see
    :func:`resolve_capabilities`). Sorted by contract name for stable
    ordering across launches and primer renders.
    ``capability_issues`` keeps broken enabled chains attached to their own
    workspace so readiness can report them without hiding healthy workspaces.
    Launch is deliberately fail-open: invalid capabilities are omitted while
    the base workspace remains available.
    """
    id: str
    name: str
    description: str
    image: str
    graph_project: str
    harness: str = "claude"
    model: str | None = None
    repos: tuple[RepoMount, ...] = ()
    working_dir: str | None = None
    needs_nested_docker: bool = False
    session_runtime: str | None = None
    network_host: bool = True
    default_tags: tuple[str, ...] = ()
    dispatch_labels: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    env_from_host: tuple[str, ...] = ()
    artifacts: tuple[ArtifactSpec, ...] = ()
    mounts: dict[str, ResolvedSetting] = field(default_factory=dict)
    capabilities: tuple[MaterializedCapability, ...] = ()
    capability_issues: tuple[CapabilityChainIssue, ...] = ()
    #: Stated reason for mounting the LIVE host platform checkout (with its
    #: data/) instead of the default git snapshot. ``None`` = snapshot (the
    #: default for every workspace). Set only via the explicit
    #: ``host_root_mount.reason`` Setting field — never inferred.
    host_root_mount_reason: str | None = None

    def __post_init__(self) -> None:
        if self.session_runtime is None:
            object.__setattr__(
                self,
                "session_runtime",
                "privileged" if self.needs_nested_docker else "standard",
            )

    @property
    def dind(self) -> bool:
        """Deprecated compatibility alias for ``needs_nested_docker``."""
        return self.needs_nested_docker


# ── Setting payload → typed model helpers ──────────────────


def resolve_local_repo_path(org: str, workspace_id: str) -> Path:
    """Node-frame home of a ``local: true`` repo: ``<root>/<org>/<id>``.

    Both components are held to the managed store's strict slug (``org``
    comes from DB routing, ``workspace_id`` is the Setting key) so a
    malformed value can never compose a path outside the store.
    """
    for label, value in (("org", org), ("workspace_id", workspace_id)):
        if not isinstance(value, str) or not LOCAL_REPO_NAME_RE.fullmatch(value):
            raise WorkspaceSettingsError(
                f"invalid local workspace repository {label}: {value!r}"
            )
    return LOCAL_WORKSPACE_REPOS_ROOT / org / workspace_id


def _parse_repo(
    raw: Any, workspace_id: str, idx: int, org: str | None = None,
) -> RepoMount:
    if not isinstance(raw, dict):
        raise WorkspaceSettingsError(
            f"workspace {workspace_id!r}: repos[{idx}] must be a mapping"
        )
    for key in ("mount",):
        if key not in raw:
            raise WorkspaceSettingsError(
                f"workspace {workspace_id!r}: repos[{idx}] missing {key!r}"
            )
    for dead in ("local_path", "base_source"):
        if raw.get(dead):
            # The field was deleted from the schema; resolving it would
            # apply a machine path that is wrong everywhere but one
            # computer. Refuse by name so the row gets rewritten.
            raise WorkspaceSettingsError(
                f"workspace {workspace_id!r}: repos[{idx}].{dead} was "
                f"deleted — rewrite the row to 'local: true' or "
                f"'host'+'repo'"
            )
    local = raw.get("local") is True
    local_path: str | None = None
    if local:
        # Portable form: the row names no location. Resolve on THIS node
        # from the owning org, the workspace id, and the node's own store
        # root; the launcher creates the bare repo at first launch.
        if not org:
            raise WorkspaceSettingsError(
                f"workspace {workspace_id!r}: repos[{idx}] declares "
                f"'local: true' but has no owning org to resolve under"
            )
        local_path = str(resolve_local_repo_path(org, workspace_id))
    return RepoMount(
        host=str(raw["host"]) if raw.get("host") else None,
        repo=str(raw["repo"]) if raw.get("repo") else None,
        user=str(raw["user"]) if raw.get("user") else None,
        local_path=local_path,
        mount=str(raw["mount"]),
        writable=bool(raw.get("writable", False)),
        local=local,
    )


def _workspace_from_setting(
    setting_payload: dict,
    workspace_id: str,
    graph_project: str,
    artifacts: tuple[ArtifactSpec, ...],
    mounts: dict[str, ResolvedSetting],
    capabilities: tuple[MaterializedCapability, ...] = (),
    capability_issues: tuple[CapabilityChainIssue, ...] = (),
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
        _parse_repo(r, workspace_id, i, graph_project)
        for i, r in enumerate(repos_raw)
    )
    env_raw = setting_payload.get("env") or {}
    env = {str(k): str(v) for k, v in env_raw.items()}
    harness = setting_payload.get("harness") or "claude"
    if not isinstance(harness, str) or harness not in {"claude", "codex"}:
        raise WorkspaceSettingsError(
            f"workspace {workspace_id!r}: invalid harness {harness!r}"
        )
    model_raw = setting_payload.get("model")
    model = model_raw if isinstance(model_raw, str) and model_raw else None
    needs_nested_docker = bool(
        setting_payload.get(
            "needs_nested_docker",
            setting_payload.get("dind", False),
        )
    )
    runtime_raw = setting_payload.get("session_runtime")
    session_runtime = (
        str(runtime_raw)
        if runtime_raw
        else ("privileged" if needs_nested_docker else "standard")
    )
    host_root_raw = setting_payload.get("host_root_mount")
    host_root_mount_reason: str | None = None
    if host_root_raw is not None:
        reason = (
            host_root_raw.get("reason") if isinstance(host_root_raw, dict) else None
        )
        if not isinstance(reason, str) or not reason.strip():
            raise WorkspaceSettingsError(
                f"workspace {workspace_id!r}: host_root_mount requires a "
                "non-empty 'reason' string"
            )
        host_root_mount_reason = reason.strip()
    return WorkspaceV1(
        id=workspace_id,
        name=str(setting_payload.get("name") or workspace_id),
        description=str(setting_payload.get("description") or ""),
        image=image,
        graph_project=graph_project,
        harness=harness,
        model=model,
        repos=repos,
        working_dir=(setting_payload.get("working_dir") or None),
        needs_nested_docker=needs_nested_docker,
        session_runtime=session_runtime,
        # Default mirrors the schema (False). Every stored row now states
        # network_host explicitly, so this fallback governs only rows created
        # in future -- where host networking, being a privilege, is off unless
        # asked for. It previously defaulted True, which granted host
        # networking to any workspace that simply omitted the field.
        network_host=bool(setting_payload.get("network_host", False)),
        default_tags=tuple(str(t) for t in (setting_payload.get("tags") or ())),
        dispatch_labels=tuple(
            str(l) for l in (setting_payload.get("dispatch_labels") or ())
        ),
        env=env,
        env_from_host=tuple(
            str(v) for v in (setting_payload.get("env_from_host") or ())
        ),
        artifacts=artifacts,
        mounts=mounts,
        capabilities=capabilities,
        capability_issues=capability_issues,
        host_root_mount_reason=host_root_mount_reason,
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
        # Default mirrors the schema (True). Every stored artifact row states
        # required explicitly, so this governs only future rows -- where an
        # artifact declared without saying otherwise is one the workspace
        # needs, and a missing file should stop the launch rather than start
        # a container that is quietly missing a credential.
        required=bool(payload.get("required", True)),
        description=str(payload.get("description") or ""),
        help=str(payload.get("help") or ""),
    )


# ── Public read path ───────────────────────────────────────


PROVISION_SET_ID = "autonomy.workspace.provision"


def resolve_provision(workspace_id: str, *, org: str | None) -> dict:
    """Merged ``autonomy.workspace.provision#1`` payload for a workspace.

    The org row is the shared definition; a personal-store row under the
    same key shadows it PER FIELD (present fields win, absent fields fall
    through). This deliberate second read is the only personal-override
    channel — the set's publication band pins ``max=curated``, so a
    personal or machine row can never enter a federated resolution.
    """
    merged: dict[str, Any] = {}
    stores = [org] if org == "personal" else [org, "personal"]
    for store in stores:
        row = ops.read_set_key(
            PROVISION_SET_ID, workspace_id, org=store, peers=[],
        )
        payload = (row or {}).get("payload") or {}
        for name, value in payload.items():
            if value is not None:
                merged[name] = value
    return merged


def materialize_startup_script(proj, run_dir: Path) -> Path | None:
    """The host path to mount read-only at ``/startup.sh``, or None.

    A provision row's ``startup_script`` is written to the session's run
    dir; no row, no startup script. (The legacy repo-relative ``startup``
    path field is dead: every workspace that had one was ported to
    ``autonomy.workspace.provision`` on 2026-08-28 and proven with a real
    launch.)
    """
    content = resolve_provision(
        proj.id, org=proj.graph_project,
    ).get("startup_script")
    if not content:
        return None
    path = Path(run_dir) / "startup.sh"
    path.write_text(content)
    path.chmod(0o755)
    return path


def _artifacts_for_workspace(
    workspace_id: str, *, org: str | None,
) -> tuple[ArtifactSpec, ...]:
    """Read the ``autonomy.workspace.artifact#1`` Set and filter by
    composite-key prefix ``<workspace-id>:``.
    """
    members = ops.read_set(
        ARTIFACT_SET_ID, org=org, peers=[],
    ).members
    prefix = f"{workspace_id}:"
    out: list[ArtifactSpec] = []
    for m in members:
        if not m.key.startswith(prefix):
            continue
        out.append(_artifact_from_setting(m.key, m.payload, workspace_id))
    out.sort(key=lambda a: a.name)
    return tuple(out)


def _artifacts_by_workspace(
    workspace_ids: set[str], *, org: str | None,
) -> dict[str, tuple[ArtifactSpec, ...]]:
    """Resolve artifacts for many workspaces with one Set read."""
    grouped: dict[str, list[ArtifactSpec]] = {wid: [] for wid in workspace_ids}
    for member in ops.read_set(ARTIFACT_SET_ID, org=org, peers=[]).members:
        workspace_id, separator, _ = member.key.partition(":")
        if not separator or workspace_id not in grouped:
            continue
        grouped[workspace_id].append(
            _artifact_from_setting(member.key, member.payload, workspace_id)
        )
    return {
        workspace_id: tuple(sorted(values, key=lambda artifact: artifact.name))
        for workspace_id, values in grouped.items()
    }


def load_mounts(
    workspace_id: str, *, org: str | None = None,
) -> dict[str, ResolvedSetting]:
    """Return the :class:`WorkspaceMountV3` Settings for *workspace_id*.

    Reads ``autonomy.workspace.mount#3`` with composite-key prefix
    ``<workspace-id>:`` and validates each payload through
    :class:`WorkspaceMountV3`. The returned dict maps composite key
    (``<workspace-id>:<mount-name>``) to the resolved Setting so
    consumers can inspect ``payload`` (typed), ``state``, and ``org``
    without re-querying. Missing schema registration returns an empty
    dict (mount declaration is optional per workspace).
    """
    if get_schema(MOUNT_SET_ID, MOUNT_REVISION_3) is None:
        return {}
    # Rev 3: guarded subpath, or machine-located (no source stored; the
    # machine's own artifact-path row locates it). host_path is deleted; a
    # straggler row fails rev-3 validation loudly instead of resolving a
    # machine path.
    return ops.read_set(
        MOUNT_SET_ID,
        org=org,
        peers=[],
        target_revision=MOUNT_REVISION_3,
        prefix=workspace_id,
        model=WorkspaceMountV3,
    ).to_dict()


def _mounts_by_workspace(
    workspace_ids: set[str], *, org: str | None,
) -> dict[str, dict[str, ResolvedSetting]]:
    """Resolve mounts for many workspaces with one Set read."""
    grouped: dict[str, dict[str, ResolvedSetting]] = {
        wid: {} for wid in workspace_ids
    }
    if get_schema(MOUNT_SET_ID, MOUNT_REVISION_3) is None:
        return grouped
    members = ops.read_set(
        MOUNT_SET_ID, org=org, peers=[],
        target_revision=MOUNT_REVISION_3, model=WorkspaceMountV3,
    ).members
    for member in members:
        workspace_id, separator, _ = member.key.partition(":")
        if separator and workspace_id in grouped:
            grouped[workspace_id][member.key] = member
    return grouped


def _impl_slug(name: str) -> str:
    """Convert an implementation name like ``autonomy/github`` to a path slug.

    Slashes become hyphens so the value is safe to splice into a container
    path (``/opt/autonomy/capabilities/<slug>``) without shell-quoting or
    nested-directory surprises.
    """
    return name.replace("/", "-")


def _impl_mount_target(name: str) -> str:
    """Container path where an implementation's package root is mounted."""
    return f"{CAPABILITIES_MOUNT_DIR}/{_impl_slug(name)}"


def _read_capability_impls(*, org: str | None) -> dict[str, dict]:
    """Return every visible ``autonomy.capability.impl#1`` keyed by name.

    Implementations are typically published in a sharing org and consumed
    by every org that installs them, so peers stay enabled (the default
    cross-org read path) — only canonical/published rows surface from
    peers (graph://bcce359d-a1d).
    """
    if get_schema(CAPABILITY_IMPL_SET_ID, CAPABILITY_IMPL_REVISION) is None:
        return {}
    members = ops.read_set(CAPABILITY_IMPL_SET_ID, org=org).members
    out: dict[str, dict] = {}
    for m in members:
        name = m.payload.get("name")
        if isinstance(name, str):
            out[name] = m.payload
    return out


def _read_capability_contracts(*, org: str | None) -> dict[str, dict]:
    """Return visible capability contracts keyed by their stable name."""
    if get_schema(CAPABILITY_CONTRACT_SET_ID, CAPABILITY_CONTRACT_REVISION) is None:
        return {}
    return {
        member.key: member.payload
        for member in ops.read_set(CAPABILITY_CONTRACT_SET_ID, org=org).members
    }


def _materialize_tool_target(payload: Any) -> CapabilityToolTarget | None:
    """Build a :class:`CapabilityToolTarget` from a validated impl payload."""
    if not isinstance(payload, dict):
        return None
    source = payload.get("source")
    target = payload.get("target")
    if not isinstance(source, str) or not isinstance(target, str):
        return None
    expose = payload.get("expose_commands") or ()
    return CapabilityToolTarget(
        source=source,
        target=target,
        expose_commands=tuple(str(c) for c in expose),
    )


def _materialize_capability(
    contract: str,
    enable_payload: dict,
    install_payload: dict,
    impl_payload: dict,
    org_primer: str = "",
) -> MaterializedCapability:
    """Build a :class:`MaterializedCapability` from validated chain payloads."""
    contract_version = enable_payload.get("contract_version")
    if contract_version is None:
        contract_version = install_payload.get("contract_version")
    impl_name = impl_payload["name"]
    return MaterializedCapability(
        contract=contract,
        contract_version=int(contract_version),
        implementation=impl_name,
        implementation_version=int(impl_payload["version"]),
        delivery_mode=str(impl_payload["delivery_mode"]),
        package_root=str(impl_payload["package_root"]),
        mount_target=_impl_mount_target(impl_name),
        required_env=tuple(str(e) for e in impl_payload.get("required_env", ())),
        required_secret_files=tuple(
            str(s) for s in impl_payload.get("required_secret_files", ())
        ),
        tool_paths=tuple(str(p) for p in impl_payload.get("tool_paths", ())),
        tool_target=_materialize_tool_target(impl_payload.get("tool_target")),
        primer_path=(impl_payload.get("primer_path") or None),
        skill_path=(impl_payload.get("skill_path") or None),
        env_bindings={
            str(k): str(v)
            for k, v in (install_payload.get("env_bindings") or {}).items()
        },
        secret_file_bindings={
            str(k): str(v)
            for k, v in (install_payload.get("secret_file_bindings") or {}).items()
        },
        workspace_overrides=(
            enable_payload.get("workspace_overrides")
            if isinstance(enable_payload.get("workspace_overrides"), dict)
            else {}
        ),
        org_primer=org_primer,
    )


def _org_capability_primers(*, org: str | None) -> dict[str, str]:
    """Return composed org guidance keyed by capability implementation.

    Rows are read only from the workspace's owning organization.  A bare
    implementation key and any ``<implementation>:<block>`` rows compose in
    the same deterministic order as workspace/org primer blocks.
    """
    if get_schema(
        ORG_CAPABILITY_PRIMER_SET_ID,
        ORG_CAPABILITY_PRIMER_REVISION,
    ) is None:
        return {}
    members = ops.read_set(
        ORG_CAPABILITY_PRIMER_SET_ID,
        org=org,
        peers=[],
        target_revision=ORG_CAPABILITY_PRIMER_REVISION,
    ).members
    grouped: dict[str, list[tuple[int, str, str]]] = {}
    for member in members:
        implementation = member.key.split(":", 1)[0]
        # Implementation ids contain a slash but not a colon. Reject malformed
        # keys rather than creating an unaddressable supplement.
        if not implementation:
            continue
        body = resolve_capability_primer_markdown(
            member.payload if isinstance(member.payload, dict) else None
        ).rstrip()
        if not body.strip():
            continue
        grouped.setdefault(implementation, []).append((
            resolve_capability_primer_order(member.payload),
            member.key,
            body,
        ))
    return {
        implementation: "\n\n".join(
            body for _, _, body in sorted(blocks, key=lambda row: (row[0], row[1]))
        )
        for implementation, blocks in grouped.items()
    }


def resolve_capabilities(
    workspace_id: str, *, org: str | None = None,
) -> tuple[MaterializedCapability, ...]:
    """Resolve every enabled capability for *workspace_id*.

    Walks the schema chain established by ``auto-0ase8``:

    1. ``autonomy.workspace.capability.enable#1`` Settings whose key is
       ``<workspace_id>:<contract>`` (workspace-scoped enables).
    2. The matching ``autonomy.org.capability.install#1`` row in the
       workspace's org (keyed by ``<contract>``) — picks the
       implementation.
    3. ``autonomy.capability.impl#1`` payload referenced by the install,
       resolved across orgs (impls are typically published from a
       sharing org and consumed everywhere).

    Returns a stable, contract-name-sorted tuple. An enable row with
    ``enabled: false`` is dropped — workspaces can opt out of an
    org-installed capability, which is the documented override behaviour
    (graph://86e04207-a25). An enabled but incomplete or version-inconsistent
    chain is omitted and logged. The shared validator still exposes the issue
    to workspace readiness, but capability configuration must never make the
    base workspace impossible to launch.
    """
    if get_schema(
        WORKSPACE_CAPABILITY_ENABLE_SET_ID,
        WORKSPACE_CAPABILITY_ENABLE_REVISION,
    ) is None:
        return ()
    enable_members = ops.read_set(
        WORKSPACE_CAPABILITY_ENABLE_SET_ID,
        org=org, peers=[], prefix=workspace_id,
    ).members
    if not enable_members:
        return ()

    install_members = (
        ops.read_set(
            ORG_CAPABILITY_INSTALL_SET_ID, org=org, peers=[],
        ).members
        if get_schema(
            ORG_CAPABILITY_INSTALL_SET_ID,
            ORG_CAPABILITY_INSTALL_REVISION,
        ) is not None
        else ()
    )
    install_by_contract: dict[str, dict] = {
        m.key: m.payload for m in install_members
    }

    impls = _read_capability_impls(org=org)
    contracts = _read_capability_contracts(org=org)
    org_primers = _org_capability_primers(org=org)
    capabilities, issues = _resolve_capabilities_from_members(
        workspace_id, enable_members, install_by_contract, contracts, impls,
        org_primers,
    )
    if issues:
        logger.warning(
            "workspace %s: omitting %d invalid enabled capability chain(s); "
            "base workspace launch remains available: %s",
            workspace_id,
            len(issues),
            "; ".join(issue.detail for issue in issues),
        )
    return capabilities


def _resolve_capabilities_from_members(
    workspace_id: str,
    enable_members: list[ResolvedSetting] | tuple[ResolvedSetting, ...],
    install_by_contract: dict[str, dict],
    contracts: dict[str, dict],
    impls: dict[str, dict],
    org_primers: dict[str, str] | None = None,
) -> tuple[tuple[MaterializedCapability, ...], tuple[CapabilityChainIssue, ...]]:
    """Evaluate one workspace from already-loaded capability Sets."""
    out: list[MaterializedCapability] = []
    broken: list[CapabilityChainIssue] = []
    prefix = f"{workspace_id}:"
    for em in enable_members:
        if not em.key.startswith(prefix):
            continue
        contract_name = em.key[len(prefix):]
        if not contract_name:
            continue
        enable_payload = em.payload
        if enable_payload.get("enabled", True) is False:
            continue
        install = install_by_contract.get(contract_name)
        impl_name = install.get("implementation") if install else None
        chain, issues = validate_capability_chain(
            contract_key=contract_name,
            enable=enable_payload,
            contract=contracts.get(contract_name),
            install=install,
            implementation=(impls.get(impl_name) if isinstance(impl_name, str) else None),
        )
        if issues:
            broken.extend(issues)
            continue
        if chain is None:
            continue
        install = dict(chain.install)
        impl_payload = dict(chain.implementation)
        out.append(_materialize_capability(
            contract_name,
            enable_payload,
            install,
            impl_payload,
            (org_primers or {}).get(impl_name, ""),
        ))
    out.sort(key=lambda c: c.contract)
    return tuple(out), tuple(broken)


def _capabilities_by_workspace(
    workspace_ids: set[str], *, org: str | None,
) -> tuple[
    dict[str, tuple[MaterializedCapability, ...]],
    dict[str, tuple[CapabilityChainIssue, ...]],
]:
    """Resolve capability chains for many workspaces once per org."""
    empty = {workspace_id: () for workspace_id in workspace_ids}
    if get_schema(
        WORKSPACE_CAPABILITY_ENABLE_SET_ID,
        WORKSPACE_CAPABILITY_ENABLE_REVISION,
    ) is None:
        return empty, dict(empty)
    enable_members = ops.read_set(
        WORKSPACE_CAPABILITY_ENABLE_SET_ID, org=org, peers=[],
    ).members
    grouped: dict[str, list[ResolvedSetting]] = {
        workspace_id: [] for workspace_id in workspace_ids
    }
    for member in enable_members:
        workspace_id, separator, _ = member.key.partition(":")
        if separator and workspace_id in grouped:
            grouped[workspace_id].append(member)
    if not any(grouped.values()):
        return empty, dict(empty)
    install_by_contract = (
        {
            member.key: member.payload
            for member in ops.read_set(
                ORG_CAPABILITY_INSTALL_SET_ID, org=org, peers=[],
            ).members
        }
        if get_schema(
            ORG_CAPABILITY_INSTALL_SET_ID,
            ORG_CAPABILITY_INSTALL_REVISION,
        ) is not None
        else {}
    )
    impls = _read_capability_impls(org=org)
    contracts = _read_capability_contracts(org=org)
    org_primers = _org_capability_primers(org=org)
    evaluated = {
        workspace_id: _resolve_capabilities_from_members(
            workspace_id, members, install_by_contract, contracts, impls,
            org_primers,
        )
        for workspace_id, members in grouped.items()
    }
    return (
        {workspace_id: result[0] for workspace_id, result in evaluated.items()},
        {workspace_id: result[1] for workspace_id, result in evaluated.items()},
    )


def _compose_workspaces(
    members: list[ResolvedSetting] | tuple[ResolvedSetting, ...],
    *,
    org: str | None,
    graph_project: str | None = None,
) -> dict[str, WorkspaceV1]:
    """Compose a workspace Set using one read per dependent Set."""
    if not members:
        return {}
    workspace_ids = {member.key for member in members}
    artifacts = _artifacts_by_workspace(workspace_ids, org=org)
    mounts = _mounts_by_workspace(workspace_ids, org=org)
    capabilities, capability_issues = _capabilities_by_workspace(
        workspace_ids, org=org,
    )
    out: dict[str, WorkspaceV1] = {}
    for member in members:
        try:
            out[member.key] = _workspace_from_setting(
                member.payload,
                workspace_id=member.key,
                graph_project=(
                    graph_project if graph_project is not None
                    else member.org or ""
                ),
                artifacts=artifacts[member.key],
                mounts=mounts[member.key],
                capabilities=capabilities[member.key],
                capability_issues=capability_issues[member.key],
            )
        except WorkspaceSettingsError as exc:
            # One malformed row darkens ITS workspace, never the fleet: a
            # straggler carrying a deleted machine-path field must fail its
            # own launch loudly, not take down load_workspaces() — which the
            # dashboard startup path and every session list depend on.
            logger.error(
                "workspace %s dropped from composition: %s", member.key, exc,
            )
    return out


def _workspaces_in_org(slug: str) -> dict[str, WorkspaceV1]:
    """Read every ``autonomy.workspace#1`` owned by *slug* + attach artifacts.

    ``peers=["personal"]`` whitelists personal.db so operators can layer
    operator-local overrides (e.g. credential env values) on top of the
    canonical workspace via standard ``graph set override --org personal``.
    Personal.db is operator-local by definition. It may own private
    workspaces directly; those rows are discovered when ``slug`` itself is
    ``personal``. For every other org, personal.db is only an override peer,
    so the auto-txg5.4 double-attribution concern (peer orgs both claiming a
    shared workspace key) doesn't apply. PEER_VISIBLE_STATES still gates the
    peer path: only published/canonical personal.db rows can contribute to a
    shared org's workspace, while raw personal-owned workspaces remain private.
    """
    members = ops.read_set(
        WORKSPACE_SET_ID, org=slug, peers=["personal"],
        target_revision=WORKSPACE_REVISION_2,
    ).members
    return _compose_workspaces(members, org=slug, graph_project=slug)


def load_workspaces() -> dict[str, WorkspaceV1]:
    """Return every visible workspace, keyed by workspace id.

    Iterates per-org DBs via :func:`tools.graph.org_ops.list_orgs` and reads
    ``autonomy.workspace#1`` from each, attaching its artifact Settings.
    Ops owns DB routing; consumers do not enumerate peers themselves.

    Process-wide cached until a dependent ``setting.changed`` event. A
    single lock protects misses so concurrent callers share one rebuild.
    """
    global _workspaces_cache_value
    with _cache_lock:
        _ensure_cache_context()
        if _workspaces_cache_value is None:
            _workspaces_cache_value = _load_workspaces_uncached()
        return _workspaces_cache_value


def _load_workspaces_uncached() -> dict[str, WorkspaceV1]:
    refs = org_ops.list_orgs()
    if not refs:
        # Pre-migration / empty-orgs fallback: read scopelessly so a
        # workspace Setting authored in the default DB is still
        # discoverable. ``org=None`` is explicit per auto-cfb8u.
        members = ops.read_set(
            WORKSPACE_SET_ID, org=None, target_revision=WORKSPACE_REVISION_2,
        ).members
        return _compose_workspaces(members, org=None)
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

    Process-wide cached until an ``autonomy.org`` changed event.
    """
    global _overrides_cache_value
    with _cache_lock:
        _ensure_cache_context()
        if _overrides_cache_value is None:
            _overrides_cache_value = _load_org_overrides_uncached()
        return _overrides_cache_value


def _load_org_overrides_uncached() -> dict[str, OrgOverride]:
    refs = org_ops.list_orgs()
    out: dict[str, OrgOverride] = {}
    if not refs:
        # Empty-orgs fallback: read scopelessly so identity Settings
        # authored in the default DB remain discoverable. ``org=None``
        # is explicit per auto-cfb8u.
        members = ops.read_set(ORG_SET_ID, org=None).members
        for m in members:
            out[m.key] = _org_override_from_payload(m.key, m.payload)
        return out
    for ref in refs:
        # ``peers=[]`` — each org publishes its own identity Setting;
        # cross-org merge here would double-register every org's row
        # from every iteration (auto-txg5.4).
        members = ops.read_set(
            ORG_SET_ID, org=ref.slug, peers=[],
        ).members
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
            ARTIFACT_PATH_SET_ID, org="personal",
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
