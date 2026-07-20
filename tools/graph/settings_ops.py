"""Settings primitive — ops layer.

Read, write, resolve, and migrate operations for the ``settings`` table.
Imported into ``tools.graph.ops`` for unified discovery; tests may reach in
here directly. Spec: graph://0d3f750f-f9c. Cross-org rules:
graph://bcce359d-a1d.

Public API contract (auto-cfb8u): every function below takes ``org`` as
a **required keyword-only** argument. There is no default. Forgetting
``org=`` is a ``TypeError`` at call time, not a silent route to the
scopeless default DB. Pass:

* a non-empty org slug — route to that org's DB (the common case);
* :data:`CALLER_ORG` — opt into the env-cascade resolver (per-request
  contextvar → ``GRAPH_ORG`` env → scopeless default). Used by CLIs and
  env-driven test contexts that legitimately want today's behavior;
* ``None`` — explicit "scopeless default DB" (rare; loud).

Background: dashboard handlers were silently writing to the scopeless DB
when the writer forgot ``org=`` while the reader carried ``X-Graph-Org``
— the row appeared to vanish (graph://53f7412f-51e). Required-org makes
that intent explicit at the API boundary.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass, field, asdict, replace
from functools import wraps
from typing import Any, Callable, Generic, Iterator, TypeVar
from uuid import uuid4

from .db import GraphDB, resolve_caller_db_path
from . import schemas


logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────


VALID_STATES = ("raw", "curated", "published", "canonical")
PRECEDENCE = {"canonical": 0, "published": 1, "curated": 2, "raw": 3}
PEER_VISIBLE_STATES = ("published", "canonical")


# ── protected identity sets (dashboard-access credentials) ───
#
# These set IDs store the human dashboard-access credentials that the
# unlock gate (tools/dashboard/unlock_routes.py) enforces on. Mutating
# them from the GENERIC settings surface (POST /api/graph/setting and the
# override/exclude/promote/deprecate/delete/migrate routes, all of which
# land here) would defeat the gate outright — an attacker with only
# network reach to the still-open /api surface could:
#   * INSERT their own passkey row, then assert against it → session;
#   * INSERT a personal-identity row with a low-sorting key to SHADOW the
#     operator's, then password-unlock against their own root;
#   * DELETE/exclude the operator's enrollment → the gate reads "nothing
#     enrolled" → fail-OPEN.
# So every mutation path below refuses these set IDs UNLESS the caller is
# the trusted identity/unlock route, which brackets its write in
# :func:`identity_write_context`. This is the C1 I1 lesson applied: the
# guard lives at the one data layer every write funnels through, so no
# individual route can forget it. Reads are unaffected (the gate must be
# able to read enrollment); only mutations are gated.

import contextvars as _contextvars

PROTECTED_IDENTITY_SET_IDS = frozenset({
    "autonomy.identity.personal",
    "autonomy.identity.passkey",
})


class ProtectedSettingError(PermissionError):
    """A generic-settings mutation targeted a protected identity set
    without the internal identity-route capability. Surfaces as 403."""


_identity_write_allowed: "_contextvars.ContextVar[bool]" = _contextvars.ContextVar(
    "settings_identity_write_allowed", default=False,
)


class _IdentityWriteContext:
    """Context manager AND decorator granting the identity-route
    capability to mutate the protected identity sets. Only
    tools/dashboard/identity_routes.py and unlock_routes.py use it."""

    def __enter__(self):
        self._token = _identity_write_allowed.set(True)
        return self

    def __exit__(self, *exc):
        _identity_write_allowed.reset(self._token)
        return False


def identity_write_context() -> _IdentityWriteContext:
    """Grant the current context permission to write the protected
    identity sets (:data:`PROTECTED_IDENTITY_SET_IDS`). The trusted
    identity/unlock routes bracket their ``upsert_by_key`` call in this;
    the generic settings API never does, so it stays refused."""
    return _IdentityWriteContext()


def _guard_protected_set(set_id: str | None) -> None:
    """Refuse a mutation of a protected identity set unless the caller
    carries the identity-route capability."""
    if set_id in PROTECTED_IDENTITY_SET_IDS and not _identity_write_allowed.get():
        raise ProtectedSettingError(
            f"{set_id!r} is a protected dashboard-identity set; it can only "
            "be mutated through the identity/unlock routes, not the generic "
            "settings API"
        )


def _guard_protected_setting_id(setting_id: str, org: str | None) -> None:
    """Guard an id-addressed mutation: resolve the target row's set_id and
    refuse if it is a protected identity set (unless capability-carrying).
    A missing row is left to the function's own not-found handling."""
    if _identity_write_allowed.get():
        return
    db = _open(org)
    try:
        row = db.conn.execute(
            "SELECT set_id FROM settings WHERE id = ?", (setting_id,)
        ).fetchone()
    finally:
        db.close()
    if row is not None:
        _guard_protected_set(row["set_id"])


# ── org= argument contract (auto-cfb8u) ──────────────────────


class _CallerOrgSentinel:
    """Sentinel marker for "resolve org via the caller cascade."

    Pass :data:`CALLER_ORG` as ``org=`` to opt into the env-driven
    resolver — per-request contextvar → ``GRAPH_ORG`` env → scopeless
    default. Used by CLIs, env-pinned tests, and any other process-context
    caller. A literal ``None`` is treated as "scopeless explicit"; the
    sentinel is the only path that consults the cascade.
    """

    _instance: "_CallerOrgSentinel | None" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<settings_ops.CALLER_ORG>"


CALLER_ORG = _CallerOrgSentinel()


def _resolve_org_arg(org: "str | None | _CallerOrgSentinel") -> str | None:
    """Collapse a public ``org=`` argument to a literal slug-or-None.

    * :data:`CALLER_ORG` → run :func:`_resolve_settings_caller`
      (contextvar → ``GRAPH_ORG`` env → ``None``).
    * Any other value (a string, or ``None``) is returned as-is.

    The point of separating "literal None" from "go consult the cascade"
    is to make the dashboard foot-gun (graph://53f7412f-51e) impossible:
    a writer that lands in the scopeless DB while the reader carries
    ``X-Graph-Org`` is now either a typo (None passed where the slug was
    intended) or a deliberate choice — never silent.
    """
    if isinstance(org, _CallerOrgSentinel):
        return _resolve_settings_caller(None)
    return org


# ── Emit hook (commit-then-emit) ─────────────────────────────


# Registered by the dashboard at lifespan startup so every Settings mutation
# — CLI, dashboard route, plugin, test fixture, future caller — fires a
# uniform ``setting.changed`` notification. CLI / test processes that don't
# import the dashboard register no hook; their writes commit normally and
# the hook is a no-op.
#
# Hook signature::
#
#     def hook(*, operation: str, snapshot: dict, org: str | None) -> None
#
# ``operation`` is one of: write, override, exclude, promote, deprecate,
# delete, migrate. ``snapshot`` is metadata-only: ``set_id``,
# ``schema_revision``, ``key``, ``publication_state``, ``deprecated``.
# Subscribers re-resolve via :func:`read_set` if they need payload data.
#
# Mutators call the hook AFTER ``db.conn.commit()`` returns — race-free
# observability for subscribers that resolve on receipt. Hooks are
# best-effort: exceptions are caught and logged; the mutator never blocks
# on hook completion.
_emit_hook: Callable[..., None] | None = None


def set_emit_hook(hook: Callable[..., None] | None) -> None:
    """Register (or clear with ``None``) the post-commit emit hook.

    See module-level commentary above the ``_emit_hook`` definition for
    the contract. Pattern mirrors the action-registry's services
    injection — the dashboard wires this at lifespan startup; CLI / test
    contexts that don't import the dashboard leave it unregistered.
    """
    global _emit_hook
    _emit_hook = hook


def _make_snapshot(
    set_id: str,
    schema_revision: int,
    key: str,
    publication_state: str,
    deprecated: bool,
) -> dict:
    return {
        "set_id": set_id,
        "schema_revision": int(schema_revision),
        "key": key,
        "publication_state": publication_state,
        "deprecated": bool(deprecated),
    }


def _call_emit_hook(
    *,
    operation: str,
    snapshot: dict,
    org: str | None,
) -> None:
    """Invoke the registered hook (best-effort).

    Mutators call this AFTER their transaction commits and the DB
    handle is closed, so a subscriber that re-resolves via ``read_set``
    on receipt sees the new row. Exceptions are swallowed at WARN — a
    broken subscriber must not corrupt write semantics.
    """
    hook = _emit_hook
    if hook is None:
        return
    try:
        hook(operation=operation, snapshot=snapshot, org=org)
    except Exception:
        logger.warning(
            "settings_ops emit hook raised on %s for set_id=%s key=%s",
            operation, snapshot.get("set_id"), snapshot.get("key"),
            exc_info=True,
        )


# ── Throughput stats ────────────────────────────────────────


