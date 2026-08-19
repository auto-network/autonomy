"""Shared safety controls for resolving operator data directories.

Production keeps its historical repository-local defaults. Tests and other
isolated callers can set ``AUTONOMY_REFUSE_REAL_DATA_FALLBACK=1`` to require an
explicit root; an omitted isolation variable then fails loudly at resolution
time instead of touching the operator's live ``data/`` tree.

**The volume contract (auto-lr6gu).** :data:`STORE_MANIFEST` is the
authoritative enumeration of every persistent store a running node owns:
its location inside the volume and the environment variable that roots
it. It is the single source for three things that must never drift
apart — the resolvers below, the completeness regression test, and
DEPLOY.md's volume table. Adding a store means adding a manifest row; a
store absent from the manifest is not in the contract.

Two rooting failures the manifest exists to prevent, both found by the
B1 audit and both invisible to ``test_no_hardcoded_host_paths.py``
(which looks for host-ABSOLUTE paths):

- *coincidence rooting* — a store resolved repo-relative with no
  environment override lands in the volume only because the checkout
  happens to sit at ``/app`` with the volume at ``/app/data``. It is not
  contract-rooted, and it moves silently if that layout changes.
- *split resolvers* — the same store resolved in two places, only one of
  which honours the environment variable. Rooting the deployment then
  moves one reader and not the other: a split brain rather than an error.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REFUSE_REAL_DATA_FALLBACK_ENV = "AUTONOMY_REFUSE_REAL_DATA_FALLBACK"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

#: The repository checkout and its ``data/`` directory — the historical
#: default root, and the volume mount point inside the node image.
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = REPO_ROOT / "data"


class RealDataFallbackRefused(RuntimeError):
    """An isolated caller attempted to use a repository-local data default."""


@dataclass(frozen=True)
class Store:
    """One persistent store in the node's volume contract."""

    key: str
    relative: str  # location inside the volume root
    env: Optional[str]  # the variable that roots it
    kind: str  # "db" | "dir" | "file"
    description: str  # rendered into DEPLOY.md's volume table
    #: Derived rooting: this store has no variable of its own and instead
    #: roots BESIDE the named anchor store, moving wherever the anchor's
    #: variable moves it. Still contract-rooted — one knob, two stores —
    #: never coincidence rooting.
    roots_with: Optional[str] = None

    def default(self, root: Optional[Path] = None) -> Path:
        base = Path(root) if root is not None else DEFAULT_DATA_ROOT
        return base / self.relative


