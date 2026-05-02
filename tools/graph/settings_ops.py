"""Settings primitive — ops layer.

Read, write, resolve, and migrate operations for the ``settings`` table.
Imported into ``tools.graph.ops`` for unified discovery; tests may reach in
here directly. Spec: graph://0d3f750f-f9c. Cross-org rules:
graph://bcce359d-a1d.

Single-DB world: ``org`` and ``peers`` are plumbed but no-op.
Routing slots in cleanly when per-org DB ships (auto-txg5.x).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, asdict, replace
from typing import Any, Callable, Generic, Iterator, TypeVar
from uuid import uuid4

from .db import GraphDB, resolve_caller_db_path
from . import schemas


logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────


VALID_STATES = ("raw", "curated", "published", "canonical")
PRECEDENCE = {"canonical": 0, "published": 1, "curated": 2, "raw": 3}
PEER_VISIBLE_STATES = ("published", "canonical")


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


def _db_path(org: str | None = None) -> str | None:
    """Resolve Settings DB path via the same cascade as ``ops._db_path``.

    Explicit ``org`` wins; otherwise ``GRAPH_ORG`` env is used; the
    resolver applies the scopeless default (``personal``) when neither is
    set. ``GRAPH_DB`` env pins the path regardless (test override).
    """
    env_db = os.environ.get("GRAPH_DB")
    if env_db:
        return env_db
    if org is None:
        org = os.environ.get("GRAPH_ORG")
    return str(resolve_caller_db_path(org))


def _open(org: str | None = None) -> GraphDB:
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
    org: str | None = None,
    state: str = "raw",
) -> str:
    """Create a base Setting in org's DB.

    Validates payload against ``(set_id, schema_revision)``. Returns the new
    Setting id. Raises ``schemas.SchemaValidationError`` on validation
    failure, ``ValueError`` on bad ``state``.
    """
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


def override_setting(
    target_id: str,
    payload_overrides: dict,
    *,
    org: str | None = None,
    state: str = "raw",
) -> str:
    """Create a Setting with ``supersedes=target_id`` and partial payload.

    Validation runs against the target's ``(set_id, schema_revision)``
    using the *merged* shape — what consumers will actually see. The
    override Setting itself lives in org's DB; the *target* may
    be either own-org or peer-origin — overriding peer content is the
    expected way to adapt shared primitives to a local org. Raises
    ``LookupError`` only when the target exists nowhere (own or peers).
    """
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
    org: str | None = None,
    state: str = "raw",
) -> str:
    """Create a Setting with ``excludes=target_id`` and empty payload.

    The exclude row itself lives in org's DB and only affects
    reads scoped to this caller — so peer-origin targets are allowed.
    """
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

    resolved_org = _resolve_settings_caller(org)
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
    org: str | None = None,
) -> None:
    """Transition publication_state. ``LookupError`` if not present.

    Peer-origin targets raise :class:`ops.CrossOrgWriteError` — only the
    origin org may alter a Setting's publication state.
    """
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
    org: str | None = None,
    successor_id: str | None = None,
) -> None:
    """Mark a Setting deprecated, optionally pointing at a successor.

    Peer-origin targets raise :class:`ops.CrossOrgWriteError`.
    """
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


def remove_setting(
    setting_id: str,
    *,
    org: str | None = None,
) -> None:
    """Hard-delete a Setting. Spec restricts to ``raw``; higher states must
    be deprecated first.

    Peer-origin targets raise :class:`ops.CrossOrgWriteError`.
    """
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
    org: str | None = None,
    peers: list[str] | None = None,
) -> list[str]:
    """Distinct ``set_id`` values visible to org.

    Own DB contributes every ``set_id``; peer DBs contribute only
    ``set_id`` values backed by a ``published``/``canonical`` row. See
    graph://bcce359d-a1d § External view.
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

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

    resolved_org = _resolve_settings_caller(org)
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
    org: str | None = None,
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
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

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
    resolved_org = _resolve_settings_caller(org)
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
    org: str | None = None,
    peers: list[str] | None = None,
) -> dict | None:
    """Resolve ``(set_id, key)`` to the winning base Setting row.

    Uses :func:`read_set` so the answer matches what consumers see at
    runtime — same precedence, same cross-org visibility, same exclude
    rules. Returns the underlying base row dict (the one that becomes
    ``ResolvedSetting.id`` post-merge), or None if no member matches.
    """
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
    org: str | None = None,
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
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    resolved_org = _resolve_settings_caller(org)
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
    org: str | None = None,
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
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    resolved_org = _resolve_settings_caller(org)
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
    org: str | None = None,
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
    """
    from .cross_org import (
        PEER_VISIBLE_STATES,
        open_peer_db,
        resolve_peers,
    )

    resolved_org = _resolve_settings_caller(org)
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
        rows = peer_db.conn.execute(
            f"SELECT * FROM settings WHERE set_id = ? "
            f"  AND deprecated = 0 "
            f"  AND publication_state IN ({placeholders})"
            f"{prefix_clause}",
            (set_id, *PEER_VISIBLE_STATES, *prefix_params),
        ).fetchall()
        for r in rows:
            raw_rows.append((peer, r))
        dep_row = peer_db.conn.execute(
            f"SELECT COUNT(*) AS n FROM settings WHERE set_id = ? "
            f"  AND deprecated = 1 "
            f"  AND publication_state IN ({placeholders})"
            f"{prefix_clause}",
            (set_id, *PEER_VISIBLE_STATES, *prefix_params),
        ).fetchone()
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
    org: str | None = None,
    dry_run: bool = False,
) -> MigrationReport:
    """Rewrite stored rows at lower revisions up to ``to_revision``.

    Optional housekeeping — read-time upconvert keeps things working without
    it. Rows already at ``to_revision`` are skipped; rows above it are left
    alone (downgrades are explicit opt-ins, not part of migrate). Rows with
    no upconvert chain are reported, not rewritten.
    """
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