_DEFAULT_SCOPE_LABEL = "(default)"
_STATS_BURST_WINDOW_SECONDS = 10
_STATS_RECENT_WINDOW_SECONDS = 60
_STATS_RETENTION_SECONDS = 300
_STATS_TOP_LIMIT = 10


@dataclass
class _SettingsStatsRollup:
    calls: int = 0
    reads: int = 0
    writes: int = 0
    errors: int = 0
    result_count: int = 0
    total_duration_ms: float = 0.0
    latency_ms: Counter[int] = field(default_factory=Counter)
    operations: Counter[str] = field(default_factory=Counter)
    set_ids: Counter[str] = field(default_factory=Counter)
    set_reads: Counter[str] = field(default_factory=Counter)
    set_writes: Counter[str] = field(default_factory=Counter)
    set_upserts: Counter[str] = field(default_factory=Counter)
    set_org_calls: Counter[tuple[str, str]] = field(default_factory=Counter)
    set_org_reads: Counter[tuple[str, str]] = field(default_factory=Counter)
    set_org_writes: Counter[tuple[str, str]] = field(default_factory=Counter)
    set_org_upserts: Counter[tuple[str, str]] = field(default_factory=Counter)
    orgs: Counter[str] = field(default_factory=Counter)


@dataclass
class _SettingsStatsBucket(_SettingsStatsRollup):
    second: int = 0


class SettingsApiStats:
    """Cheap process-local counters for Settings throughput visibility."""

    burst_window_seconds = _STATS_BURST_WINDOW_SECONDS
    recent_window_seconds = _STATS_RECENT_WINDOW_SECONDS
    retention_seconds = _STATS_RETENTION_SECONDS

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        now = time.time()
        with self._lock:
            self._started_at = now
            self._totals = _SettingsStatsRollup()
            self._buckets: dict[int, _SettingsStatsBucket] = {}
            self._last_call: dict[str, Any] | None = None
            self._last_error: dict[str, Any] | None = None

    def record(
        self,
        *,
        operation: str,
        set_id: str | None,
        org: str | None,
        kind: str,
        ok: bool,
        duration_ms: float,
        result_count: int = 0,
    ) -> None:
        now = time.time()
        second = int(now)
        scope = self._scope_label(org)
        count = max(int(result_count), 0)
        call = {
            "ts": now,
            "operation": operation,
            "set_id": set_id,
            "org": scope,
            "kind": kind,
            "ok": bool(ok),
            "duration_ms": round(float(duration_ms), 3),
            "result_count": count,
        }
        with self._lock:
            self._sweep_locked(now)
            bucket = self._buckets.get(second)
            if bucket is None:
                bucket = _SettingsStatsBucket(second=second)
                self._buckets[second] = bucket
            self._apply_rollup(
                self._totals,
                operation=operation,
                set_id=set_id,
                org=scope,
                kind=kind,
                ok=ok,
                duration_ms=duration_ms,
                result_count=count,
            )
            self._apply_rollup(
                bucket,
                operation=operation,
                set_id=set_id,
                org=scope,
                kind=kind,
                ok=ok,
                duration_ms=duration_ms,
                result_count=count,
            )
            self._last_call = call
            if not ok:
                self._last_error = dict(call)

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._sweep_locked(now)
            burst = self._aggregate_locked(self.burst_window_seconds, now)
            recent = self._aggregate_locked(self.recent_window_seconds, now)
            return {
                "started_at": self._started_at,
                "now": now,
                "burst_window_seconds": self.burst_window_seconds,
                "recent_window_seconds": self.recent_window_seconds,
                "retention_seconds": self.retention_seconds,
                "last_call": dict(self._last_call) if self._last_call else None,
                "last_error": dict(self._last_error) if self._last_error else None,
                "totals": self._render_rollup(self._totals, window_seconds=None),
                f"last_{self.burst_window_seconds}s": self._render_rollup(
                    burst, window_seconds=self.burst_window_seconds,
                ),
                f"last_{self.recent_window_seconds}s": self._render_rollup(
                    recent, window_seconds=self.recent_window_seconds,
                ),
            }

    def set_metrics_snapshot(self, *, org: str | None = None) -> dict[str, Any]:
        now = time.time()
        scope = self._scope_label(org) if org is not None else None
        with self._lock:
            self._sweep_locked(now)
            burst = self._aggregate_locked(self.burst_window_seconds, now)
            recent = self._aggregate_locked(self.recent_window_seconds, now)
            return {
                "totals": self._render_set_metrics(self._totals, scope=scope),
                f"last_{self.burst_window_seconds}s": self._render_set_metrics(
                    burst, scope=scope,
                ),
                f"last_{self.recent_window_seconds}s": self._render_set_metrics(
                    recent, scope=scope,
                ),
            }

    @staticmethod
    def _scope_label(org: str | None) -> str:
        return str(org) if org else _DEFAULT_SCOPE_LABEL

    @staticmethod
    def _apply_rollup(
        rollup: _SettingsStatsRollup,
        *,
        operation: str,
        set_id: str | None,
        org: str,
        kind: str,
        ok: bool,
        duration_ms: float,
        result_count: int,
    ) -> None:
        rounded_duration_ms = max(int(round(duration_ms)), 0)
        rollup.calls += 1
        if kind == "read":
            rollup.reads += 1
        elif kind == "write":
            rollup.writes += 1
        if not ok:
            rollup.errors += 1
        rollup.result_count += result_count
        rollup.total_duration_ms += float(duration_ms)
        rollup.latency_ms[rounded_duration_ms] += 1
        rollup.operations[operation] += 1
        rollup.orgs[org] += 1
        if set_id:
            rollup.set_ids[set_id] += 1
            rollup.set_org_calls[(org, set_id)] += 1
            if kind == "read":
                rollup.set_reads[set_id] += 1
                rollup.set_org_reads[(org, set_id)] += 1
            elif kind == "write":
                rollup.set_writes[set_id] += 1
                rollup.set_org_writes[(org, set_id)] += 1
                if operation == "upsert_by_key":
                    rollup.set_upserts[set_id] += 1
                    rollup.set_org_upserts[(org, set_id)] += 1

    def _sweep_locked(self, now: float) -> None:
        cutoff = now - self.retention_seconds
        stale = [second for second in self._buckets if second < cutoff]
        for second in stale:
            self._buckets.pop(second, None)

    def _aggregate_locked(
        self, window_seconds: int, now: float,
    ) -> _SettingsStatsRollup:
        out = _SettingsStatsRollup()
        for second, bucket in self._buckets.items():
            if now - second >= window_seconds:
                continue
            out.calls += bucket.calls
            out.reads += bucket.reads
            out.writes += bucket.writes
            out.errors += bucket.errors
            out.result_count += bucket.result_count
            out.total_duration_ms += bucket.total_duration_ms
            out.latency_ms.update(bucket.latency_ms)
            out.operations.update(bucket.operations)
            out.set_ids.update(bucket.set_ids)
            out.set_reads.update(bucket.set_reads)
            out.set_writes.update(bucket.set_writes)
            out.set_upserts.update(bucket.set_upserts)
            out.set_org_calls.update(bucket.set_org_calls)
            out.set_org_reads.update(bucket.set_org_reads)
            out.set_org_writes.update(bucket.set_org_writes)
            out.set_org_upserts.update(bucket.set_org_upserts)
            out.orgs.update(bucket.orgs)
        return out

    @staticmethod
    def _percentile_ms(
        latency_ms: Counter[int], total_calls: int, percentile: float,
    ) -> int | None:
        if total_calls <= 0:
            return None
        threshold = max(int(math.ceil(total_calls * percentile)), 1)
        seen = 0
        for duration_ms in sorted(latency_ms):
            seen += latency_ms[duration_ms]
            if seen >= threshold:
                return duration_ms
        return max(latency_ms) if latency_ms else None

    def _render_rollup(
        self,
        rollup: _SettingsStatsRollup,
        *,
        window_seconds: int | None,
    ) -> dict[str, Any]:
        calls = rollup.calls
        total_ms = round(rollup.total_duration_ms, 3)
        out = {
            "calls": calls,
            "reads": rollup.reads,
            "writes": rollup.writes,
            "errors": rollup.errors,
            "result_count": rollup.result_count,
            "total_duration_ms": total_ms,
            "avg_duration_ms": round(
                (rollup.total_duration_ms / calls), 3,
            ) if calls else 0.0,
            "latency_ms": {
                "p50": self._percentile_ms(rollup.latency_ms, calls, 0.50),
                "p95": self._percentile_ms(rollup.latency_ms, calls, 0.95),
                "p99": self._percentile_ms(rollup.latency_ms, calls, 0.99),
            },
            "operations": {
                key: rollup.operations[key]
                for key in sorted(rollup.operations)
            },
            "top_sets": [
                {
                    "set_id": set_id,
                    "calls": count,
                    "reads": rollup.set_reads.get(set_id, 0),
                    "writes": rollup.set_writes.get(set_id, 0),
                    "upserts": rollup.set_upserts.get(set_id, 0),
                }
                for set_id, count in rollup.set_ids.most_common(
                    _STATS_TOP_LIMIT,
                )
            ],
            "top_orgs": [
                {"org": org, "calls": count}
                for org, count in rollup.orgs.most_common(_STATS_TOP_LIMIT)
            ],
        }
        if window_seconds is not None:
            out["calls_per_second"] = round(
                calls / float(window_seconds), 3,
            )
        return out

    @staticmethod
    def _render_set_metrics(
        rollup: _SettingsStatsRollup,
        *,
        scope: str | None,
    ) -> dict[str, dict[str, int]]:
        if scope is None:
            set_ids = sorted(rollup.set_ids)
            return {
                set_id: {
                    "calls": rollup.set_ids.get(set_id, 0),
                    "reads": rollup.set_reads.get(set_id, 0),
                    "writes": rollup.set_writes.get(set_id, 0),
                    "upserts": rollup.set_upserts.get(set_id, 0),
                }
                for set_id in set_ids
            }
        set_ids = sorted({
            set_id for (org_name, set_id) in rollup.set_org_calls
            if org_name == scope
        })
        return {
            set_id: {
                "calls": rollup.set_org_calls.get((scope, set_id), 0),
                "reads": rollup.set_org_reads.get((scope, set_id), 0),
                "writes": rollup.set_org_writes.get((scope, set_id), 0),
                "upserts": rollup.set_org_upserts.get((scope, set_id), 0),
            }
            for set_id in set_ids
        }