#: Every store a running node reads or writes, in DEPLOY.md's render order.
STORE_MANIFEST: tuple = (
    Store("orgs", "orgs", "AUTONOMY_ORGS_DIR", "dir",
          "per-org graph DBs — identity Settings and credential rows "
          "(**the secret store**; organizations only)"),
    # The two local stores deliberately have NO environment variable of
    # their own: they root beside the orgs directory, wherever that
    # resolves, so the ONE knob (`AUTONOMY_ORGS_DIR` / the ambient root)
    # moves all three identity-bearing stores together. A second variable
    # would be a second resolver for the same data — the split-brain this
    # manifest exists to prevent — and anything that pins every store env
    # per-process (the hermetic test harness) would silently share one
    # personal store across isolation boundaries.
    Store("personal", "personal.db", None, "db",
          "the operator's own store — follows them across their fleet; "
          "not an organization, lives beside `orgs/` and roots with it",
          roots_with="orgs"),
    Store("machine", "machine.db", None, "db",
          "this machine's own store — never leaves this computer; "
          "not an organization, lives beside `orgs/` and roots with it",
          roots_with="orgs"),
    Store("graph", "graph.db", "GRAPH_DB", "db",
          "main knowledge-graph DB"),
    Store("dashboard", "dashboard.db", "DASHBOARD_DB", "db",
          "dashboard operational store"),
    Store("auth", "auth.db", "AUTH_DB", "db",
          "dashboard auth store"),
    Store("dispatch", "dispatch.db", "DISPATCH_DB", "db",
          "dispatch operational store"),
    Store("approval_requests", "approval_requests.db", "APPROVAL_REQUESTS_DB", "db",
          "approval-request store"),
    Store("commit_workflow", "commit_workflow.db", "COMMIT_WORKFLOW_DB", "db",
          "commit-workflow store"),
    Store("mission_control", "mission_control.db", "MISSION_CONTROL_DB", "db",
          "Mission Control store (missions + site revisions)"),
    Store("mcp_relay", "mcp_relay.db", "MCP_RELAY_DB", "db",
          "MCP-relay peer store (per-openai-session org bindings + crosstalk grants)"),
    Store("identity_sessions", "dashboard_identity_sessions.db",
          "DASHBOARD_IDENTITY_SESSION_DB", "db",
          "identity unlock-session store"),
    Store("pending_joins", "pending_joins.db", "AUTONOMY_PENDING_JOINS_DB", "db",
          "restart-safe invite-join progress (identifiers and counts only)"),
    Store("serving_keys", "network", "AUTONOMY_NETWORK_KEY_DIR", "dir",
          "mode-0600 auto.network tunnel-serving delegate keys"),
    Store("repl_login_key", "repl-login.key", "REPL_LOGIN_KEY_FILE", "file",
          "mode-0600 X25519 private key — the HPKE recipient for "
          "browser-sealed secure-setting provisioning"),
    Store("tls_cert", "tls.crt", "AUTONOMY_TLS_CERT", "file",
          "TLS certificate (self-signed by default)"),
    Store("tls_key", "tls.key", "AUTONOMY_TLS_KEY", "file",
          "TLS private key"),
    Store("agent_runs", "agent-runs", "DASHBOARD_AGENT_RUNS_DIR", "dir",
          "session artifacts"),
    Store("session_traces", "session-traces", "DASHBOARD_TRACE_DIR", "dir",
          "session traces"),
)

STORES_BY_KEY = {store.key: store for store in STORE_MANIFEST}


class AmbiguousDataRoot(RuntimeError):
    """``AUTONOMY_DATA_ROOT`` was set to something that cannot be an
    unambiguous base directory. Fail closed rather than guess."""


DATA_ROOT_ENV = "AUTONOMY_DATA_ROOT"


def resolve_data_root() -> Optional[Path]:
    """The ambient BASE directory for the whole volume (auto-fm4zz).

    A base DIRECTORY, never a whole-DB pin: it composes with each store's
    ``relative`` (and with the org slug via ``base/orgs/<org>.db``), so it
    has no org-discarding failure mode — the property that killed the
    retired ``GRAPH_DB``-as-root idea (see resolve_caller_db_path's
    conflict refusal, auto-23d9m). Precedence everywhere: the store's own
    variable → this base → the explicit ``root`` argument → the
    repository default under the refuse guard.

    Relative values are refused fail-closed: an ambient root that changes
    meaning with the working directory is an ambiguity, and the seam
    ruling (23d9m/fm4zz) says ambiguity refuses rather than resolves.
    """
    value = os.environ.get(DATA_ROOT_ENV)
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise AmbiguousDataRoot(
            f"{DATA_ROOT_ENV}={value!r} is not an absolute path; an ambient "
            f"base that depends on the working directory is ambiguous"
        )
    return path


#: The single effective volume base directory, imported everywhere in place
#: of a per-module repo-local ``data/`` join (auto-dnjn0). It is the ambient
#: :data:`AUTONOMY_DATA_ROOT` when that is set, else the historical
#: repository-local :data:`DEFAULT_DATA_ROOT` — so with the variable unset
#: (the case today) ``DATA_ROOT == DEFAULT_DATA_ROOT`` and no default moves.
#: Composing with :func:`resolve_data_root` keeps one env-reading path and
#: inherits its fail-closed refusal of an ambiguous relative root, rather than
#: introducing a second, laxer reader of the same variable.
DATA_ROOT = resolve_data_root() or DEFAULT_DATA_ROOT


def refuse_real_data_fallback_enabled() -> bool:
    """Return whether repository-local fallback paths must be refused."""
    value = os.environ.get(REFUSE_REAL_DATA_FALLBACK_ENV, "")
    return value.strip().lower() in _TRUE_VALUES


def _refuse(what: str, env: Optional[str]) -> RealDataFallbackRefused:
    hint = f"set {env}" if env else "pass an explicit root"
    return RealDataFallbackRefused(
        f"refusing repository data fallback for {what}: {hint} "
        f"(or unset {REFUSE_REAL_DATA_FALLBACK_ENV})"
    )


def resolve_store(key: str, *, root: Path | str | None = None) -> Path:
    """Resolve one manifest store to an absolute path.

    Precedence: the store's environment variable → *root* (the volume
    root this caller is operating on) → the repository-local default.
    Under the refuse guard that last step raises instead of silently
    touching the operator's live tree.

    The environment variable deliberately outranks *root*: a writer that
    is handed a volume root and a reader that only knows the environment
    must agree on one path, or the writer lays state down where the
    reader will not look. :func:`resolve_orgs_root` follows the same
    order, so there is one precedence rule across every store.

    Every reader of a store MUST resolve through here, so that rooting a
    deployment moves all of its readers together (no split resolvers).
    """
    try:
        store = STORES_BY_KEY[key]
    except KeyError:
        raise KeyError(f"{key!r} is not in the volume contract manifest") from None
    if store.roots_with:
        # Derived rooting: beside the ANCHOR store, wherever the anchor's
        # own resolution puts it (its env variable, the ambient base, the
        # caller's volume root, or the default — in that order). The same
        # rule tools.graph.db applies for the local stores, stated once in
        # the contract so a rooted deployment moves anchor and dependent
        # together.
        anchor = resolve_store(store.roots_with, root=root)
        return anchor.parent / store.relative
    if store.env:
        env_value = os.environ.get(store.env)
        if env_value:
            return Path(env_value)
    ambient = resolve_data_root()
    if ambient is not None:
        # The ambient base outranks the *root* argument for the same
        # writer/reader-convergence reason the store variable outranks
        # both: a reader that only knows the environment must agree with
        # a writer that was handed a root.
        return store.default(ambient)
    if root is not None:
        return store.default(root)
    if refuse_real_data_fallback_enabled():
        raise _refuse(store.relative, store.env)
    return store.default()


def resolve_orgs_root(
    root: Path | str | None,
    *,
    default: Path,
) -> Path:
    """Resolve the organization DB root — the identity and secret store.

    Same precedence as every other store (:func:`resolve_store`):
    ``AUTONOMY_ORGS_DIR`` → *root* (the deployment root a caller is
    operating on) → the repository-local default. The variable outranks
    *root* so a writer handed a deployment root and a reader that knows
    only the variable converge on one path; for the orgs store that
    disagreement would put identity and credential rows where readers do
    not look, which is the worst version of the bug.

    An operator naming this directory EXPLICITLY (a ``--orgs-dir`` flag)
    should not be overridden by the environment — those call sites use
    their flag directly and reach here only when it is absent.

    The refuse-fallback guard is unchanged: an unrooted resolution still
    raises rather than touching the operator's live tree.
    """
    env = os.environ.get("AUTONOMY_ORGS_DIR")
    if env:
        return Path(env)
    ambient = resolve_data_root()
    if ambient is not None:
        return ambient / "orgs"
    if root is not None:
        return Path(root)
    if refuse_real_data_fallback_enabled():
        raise _refuse("data/orgs", "AUTONOMY_ORGS_DIR")
    return default