_SETTINGS_API_STATS = SettingsApiStats()


def settings_api_stats_snapshot() -> dict[str, Any]:
    return _SETTINGS_API_STATS.snapshot()


def settings_api_set_metrics_snapshot(
    *,
    org: str | None = None,
) -> dict[str, Any]:
    return _SETTINGS_API_STATS.set_metrics_snapshot(org=org)


def reset_settings_api_stats() -> None:
    _SETTINGS_API_STATS.reset()


def _record_settings_api_call(
    *,
    operation: str,
    set_id: str | None,
    org: str | None,
    kind: str,
    ok: bool,
    start: float,
    result_count: int = 0,
) -> None:
    _SETTINGS_API_STATS.record(
        operation=operation,
        set_id=set_id,
        org=org,
        kind=kind,
        ok=ok,
        duration_ms=(time.perf_counter() - start) * 1000.0,
        result_count=result_count,
    )


def _stats_result_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, list):
        return len(value)
    members = getattr(value, "members", None)
    if isinstance(members, list):
        return len(members)
    if isinstance(value, dict):
        layers = value.get("layers")
        if isinstance(layers, list):
            return len(layers)
    return 1


# ── Result types ─────────────────────────────────────────────


T = TypeVar("T")


@dataclass
class ResolvedSetting(Generic[T]):
    """A Setting after resolution.

    ``target_revision`` is ``None`` when returned at its stored revision
    (the default); otherwise it carries the revision the payload was
    reshaped to. ``upconverted`` records whether the payload was rewritten
    by the upconvert chain (True) or returned at its stored shape (False).
    ``org`` is the originating DB's org slug — ``None`` in single-DB mode.

    ``payload`` is typed on the ``T`` parameter: a bare ``dict`` when no
    ``model=`` was supplied to :func:`read_set` / :func:`get_setting`; a
    validated Pydantic (or compatible) instance when a model was supplied.
    """
    id: str
    set_id: str
    stored_revision: int
    key: str
    payload: T
    state: str
    supersedes: str | None
    excludes: str | None
    deprecated: bool
    successor_id: str | None
    created_at: str
    updated_at: str
    target_revision: int | None = None
    org: str | None = None
    upconverted: bool = False

    def to_dict(self) -> dict:
        """Serialize the Setting as a dict (payload included as-is)."""
        d = asdict(self)
        return d


@dataclass
class DropAccounting:
    """Per-query row-drop counts from :func:`read_set` / :func:`get_setting`.

    Every field is a non-negative count of rows eliminated during
    resolution. ``schema_invalid`` covers the ``model=`` validation path —
    rows whose stored payload fails ``model_validate`` are dropped and
    logged at WARN rather than crashing the query.
    """
    below_min_revision: int = 0
    no_upconvert_path: int = 0
    above_target_no_downgrade: int = 0
    schema_invalid: int = 0
    deprecated_filtered: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    def values(self):
        """Back-compat: callers that tested ``any(dropped.values())``."""
        return self.to_dict().values()

    def items(self):
        """Back-compat: callers that iterated ``dict(dropped).items()``."""
        return self.to_dict().items()

    def __iter__(self):
        return iter(self.to_dict())

    def __getitem__(self, key: str) -> int:
        """Back-compat: callers that accessed ``dropped["below_min_revision"]``."""
        return getattr(self, key)


@dataclass
class SetMembers(Generic[T]):
    """Result of :func:`read_set`: resolved members + drop accounting.

    Iterable and sized over the resolved members; :meth:`to_dict` returns
    a ``{setting.key: ResolvedSetting}`` mapping since supersedes
    resolution has already deduped to one winner per key. Use
    :meth:`as_payload` for the JSON-serializable shape expected by the
    dashboard Settings API.
    """
    members: list[ResolvedSetting[T]]
    dropped: DropAccounting = field(default_factory=DropAccounting)

    def __iter__(self) -> Iterator[ResolvedSetting[T]]:
        return iter(self.members)

    def __len__(self) -> int:
        return len(self.members)

    def to_dict(self) -> dict[str, ResolvedSetting[T]]:
        """Map keyed by resolved ``Setting.key``. Supersedes resolution has
        already deduped — one winning Setting per key.
        """
        return {rs.key: rs for rs in self.members}

    def as_payload(self) -> dict:
        """JSON-serializable shape: ``{"members": [...], "dropped": {...}}``.

        Used by the dashboard Settings API endpoints that send the full
        query result over the wire. ``to_dict()`` is reserved for the
        key-indexed consumer view.
        """
        return {
            "members": [_serialize_member(m) for m in self.members],
            "dropped": self.dropped.to_dict(),
        }


def _serialize_member(m: ResolvedSetting) -> dict:
    """Render a :class:`ResolvedSetting` as a JSON-friendly dict.

    Pydantic payloads are dumped via ``model_dump``; plain dicts pass
    through unchanged.
    """
    d = dict(m.__dict__)
    payload = d.get("payload")
    if hasattr(payload, "model_dump"):
        d["payload"] = payload.model_dump()
    return d


@dataclass
class MigrationReport:
    """Result of ``migrate_setting_revisions``."""
    set_id: str
    to_revision: int
    dry_run: bool
    rewrote: int = 0
    no_upconvert_path: int = 0
    already_at_target: int = 0
    above_target: int = 0
    affected_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ── DB selection (mirrors ops._open) ─────────────────────────


def _db_path(org: str | None) -> str | None:
    """Resolve Settings DB path for a literal ``org`` value.

    ``org`` is the post-:func:`_resolve_org_arg` value: a slug (route to
    that org's DB) or ``None`` (scopeless default). No env-cascade here —
    public callers pre-resolve via :data:`CALLER_ORG` if they want it.
    ``GRAPH_DB`` env pins the path regardless (test override).
    """
    env_db = os.environ.get("GRAPH_DB")
    if env_db:
        return env_db
    return str(resolve_caller_db_path(org))


def _open(org: str | None) -> GraphDB:
    return GraphDB(_db_path(org))


# ── JSON merge-patch (RFC 7396) ──────────────────────────────


def json_merge_patch(target: Any, patch: Any) -> Any:
    """Apply RFC 7396 merge-patch.

    - If ``patch`` is not a dict, return it (replace).
    - For dict patches: per key, remove on null; recurse for dict-on-dict;
      otherwise replace.
    - Lists are replaced wholesale, never element-merged.
    """
    if not isinstance(patch, dict):
        return patch
    if not isinstance(target, dict):
        target = {}
    out = dict(target)
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = json_merge_patch(out[k], v)
        else:
            out[k] = v
    return out


# ── Row → ResolvedSetting ────────────────────────────────────


def _row_to_resolved(row, *, org: str | None = None,
                     target_revision: int | None = None) -> ResolvedSetting[dict]:
    payload = row["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            payload = {}
    return ResolvedSetting(
        id=row["id"],
        set_id=row["set_id"],
        stored_revision=int(row["schema_revision"]),
        key=row["key"],
        payload=payload,
        state=row["publication_state"],
        supersedes=row["supersedes"],
        excludes=row["excludes"],
        deprecated=bool(row["deprecated"]),
        successor_id=row["successor_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        target_revision=target_revision,
        org=org,
        upconverted=False,
    )


def _apply_model(
    resolved: ResolvedSetting[dict],
    model: type[Any],
    dropped: DropAccounting,
) -> ResolvedSetting[Any] | None:
    """Validate ``resolved.payload`` against *model* and retype the payload.

    *model* is any class exposing a ``model_validate(data) -> instance``
    classmethod — Pydantic ``BaseModel`` subclasses satisfy this. On
    validation failure the row is dropped: ``dropped.schema_invalid`` is
    incremented, a WARN is logged, and ``None`` is returned so the caller
    can skip the row without crashing the query.
    """
    validator = getattr(model, "model_validate", None)
    if validator is None:
        raise TypeError(
            f"model={model!r} has no model_validate() classmethod — "
            f"pass a Pydantic BaseModel (or compatible) class"
        )
    try:
        typed_payload = validator(resolved.payload)
    except Exception as exc:
        dropped.schema_invalid += 1
        logger.warning(
            "read_set(%s, model=%s): payload validation failed for "
            "setting id=%s key=%r: %s",
            resolved.set_id, model.__name__, resolved.id, resolved.key, exc,
        )
        return None
    return replace(resolved, payload=typed_payload)


# ── Write paths ──────────────────────────────────────────────


def add_setting(
    set_id: str,
    schema_revision: int,
    key: str,
    payload: dict,
    *,
    org: "str | None | _CallerOrgSentinel",
    state: str = "raw",
) -> str:
    """Create a base Setting in org's DB.

    ``org`` is **required**: pass an org slug for that org's DB,
    :data:`CALLER_ORG` for the env-cascade resolver, or ``None`` for an
    explicit scopeless write. Forgetting ``org=`` is a ``TypeError`` —
    see module docstring.

    Validates payload against ``(set_id, schema_revision)``. Returns the new
    Setting id. Raises ``schemas.SchemaValidationError`` on validation
    failure, ``ValueError`` on bad ``state``.
    """
    org = _resolve_org_arg(org)
    _guard_protected_set(set_id)
    if state not in VALID_STATES:
        raise ValueError(f"invalid state {state!r}; valid: {VALID_STATES}")
    schemas.validate_payload(set_id, schema_revision, payload)
    sid = str(uuid4())
    now = _now_iso()
    expires_at = schemas.cache_expires_at(set_id, int(schema_revision), now)
    db = _open(org)
    try:
        db.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
            "publication_state, created_at, updated_at, expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (sid, set_id, int(schema_revision), key, json.dumps(payload),
             state, now, now, expires_at),
        )
        db.conn.commit()
    finally:
        db.close()
    _call_emit_hook(
        operation="write",
        snapshot=_make_snapshot(set_id, schema_revision, key, state, False),
        org=org,
    )
    return sid


def upsert_by_key(
    set_id: str,
    schema_revision: int,
    key: str,
    payload: dict,
    *,
    org: "str | None | _CallerOrgSentinel",
    state: str = "raw",
) -> str:
    """Atomic single-tx UPDATE-or-INSERT at ``(set_id, schema_revision, key)``.

    Closes substrate gap #2 (``graph://dff97eec-c59``): callers that want
    a single, evolving base row per composite key get one — no
    ``read_set`` / ``add_setting`` race window, no append-by-default
    surprise. The returned setting id is stable across subsequent
    upserts of the same key, so callers can hold ids long-term.

    Algorithm (one transaction, ``BEGIN IMMEDIATE``):
        1. validate ``payload`` against the registered schema,
        2. ``SELECT`` the newest base row (``supersedes IS NULL AND
           excludes IS NULL``) for ``(set_id, schema_revision, key)``,
        3. INSERT a fresh row when none exists, otherwise UPDATE that
           row in place (id, ``created_at`` preserved),
        4. restamp ``expires_at`` from :func:`schemas.cache_expires_at`
           so ``@cache``-decorated schemas keep sliding-window TTLs,
        5. fire exactly one ``set_emit_hook`` callback after commit
           (``operation="write"`` — same shape :func:`add_setting`
           emits today).

    Legacy duplicates (multiple base rows for the same composite key
    left behind by older :func:`add_setting` callers) are tolerated:
    the newest by ``created_at`` wins and is updated in place — no
    error, no cleanup. Override (``supersedes``) and exclude
    (``excludes``) rows are deliberately ignored: they belong to a
    separate layer and are not the "writable base."

    Concurrency: ``BEGIN IMMEDIATE`` acquires SQLite's reserved-write
    lock before the SELECT, so two writers in the same process serialize
    cleanly — one blocks until the other commits, then sees the row it
    just wrote.

    Returns the upserted row's setting id (new on insert, preserved on
    update). Raises :class:`schemas.SchemaValidationError` on bad
    payload or unknown schema; ``ValueError`` on bad ``state``.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    org = _resolve_org_arg(org)
    _guard_protected_set(set_id)
    if state not in VALID_STATES:
        raise ValueError(f"invalid state {state!r}; valid: {VALID_STATES}")
    schemas.validate_payload(set_id, schema_revision, payload)
    now = _now_iso()
    expires_at = schemas.cache_expires_at(set_id, int(schema_revision), now)
    payload_json = json.dumps(payload)
    db = _open(org)
    try:
        db.conn.execute("BEGIN IMMEDIATE")
        existing = db.conn.execute(
            "SELECT id FROM settings "
            "WHERE set_id = ? AND schema_revision = ? AND key = ? "
            "  AND supersedes IS NULL AND excludes IS NULL "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (set_id, int(schema_revision), key),
        ).fetchone()
        if existing is None:
            sid = str(uuid4())
            db.conn.execute(
                "INSERT INTO settings(id, set_id, schema_revision, key, "
                "payload, publication_state, created_at, updated_at, "
                "expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (sid, set_id, int(schema_revision), key, payload_json,
                 state, now, now, expires_at),
            )
        else:
            sid = existing["id"]
            db.conn.execute(
                "UPDATE settings SET payload = ?, publication_state = ?, "
                "updated_at = ?, expires_at = ? WHERE id = ?",
                (payload_json, state, now, expires_at, sid),
            )
        db.conn.commit()
    finally:
        db.close()
    _call_emit_hook(
        operation="write",
        snapshot=_make_snapshot(set_id, schema_revision, key, state, False),
        org=org,
    )
    return sid


def override_setting(
    target_id: str,
    payload_overrides: dict,
    *,
    org: "str | None | _CallerOrgSentinel",
    state: str = "raw",
) -> str:
    """Create a Setting with ``supersedes=target_id`` and partial payload.

    Validation runs against the target's ``(set_id, schema_revision)``
    using the *merged* shape — what consumers will actually see. The
    override Setting itself lives in org's DB; the *target* may
    be either own-org or peer-origin — overriding peer content is the
    expected way to adapt shared primitives to a local org. Raises
    ``LookupError`` only when the target exists nowhere (own or peers).

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    org = _resolve_org_arg(org)
    _guard_protected_setting_id(target_id, org)
    if state not in VALID_STATES:
        raise ValueError(f"invalid state {state!r}; valid: {VALID_STATES}")
    target = _fetch_setting_any_org(target_id, org)
    if target is None:
        raise LookupError(f"override target not found: {target_id!r}")
    db = _open(org)
    try:
        target_payload = json.loads(target["payload"])
        merged = json_merge_patch(target_payload, payload_overrides)
        schemas.validate_payload(
            target["set_id"], int(target["schema_revision"]), merged,
        )
        sid = str(uuid4())
        now = _now_iso()
        expires_at = schemas.cache_expires_at(
            target["set_id"], int(target["schema_revision"]), now,
        )
        db.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
            "publication_state, supersedes, created_at, updated_at, "
            "expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (sid, target["set_id"], int(target["schema_revision"]),
             target["key"], json.dumps(payload_overrides),
             state, target_id, now, now, expires_at),
        )
        db.conn.commit()
    finally:
        db.close()
    _call_emit_hook(
        operation="override",
        snapshot=_make_snapshot(
            target["set_id"], int(target["schema_revision"]),
            target["key"], state, False,
        ),
        org=org,
    )
    return sid


def exclude_setting(
    target_id: str,
    *,
    org: "str | None | _CallerOrgSentinel",
    state: str = "raw",
) -> str:
    """Create a Setting with ``excludes=target_id`` and empty payload.

    The exclude row itself lives in org's DB and only affects
    reads scoped to this caller — so peer-origin targets are allowed.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    org = _resolve_org_arg(org)
    _guard_protected_setting_id(target_id, org)
    if state not in VALID_STATES:
        raise ValueError(f"invalid state {state!r}; valid: {VALID_STATES}")
    target = _fetch_setting_any_org(target_id, org)
    if target is None:
        raise LookupError(f"exclude target not found: {target_id!r}")
    db = _open(org)
    try:
        sid = str(uuid4())
        now = _now_iso()
        expires_at = schemas.cache_expires_at(
            target["set_id"], int(target["schema_revision"]), now,
        )
        db.conn.execute(
            "INSERT INTO settings(id, set_id, schema_revision, key, payload, "
            "publication_state, excludes, created_at, updated_at, "
            "expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (sid, target["set_id"], int(target["schema_revision"]),
             target["key"], "{}", state, target_id, now, now, expires_at),
        )
        db.conn.commit()
    finally:
        db.close()
    _call_emit_hook(
        operation="exclude",
        snapshot=_make_snapshot(
            target["set_id"], int(target["schema_revision"]),
            target["key"], state, False,
        ),
        org=org,
    )
    return sid


def _reject_peer_setting_target(
    setting_id: str,
    org: str | None,
) -> None:
    """Raise :class:`ops.CrossOrgWriteError` when ``setting_id`` lives in a peer DB.

    Lookup order mirrors :func:`_fetch_setting_any_org`, but instead of
    returning the row we raise so promote/deprecate/remove fail fast
    with the structured cross-org error rather than a bare LookupError.
    """
    from .cross_org import open_peer_db, resolve_peers
    from .ops import CrossOrgWriteError  # local: avoid import cycle at top

    # Internal helper — public callers have already normalized via
    # ``_resolve_org_arg``, so ``org`` is a literal slug-or-None.
    resolved_org = org
    for peer in sorted(resolve_peers(resolved_org, None)):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        row = peer_db.conn.execute(
            "SELECT 1 FROM settings WHERE id = ?", (setting_id,)
        ).fetchone()
        if row is not None:
            raise CrossOrgWriteError(setting_id, peer)


def promote_setting(
    setting_id: str,
    to_state: str,
    *,
    org: "str | None | _CallerOrgSentinel",
) -> None:
    """Transition publication_state. ``LookupError`` if not present.

    Peer-origin targets raise :class:`ops.CrossOrgWriteError` — only the
    origin org may alter a Setting's publication state.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    org = _resolve_org_arg(org)
    _guard_protected_setting_id(setting_id, org)
    if to_state not in VALID_STATES:
        raise ValueError(f"invalid state {to_state!r}; valid: {VALID_STATES}")
    now = _now_iso()
    db = _open(org)
    snapshot = None
    try:
        # Look up set_id/schema_revision before the UPDATE so we can
        # recompute expires_at for cache rows. TTL is a sliding window
        # from updated_at, so any UPDATE that bumps updated_at must
        # restamp expires_at (HTTP-cache max-age semantics).
        pre = db.conn.execute(
            "SELECT set_id, schema_revision FROM settings WHERE id = ?",
            (setting_id,),
        ).fetchone()
        if pre is None:
            _reject_peer_setting_target(setting_id, org)
            raise LookupError(f"setting not found: {setting_id!r}")
        expires_at = schemas.cache_expires_at(
            pre["set_id"], int(pre["schema_revision"]), now,
        )
        db.conn.execute(
            "UPDATE settings SET publication_state = ?, updated_at = ?, "
            "expires_at = ? WHERE id = ?",
            (to_state, now, expires_at, setting_id),
        )
        post = db.conn.execute(
            "SELECT set_id, schema_revision, key, publication_state, "
            "deprecated FROM settings WHERE id = ?",
            (setting_id,),
        ).fetchone()
        if post is not None:
            snapshot = _make_snapshot(
                post["set_id"], post["schema_revision"], post["key"],
                post["publication_state"], post["deprecated"],
            )
        db.conn.commit()
    finally:
        db.close()
    if snapshot is not None:
        _call_emit_hook(operation="promote", snapshot=snapshot, org=org)


def deprecate_setting(
    setting_id: str,
    *,
    org: "str | None | _CallerOrgSentinel",
    successor_id: str | None = None,
) -> None:
    """Mark a Setting deprecated, optionally pointing at a successor.

    Peer-origin targets raise :class:`ops.CrossOrgWriteError`.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    org = _resolve_org_arg(org)
    _guard_protected_setting_id(setting_id, org)
    now = _now_iso()
    db = _open(org)
    snapshot = None
    try:
        pre = db.conn.execute(
            "SELECT set_id, schema_revision FROM settings WHERE id = ?",
            (setting_id,),
        ).fetchone()
        if pre is None:
            _reject_peer_setting_target(setting_id, org)
            raise LookupError(f"setting not found: {setting_id!r}")
        expires_at = schemas.cache_expires_at(
            pre["set_id"], int(pre["schema_revision"]), now,
        )
        db.conn.execute(
            "UPDATE settings SET deprecated = 1, successor_id = ?, "
            "updated_at = ?, expires_at = ? WHERE id = ?",
            (successor_id, now, expires_at, setting_id),
        )
        post = db.conn.execute(
            "SELECT set_id, schema_revision, key, publication_state, "
            "deprecated FROM settings WHERE id = ?",
            (setting_id,),
        ).fetchone()
        if post is not None:
            snapshot = _make_snapshot(
                post["set_id"], post["schema_revision"], post["key"],
                post["publication_state"], post["deprecated"],
            )
        db.conn.commit()
    finally:
        db.close()
    if snapshot is not None:
        _call_emit_hook(operation="deprecate", snapshot=snapshot, org=org)


def remove_settings_by_key_prefix(
    set_id: str,
    *,
    prefix: str,
    org: "str | None | _CallerOrgSentinel",
) -> int:
    """Hard-delete every base/override Setting under ``set_id`` whose key
    matches ``<prefix>:%`` (composite-key child rows).

    ``org`` is **required** — see :func:`add_setting` for the contract.

    Bypasses the per-row ``raw``-only constraint that :func:`remove_setting`
    enforces because this is a system-level cleanup path: callers wipe
    binding rows when the worktree they describe is destroyed, regardless
    of publication state. The companion review_state cache is *not*
    deleted by callers using this — bindings die per-worktree, caches
    persist per-org.

    Returns the number of rows deleted. Cross-org targets raise
    :class:`ops.CrossOrgWriteError` indirectly: peer DBs are read-only
    from this caller's perspective, so the SQL ``DELETE`` simply finds
    nothing in the local DB and returns 0 — there is no silent writethrough.
    """
    org = _resolve_org_arg(org)
    _guard_protected_set(set_id)
    db = _open(org)
    deleted_keys: list[tuple[str, int, str, str, bool]] = []
    try:
        like = _prefix_like_pattern(prefix)
        rows = db.conn.execute(
            "SELECT id, set_id, schema_revision, key, publication_state, "
            "deprecated FROM settings "
            "WHERE set_id = ? AND key LIKE ? ESCAPE '\\'",
            (set_id, like),
        ).fetchall()
        for row in rows:
            deleted_keys.append((
                row["set_id"], int(row["schema_revision"]), row["key"],
                row["publication_state"], bool(row["deprecated"]),
            ))
        cur = db.conn.execute(
            "DELETE FROM settings "
            "WHERE set_id = ? AND key LIKE ? ESCAPE '\\'",
            (set_id, like),
        )
        n = cur.rowcount or 0
        db.conn.commit()
    finally:
        db.close()
    for sid, srev, key, state, dep in deleted_keys:
        _call_emit_hook(
            operation="delete",
            snapshot=_make_snapshot(sid, srev, key, state, dep),
            org=org,
        )
    return n


def remove_setting(
    setting_id: str,
    *,
    org: "str | None | _CallerOrgSentinel",
) -> None:
    """Hard-delete a Setting. Spec restricts to ``raw``; higher states must
    be deprecated first.

    Peer-origin targets raise :class:`ops.CrossOrgWriteError`.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    org = _resolve_org_arg(org)
    _guard_protected_setting_id(setting_id, org)
    db = _open(org)
    snapshot = None
    try:
        row = db.conn.execute(
            "SELECT set_id, schema_revision, key, publication_state, "
            "deprecated FROM settings WHERE id = ?",
            (setting_id,),
        ).fetchone()
        if not row:
            _reject_peer_setting_target(setting_id, org)
            raise LookupError(f"setting not found: {setting_id!r}")
        if row["publication_state"] != "raw":
            raise ValueError(
                f"can only remove raw Settings; this is "
                f"{row['publication_state']!r} — deprecate first"
            )
        # Capture pre-delete state so the post-commit hook still has
        # set_id/key/etc. — the row is gone after DELETE.
        snapshot = _make_snapshot(
            row["set_id"], row["schema_revision"], row["key"],
            row["publication_state"], row["deprecated"],
        )
        db.conn.execute("DELETE FROM settings WHERE id = ?", (setting_id,))
        db.conn.commit()
    finally:
        db.close()
    if snapshot is not None:
        _call_emit_hook(operation="delete", snapshot=snapshot, org=org)


# ── Read paths ───────────────────────────────────────────────


def list_set_ids(
    *,
    org: "str | None | _CallerOrgSentinel",
    peers: list[str] | None = None,
) -> list[str]:
    """Distinct ``set_id`` values visible to org.

    Own DB contributes every ``set_id``; peer DBs contribute only
    ``set_id`` values backed by a ``published``/``canonical`` row. See
    graph://bcce359d-a1d § External view.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    org = _resolve_org_arg(org)
    seen: set[str] = set()
    db = _open(org)
    try:
        rows = db.conn.execute(
            "SELECT DISTINCT set_id FROM settings"
        ).fetchall()
        for r in rows:
            if r[0]:
                seen.add(r[0])
    finally:
        db.close()

    resolved_org = org
    for peer in sorted(resolve_peers(resolved_org, peers)):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        placeholders = ",".join("?" for _ in PEER_VISIBLE_STATES)
        rows = peer_db.conn.execute(
            f"SELECT DISTINCT set_id FROM settings "
            f"WHERE publication_state IN ({placeholders}) "
            f"  AND excludes IS NULL AND deprecated = 0",
            list(PEER_VISIBLE_STATES),
        ).fetchall()
        for r in rows:
            if r[0]:
                seen.add(r[0])
    return sorted(seen)


def _resolve_settings_caller(org: str | None) -> str | None:
    """Thin wrapper over ``ops._resolve_org`` to avoid the import cycle
    at module import time. Honours the same cascade: explicit kwarg,
    then per-request contextvar (set by dashboard middleware), then
    ``GRAPH_ORG`` env, then ``None`` (scopeless default)."""
    from . import ops as _ops
    return _ops._resolve_org(org)


def resolve_setting_strict(
    value: str,
    *,
    org: "str | None | _CallerOrgSentinel",
    peers: list[str] | None = None,
) -> dict | list[dict] | None:
    """Resolve a Setting by full id or id-prefix, own-first then peers.

    Mirrors :func:`resolve_source_strict` semantics. Own-org sees every
    state; peer DBs only contribute rows whose ``publication_state`` is
    ``published`` or ``canonical``. Returns:

    * dict — single row (success)
    * list[dict] — multiple matches in one scope (caller surfaces as
      ambiguity error with candidate UUIDs)
    * None — no match anywhere in scope

    The resolver short-circuits as soon as a scope (own, then each peer)
    yields any match, so an exact own-org hit wins over a peer prefix
    match — consistent with how source resolution already behaves.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    org = _resolve_org_arg(org)
    db = _open(org)
    try:
        # 1. Exact id, own org
        row = db.conn.execute(
            "SELECT * FROM settings WHERE id = ?", (value,)
        ).fetchone()
        if row is not None:
            return dict(row)
        # 2. Prefix id, own org
        rows = db.conn.execute(
            "SELECT * FROM settings WHERE id LIKE ?", (f"{value}%",)
        ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1:
            return [dict(r) for r in rows]
    finally:
        db.close()

    # 3. Peer scan (public surface only)
    resolved_org = org
    placeholders = ",".join("?" for _ in PEER_VISIBLE_STATES)
    for peer in sorted(resolve_peers(resolved_org, peers)):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        # exact id
        row = peer_db.conn.execute(
            "SELECT * FROM settings WHERE id = ?", (value,)
        ).fetchone()
        if row is not None and row["publication_state"] in PEER_VISIBLE_STATES:
            return dict(row)
        # prefix id
        rows = peer_db.conn.execute(
            f"SELECT * FROM settings WHERE id LIKE ? "
            f"  AND publication_state IN ({placeholders})",
            (f"{value}%", *PEER_VISIBLE_STATES),
        ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1:
            return [dict(r) for r in rows]
    return None


def resolve_set_key(
    set_id: str,
    key: str,
    *,
    org: "str | None | _CallerOrgSentinel",
    peers: list[str] | None = None,
) -> dict | None:
    """Resolve ``(set_id, key)`` to the winning base Setting row.

    Uses :func:`read_set` so the answer matches what consumers see at
    runtime — same precedence, same cross-org visibility, same exclude
    rules. Returns the underlying base row dict (the one that becomes
    ``ResolvedSetting.id`` post-merge), or None if no member matches.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    org = _resolve_org_arg(org)
    members = read_set(set_id, org=org, peers=peers)
    for m in members.members:
        if m.key == key:
            # m.id is the chosen base id; fetch the row in its origin DB.
            return _fetch_setting_any_org(m.id, org)
    return None


def chain_setting(
    set_id: str,
    key: str,
    *,
    org: "str | None | _CallerOrgSentinel",
    peers: list[str] | None = None,
) -> dict | None:
    """Walk the supersedes chain for ``(set_id, key)`` and return the
    per-layer contributions.

    Returns ``None`` if no winning base exists. Otherwise returns:

    .. code-block:: python

        {
            "set_id": ...,
            "key": ...,
            "layers": [
                {
                    "id": ..., "kind": "base"|"override",
                    "state": ..., "stored_revision": ..., "org": ...,
                    "supersedes": ..., "created_at": ...,
                    "patch": {...},   # full base payload, or override patch
                    "result": {...},  # merged-so-far after this layer
                },
                ...
            ],
            "final": {...},          # final resolved payload
        }

    Override application order matches :func:`read_set`: each override
    whose ``supersedes`` points at the chosen base contributes one
    layer. Overrides of overrides are not currently followed (the
    underlying resolver does not chase them either).

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    org = _resolve_org_arg(org)
    resolved_org = org
    raw_rows: list[tuple[str | None, Any]] = []
    db = _open(org)
    try:
        rows = db.conn.execute(
            "SELECT * FROM settings WHERE set_id = ? AND key = ? "
            "  AND deprecated = 0",
            (set_id, key),
        ).fetchall()
        for r in rows:
            raw_rows.append((resolved_org, r))
    finally:
        db.close()

    placeholders = ",".join("?" for _ in PEER_VISIBLE_STATES)
    for peer in sorted(resolve_peers(resolved_org, peers)):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        rows = peer_db.conn.execute(
            f"SELECT * FROM settings WHERE set_id = ? AND key = ? "
            f"  AND deprecated = 0 "
            f"  AND publication_state IN ({placeholders})",
            (set_id, key, *PEER_VISIBLE_STATES),
        ).fetchall()
        for r in rows:
            raw_rows.append((peer, r))

    if not raw_rows:
        return None

    bases: list[tuple[str | None, Any]] = []
    overrides: list[tuple[str | None, Any]] = []
    excludes: list[tuple[str | None, Any]] = []
    for src_org, r in raw_rows:
        if r["excludes"] is not None:
            excludes.append((src_org, r))
        elif r["supersedes"] is not None:
            overrides.append((src_org, r))
        else:
            bases.append((src_org, r))

    excluded_ids = {r["excludes"] for (_, r) in excludes}
    candidate_bases = [
        (o, r) for (o, r) in bases if r["id"] not in excluded_ids
    ]
    if not candidate_bases:
        return None
    candidate_bases.sort(key=lambda om: om[1]["created_at"] or "", reverse=True)
    candidate_bases.sort(
        key=lambda om: PRECEDENCE.get(om[1]["publication_state"], 99),
    )
    chosen_org, chosen_row = candidate_bases[0]

    base_payload = json.loads(chosen_row["payload"])
    layers: list[dict] = [{
        "id": chosen_row["id"],
        "kind": "base",
        "state": chosen_row["publication_state"],
        "stored_revision": int(chosen_row["schema_revision"]),
        "org": chosen_org,
        "supersedes": chosen_row["supersedes"],
        "created_at": chosen_row["created_at"],
        "patch": base_payload,
        "result": dict(base_payload),
    }]

    merged = dict(base_payload)
    for ov_org, ov_row in overrides:
        if ov_row["supersedes"] != chosen_row["id"]:
            continue
        ov_payload = json.loads(ov_row["payload"])
        merged = json_merge_patch(merged, ov_payload)
        layers.append({
            "id": ov_row["id"],
            "kind": "override",
            "state": ov_row["publication_state"],
            "stored_revision": int(ov_row["schema_revision"]),
            "org": ov_org,
            "supersedes": ov_row["supersedes"],
            "created_at": ov_row["created_at"],
            "patch": ov_payload,
            "result": dict(merged),
        })

    return {
        "set_id": set_id,
        "key": key,
        "layers": layers,
        "final": merged,
    }


def _fetch_setting_any_org(
    setting_id: str,
    org: str | None,
) -> dict | None:
    """Return the Setting row as a plain dict, searching own-org then peers.

    Peer rows must satisfy the public-surface filter
    (``publication_state IN ('published','canonical')``). Used by
    override/exclude targets, which are allowed to reference peer
    content — the *override row* itself still lands in org's DB.
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    db = _open(org)
    try:
        row = db.conn.execute(
            "SELECT * FROM settings WHERE id = ?", (setting_id,)
        ).fetchone()
        if row is not None:
            return dict(row)
    finally:
        db.close()

    resolved_org = _resolve_settings_caller(org)
    for peer in sorted(resolve_peers(resolved_org, None)):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        row = peer_db.conn.execute(
            "SELECT * FROM settings WHERE id = ?", (setting_id,)
        ).fetchone()
        if row is not None and row["publication_state"] in PEER_VISIBLE_STATES:
            return dict(row)
    return None


def get_setting(
    setting_id: str,
    *,
    org: "str | None | _CallerOrgSentinel",
    peers: list[str] | None = None,
    target_revision: int | None = None,
    model: type[Any] | None = None,
) -> ResolvedSetting[Any] | None:
    """Resolve a single Setting by id, own-first then peers.

    ``None`` if not found or dropped by revision constraints. Peer rows
    only surface when their ``publication_state`` is ``published`` or
    ``canonical``.

    ``model=`` mirrors :func:`read_set`: when supplied, the payload is
    passed through ``model.model_validate`` and the returned
    :class:`ResolvedSetting` carries the typed instance. A validation
    failure logs WARN and returns ``None``.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    org = _resolve_org_arg(org)
    resolved_org = org
    db = _open(org)
    try:
        row = db.conn.execute(
            "SELECT * FROM settings WHERE id = ?", (setting_id,)
        ).fetchone()
        if row:
            resolved = _row_to_resolved(row, org=resolved_org)
    finally:
        db.close()

    if 'resolved' not in locals():
        resolved = None
        for peer in sorted(resolve_peers(resolved_org, peers)):
            peer_db = open_peer_db(peer)
            if peer_db is None:
                continue
            row = peer_db.conn.execute(
                "SELECT * FROM settings WHERE id = ?", (setting_id,)
            ).fetchone()
            if row and row["publication_state"] in PEER_VISIBLE_STATES:
                resolved = _row_to_resolved(row, org=peer)
                break
        if resolved is None:
            return None

    if target_revision is not None:
        transformed, _reason = _shape_to_target(resolved, target_revision)
        if transformed is None:
            return None
        resolved = transformed

    if model is not None:
        typed = _apply_model(resolved, model, DropAccounting())
        return typed
    return resolved


def _prefix_like_pattern(prefix: str) -> str:
    r"""Build the SQL ``LIKE`` pattern for a composite-key prefix match.

    ``prefix=X`` becomes ``'X:%'`` — the ``:`` separator is auto-appended,
    so callers pass parent identity unadorned (e.g., ``prefix="enterprise-ng"``
    matches ``enterprise-ng:vuln-diff`` but not ``enterprise-ng-alt:foo``).
    ``%`` / ``_`` / ``\`` inside the prefix are escaped so literal
    characters keep literal meaning under ``LIKE``.
    """
    escaped = (
        prefix
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"{escaped}:%"


def read_set(
    set_id: str,
    *,
    org: "str | None | _CallerOrgSentinel",
    peers: list[str] | None = None,
    target_revision: int | None = None,
    min_revision: int | None = None,
    prefix: str | None = None,
    model: type[Any] | None = None,
) -> SetMembers[Any]:
    """Resolve members of *set_id* visible to org's session.

    Five-step pipeline (see graph://0d3f750f-f9c § Resolution algorithm):
    1. per-DB fetch (single DB today, peers loop ready),
    2. group by key into bases / overrides / exclusions,
    3. drop excluded bases,
    4. pick highest-precedence base per key (tie-break: most recent),
    5. apply overrides via JSON-merge-patch.

    Optional ``min_revision`` filters before transform; ``target_revision``
    upconverts (or drops if no chain). ``prefix=X`` restricts the query
    to composite keys under the ``X:`` parent (child-set pattern —
    ``<parent>:<child>`` with ``:`` as the canonical separator). ``model=``
    validates each resolved payload through ``model.model_validate`` and
    produces ``SetMembers[Model]``; validation failures are dropped and
    counted in ``dropped.schema_invalid``. Returns a ``SetMembers`` with
    drop accounting populated.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    org = _resolve_org_arg(org)
    resolved_org = org
    raw_rows: list[tuple[str | None, Any]] = []
    deprecated_filtered = 0
    prefix_clause = ""
    prefix_params: tuple[Any, ...] = ()
    if prefix is not None:
        prefix_clause = " AND key LIKE ? ESCAPE '\\'"
        prefix_params = (_prefix_like_pattern(prefix),)
    db = _open(org)
    try:
        rows = db.conn.execute(
            f"SELECT * FROM settings WHERE set_id = ? "
            f"  AND deprecated = 0"
            f"{prefix_clause}",
            (set_id, *prefix_params),
        ).fetchall()
        for r in rows:
            raw_rows.append((resolved_org, r))
        # Count what we just filtered out so operators can spot drift
        # (e.g. a deprecated row still surfacing through some other path).
        dep_row = db.conn.execute(
            f"SELECT COUNT(*) AS n FROM settings WHERE set_id = ? "
            f"  AND deprecated = 1"
            f"{prefix_clause}",
            (set_id, *prefix_params),
        ).fetchone()
        if dep_row is not None:
            deprecated_filtered += int(dep_row["n"])
    finally:
        db.close()

    # Peer-org contributions: public surface only.
    resolved_peers = resolve_peers(resolved_org, peers)
    for peer in sorted(resolved_peers):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        placeholders = ",".join("?" for _ in PEER_VISIBLE_STATES)
        try:
            rows = peer_db.conn.execute(
                f"SELECT * FROM settings WHERE set_id = ? "
                f"  AND deprecated = 0 "
                f"  AND publication_state IN ({placeholders})"
                f"{prefix_clause}",
                (set_id, *PEER_VISIBLE_STATES, *prefix_params),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table: settings" in str(exc).lower():
                continue
            raise
        for r in rows:
            raw_rows.append((peer, r))
        try:
            dep_row = peer_db.conn.execute(
                f"SELECT COUNT(*) AS n FROM settings WHERE set_id = ? "
                f"  AND deprecated = 1 "
                f"  AND publication_state IN ({placeholders})"
                f"{prefix_clause}",
                (set_id, *PEER_VISIBLE_STATES, *prefix_params),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: settings" in str(exc).lower():
                continue
            raise
        if dep_row is not None:
            deprecated_filtered += int(dep_row["n"])

    dropped = DropAccounting()
    dropped.deprecated_filtered = deprecated_filtered

    # Apply min_revision floor before transform (drops live rows wholesale).
    survivors: list[tuple[str | None, Any]] = []
    for src_org, row in raw_rows:
        if min_revision is not None and int(row["schema_revision"]) < min_revision:
            dropped.below_min_revision += 1
            continue
        survivors.append((src_org, row))

    # Group by key.
    bases: dict[str, list[tuple[str | None, Any]]] = {}
    overrides: dict[str, list[tuple[str | None, Any]]] = {}
    excludes: dict[str, list[tuple[str | None, Any]]] = {}
    for src_org, row in survivors:
        bucket = (
            "excludes" if row["excludes"] is not None
            else "overrides" if row["supersedes"] is not None
            else "bases"
        )
        target = (excludes if bucket == "excludes"
                  else overrides if bucket == "overrides"
                  else bases)
        target.setdefault(row["key"], []).append((src_org, row))

    members: list[ResolvedSetting[Any]] = []
    keys_seen = sorted(bases.keys())
    for key in keys_seen:
        excluded_ids = {row["excludes"] for (_, row) in excludes.get(key, [])}
        candidate_bases = [
            (src_org, row) for (src_org, row) in bases[key]
            if row["id"] not in excluded_ids
        ]
        if not candidate_bases:
            continue

        # Pick highest precedence; tie-break by most recent created_at.
        # Two-pass stable sort: recency first, then precedence wins.
        candidate_bases.sort(key=lambda om: om[1]["created_at"] or "", reverse=True)
        candidate_bases.sort(
            key=lambda om: PRECEDENCE.get(om[1]["publication_state"], 99),
        )
        chosen_org, chosen_row = candidate_bases[0]

        # Apply overrides whose supersedes targets this base.
        merged_payload = json.loads(chosen_row["payload"])
        for (_, ov_row) in overrides.get(key, []):
            if ov_row["supersedes"] == chosen_row["id"]:
                ov_payload = json.loads(ov_row["payload"])
                merged_payload = json_merge_patch(merged_payload, ov_payload)

        resolved = _row_to_resolved(chosen_row, org=chosen_org)
        resolved.payload = merged_payload

        # Optional revision transform.
        if target_revision is not None:
            transformed, reason = _shape_to_target(resolved, target_revision)
            if transformed is None:
                if reason == "no_upconvert_path":
                    dropped.no_upconvert_path += 1
                elif reason == "above_target_no_downgrade":
                    dropped.above_target_no_downgrade += 1
                continue
            resolved = transformed

        # Optional payload typing via Pydantic (or compatible) model.
        if model is not None:
            typed = _apply_model(resolved, model, dropped)
            if typed is None:
                continue
            resolved = typed

        members.append(resolved)

    return SetMembers(members=members, dropped=dropped)


def read_owned_set(
    set_id: str,
    *,
    org: "str | None | _CallerOrgSentinel",
    target_revision: int | None = None,
    min_revision: int | None = None,
    prefix: str | None = None,
    model: type[Any] | None = None,
) -> SetMembers[Any]:
    """Resolve *set_id* from its owning database only.

    ``read_set`` deliberately composes the owner's rows with published and
    canonical rows from subscribed peer orgs.  Identity, authentication, and
    authority-binding decisions must not use that federated view: choosing
    ``org=None`` or a concrete org selects the owning database, but does *not*
    by itself disable peer composition.  This named reader makes the security
    boundary explicit and prevents those call sites from silently forgetting
    the otherwise easy-to-miss ``peers=[]`` argument.
    """
    return read_set(
        set_id,
        org=org,
        peers=[],
        target_revision=target_revision,
        min_revision=min_revision,
        prefix=prefix,
        model=model,
    )


# ── Schema versioning helpers ────────────────────────────────


def _shape_to_target(
    resolved: ResolvedSetting,
    target_revision: int,
) -> tuple[ResolvedSetting | None, str]:
    """Return (transformed, '') on success, or (None, reason) on drop.

    Identity case copies through. Lower stored → upconvert via registry chain.
    Higher stored → drop with reason ``above_target_no_downgrade``.
    Missing chain → drop with reason ``no_upconvert_path``.
    """
    stored = resolved.stored_revision
    if stored == target_revision:
        out = replace(resolved, target_revision=target_revision)
        return out, ""
    if stored > target_revision:
        return None, "above_target_no_downgrade"
    converted = schemas.upconvert_payload(
        resolved.set_id, stored, target_revision, resolved.payload,
    )
    if converted is None:
        return None, "no_upconvert_path"
    out = replace(
        resolved,
        payload=converted,
        target_revision=target_revision,
        upconverted=True,
    )
    return out, ""


# ── Storage migration ────────────────────────────────────────


def migrate_setting_revisions(
    set_id: str,
    to_revision: int,
    *,
    org: "str | None | _CallerOrgSentinel",
    dry_run: bool = False,
) -> MigrationReport:
    """Rewrite stored rows at lower revisions up to ``to_revision``.

    Optional housekeeping — read-time upconvert keeps things working without
    it. Rows already at ``to_revision`` are skipped; rows above it are left
    alone (downgrades are explicit opt-ins, not part of migrate). Rows with
    no upconvert chain are reported, not rewritten.

    ``org`` is **required** — see :func:`add_setting` for the contract.
    """
    org = _resolve_org_arg(org)
    _guard_protected_set(set_id)
    report = MigrationReport(
        set_id=set_id, to_revision=int(to_revision), dry_run=dry_run,
    )
    now = _now_iso()
    # Migrate rewrites every row to ``to_revision`` — its TTL is the only
    # one we need to know. Compute once and reuse for every cache-row
    # rewrite in this loop.
    expires_at = schemas.cache_expires_at(set_id, int(to_revision), now)
    db = _open(org)
    affected_snapshots: list[dict] = []
    try:
        rows = db.conn.execute(
            "SELECT id, schema_revision, payload, key, publication_state, "
            "deprecated FROM settings "
            "WHERE set_id = ? AND excludes IS NULL",
            (set_id,),
        ).fetchall()
        for row in rows:
            stored = int(row["schema_revision"])
            if stored == to_revision:
                report.already_at_target += 1
                continue
            if stored > to_revision:
                report.above_target += 1
                continue
            payload = json.loads(row["payload"])
            converted = schemas.upconvert_payload(
                set_id, stored, to_revision, payload,
            )
            if converted is None:
                report.no_upconvert_path += 1
                continue
            report.affected_ids.append(row["id"])
            report.rewrote += 1
            if not dry_run:
                db.conn.execute(
                    "UPDATE settings SET payload = ?, schema_revision = ?, "
                    "updated_at = ?, expires_at = ? WHERE id = ?",
                    (json.dumps(converted), int(to_revision), now,
                     expires_at, row["id"]),
                )
                # Schema revision is now to_revision; key / state /
                # deprecated unchanged by migrate.
                affected_snapshots.append(_make_snapshot(
                    set_id, int(to_revision), row["key"],
                    row["publication_state"], row["deprecated"],
                ))
        if not dry_run:
            db.conn.commit()
    finally:
        db.close()
    if not dry_run:
        for snap in affected_snapshots:
            _call_emit_hook(operation="migrate", snapshot=snap, org=org)
    return report


# ── Helpers ──────────────────────────────────────────────────


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tracked_set_id_from_row_id(
    row_id: str | None, org: str | None,
) -> str | None:
    if not row_id:
        return None
    row = _fetch_setting_any_org(row_id, org)
    if row is None:
        return None
    return row.get("set_id")


def _tracked_settings_call(
    operation: str,
    *,
    kind: str,
    set_id_getter: Callable[[tuple[Any, ...], dict[str, Any], Any], str | None] | None = None,
    result_count_getter: Callable[[tuple[Any, ...], dict[str, Any], Any], int] | None = None,
):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            # Public API normalizes ``org=`` via ``_resolve_org_arg``;
            # mirror that here so stats labels reflect the resolved slug
            # (including the cascade result when ``CALLER_ORG`` is in
            # play). Missing ``org=`` would raise ``TypeError`` inside
            # the wrapped function — the wrapper still records the
            # failed call.
            try:
                resolved_org = _resolve_org_arg(kwargs.get("org"))
            except Exception:
                resolved_org = None
            ok = False
            result = None
            try:
                result = fn(*args, **kwargs)
                ok = True
                return result
            finally:
                set_id = None
                if set_id_getter is not None:
                    try:
                        set_id = set_id_getter(args, kwargs, result)
                    except Exception:
                        set_id = None
                result_count = 0
                if ok:
                    if result_count_getter is not None:
                        try:
                            result_count = max(
                                int(result_count_getter(args, kwargs, result)),
                                0,
                            )
                        except Exception:
                            result_count = _stats_result_count(result)
                    else:
                        result_count = _stats_result_count(result)
                _record_settings_api_call(
                    operation=operation,
                    set_id=set_id,
                    org=resolved_org,
                    kind=kind,
                    ok=ok,
                    start=start,
                    result_count=result_count,
                )

        return wrapper

    return decorator


add_setting = _tracked_settings_call(
    "add_setting",
    kind="write",
    set_id_getter=lambda args, _kwargs, _result: args[0] if args else None,
)(add_setting)
upsert_by_key = _tracked_settings_call(
    "upsert_by_key",
    kind="write",
    set_id_getter=lambda args, _kwargs, _result: args[0] if args else None,
)(upsert_by_key)
override_setting = _tracked_settings_call(
    "override_setting",
    kind="write",
    set_id_getter=lambda _args, kwargs, result: _tracked_set_id_from_row_id(
        result, kwargs.get("org"),
    ),
)(override_setting)
exclude_setting = _tracked_settings_call(
    "exclude_setting",
    kind="write",
    set_id_getter=lambda _args, kwargs, result: _tracked_set_id_from_row_id(
        result, kwargs.get("org"),
    ),
)(exclude_setting)
promote_setting = _tracked_settings_call(
    "promote_setting",
    kind="write",
    set_id_getter=lambda args, kwargs, _result: _tracked_set_id_from_row_id(
        args[0] if args else None, kwargs.get("org"),
    ),
)(promote_setting)
deprecate_setting = _tracked_settings_call(
    "deprecate_setting",
    kind="write",
    set_id_getter=lambda args, kwargs, _result: _tracked_set_id_from_row_id(
        args[0] if args else None, kwargs.get("org"),
    ),
)(deprecate_setting)
remove_settings_by_key_prefix = _tracked_settings_call(
    "remove_settings_by_key_prefix",
    kind="write",
    set_id_getter=lambda args, _kwargs, _result: args[0] if args else None,
    result_count_getter=lambda _args, _kwargs, result: result,
)(remove_settings_by_key_prefix)
remove_setting = _tracked_settings_call(
    "remove_setting",
    kind="write",
)(remove_setting)
list_set_ids = _tracked_settings_call(
    "list_set_ids",
    kind="read",
)(list_set_ids)
resolve_setting_strict = _tracked_settings_call(
    "resolve_setting_strict",
    kind="read",
)(resolve_setting_strict)
chain_setting = _tracked_settings_call(
    "chain_setting",
    kind="read",
    set_id_getter=lambda args, _kwargs, _result: args[0] if args else None,
)(chain_setting)
get_setting = _tracked_settings_call(
    "get_setting",
    kind="read",
    set_id_getter=lambda _args, _kwargs, result: (
        getattr(result, "set_id", None)
        if result is not None else None
    ),
)(get_setting)
read_set = _tracked_settings_call(
    "read_set",
    kind="read",
    set_id_getter=lambda args, _kwargs, _result: args[0] if args else None,
)(read_set)
migrate_setting_revisions = _tracked_settings_call(
    "migrate_setting_revisions",
    kind="write",
    set_id_getter=lambda args, _kwargs, _result: args[0] if args else None,
    result_count_getter=lambda _args, _kwargs, result: (
        result.rewrote if result is not None else 0
    ),
)(migrate_setting_revisions)
