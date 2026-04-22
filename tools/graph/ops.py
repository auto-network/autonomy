"""Graph service layer.

All graph DB access flows through this module. CLI commands (via
``client.LocalClient``) and dashboard API handlers both call into ``ops.*``;
neither imports ``GraphDB`` directly outside this layer.

Per-org write routing is live (auto-txg5.3): every write lands in the
caller's own org DB. ``org`` resolution cascade:

  1. Explicit ``org=`` kwarg (API handlers, tests, container-aware
     call sites).
  2. ``GRAPH_ORG`` env var (container-scoped default — session launcher
     exports it, dashboard API handlers pass the incoming ``X-Graph-Org``
     header through when present).
  3. Scopeless default → ``personal`` (per the orgs-schema reconciliation;
     auto-s45z9 absorbed here).

``GRAPH_DB`` env still wins above all of the above (test pinning / override).
``peers`` stays reserved for cross-org reads (auto-txg5.4); writes remain
own-org only (cross-org writes are not supported — promote content in
its origin org).

Design reference: graph://bcce359d-a1d (Cross-Org Search Architecture).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .db import GraphDB, resolve_caller_db_path
from . import cross_org
from .cross_org import (
    PEER_VISIBLE_STATES,
    chronological_merge,
    open_peer_db,
    resolve_peers,
    rrf_merge,
    run_across_orgs,
)
from .settings_ops import (  # noqa: F401 — re-exported as ops.* surface
    ResolvedSetting,
    SetMembers,
    MigrationReport,
    add_setting,
    override_setting,
    exclude_setting,
    promote_setting,
    deprecate_setting,
    remove_setting,
    list_set_ids,
    get_setting,
    read_set,
    migrate_setting_revisions,
    json_merge_patch,
)


# ── Cross-org write-rejection error ──────────────────────────


class CrossOrgWriteError(RuntimeError):
    """Raised when a mutation targets a peer-origin Source/Setting.

    Per the spec (graph://bcce359d-a1d § Cross-org write semantics):
    writes MUST act in the origin org. We surface a structured message
    so CLI + API can translate it into the ``cannot modify cross-org
    content; act in origin org <slug>`` response shape without string
    parsing.

    Exceptions (link/bead primitives) stay write-routed to org
    even when the *target* source lives in a peer DB — the edge/bead
    row is owned by org, so it's a local write that happens to
    reference a peer ID.
    """

    def __init__(self, target_id: str, origin_org: str):
        self.target_id = target_id
        self.origin_org = origin_org
        super().__init__(
            f"cannot modify cross-org content; act in origin org {origin_org!r}"
        )

    def to_dict(self) -> dict:
        return {
            "error": "cross_org_write_rejected",
            "target_id": self.target_id,
            "origin_org": self.origin_org,
            "message": str(self),
        }


# ── DB selection ─────────────────────────────────────────────


import contextvars as _ctxvars

# Per-request caller-org context. The dashboard's ``caller_org_middleware``
# sets this from the ``X-Graph-Org`` header on every inbound request, so
# every ``ops.*`` call inside any handler sees the right org automatically
# — no manual ``org=`` threading at each endpoint. See
# graph://bcce359d-a1d § Cross-org request routing.
_caller_org_var: "_ctxvars.ContextVar[str | None]" = _ctxvars.ContextVar(
    "graph_caller_org", default=None,
)


def set_caller_org(org: str | None):
    """Set the request-scoped caller org and return the reset token.

    Callers MUST call ``reset(token)`` in a ``finally`` block to avoid
    leaking state between requests. Middleware does this automatically.
    """
    return _caller_org_var.set(org)


def reset_caller_org(token) -> None:
    """Reset the caller-org contextvar using the token from ``set_caller_org``."""
    _caller_org_var.reset(token)


def _resolve_org(org: str | None) -> str | None:
    """Apply the org resolution cascade (explicit → contextvar → env → default).

    Priority:
      1. Explicit ``org=`` kwarg (wins; used by code that knows exactly
         which org it wants — ``ops`` internal helpers, host CLI tests).
      2. Per-request contextvar — set by ``caller_org_middleware`` on the
         dashboard from ``X-Graph-Org``. Handlers don't need to thread
         ``org=`` through; the ops layer picks it up automatically.
      3. ``GRAPH_ORG`` env — host CLI (no middleware in play).
      4. ``None`` — scopeless default (callers iterate every per-org DB
         in :func:`_iter_org_dbs`, except when ``GRAPH_DB`` pinning is
         active — see :func:`_global_scope_active`).
    """
    if org is not None:
        return org
    ctx_org = _caller_org_var.get()
    if ctx_org:
        return ctx_org
    env_org = os.environ.get("GRAPH_ORG")
    if env_org:
        return env_org
    return None


def _global_scope_active(resolved_org: str | None, only_org: str | None) -> bool:
    """Return True when reads should fan out across every per-org DB.

    Global scope fires only when no caller is set anywhere AND ``GRAPH_DB``
    pinning is inactive. ``GRAPH_DB`` is the test/legacy override that
    collapses the entire stack to a single DB; when it's set we route as
    if the caller were that single DB's only org (peers already shrink
    to ``[]`` via :func:`cross_org.resolve_peers`).
    """
    if resolved_org is not None or only_org is not None:
        return False
    if os.environ.get("GRAPH_DB"):
        return False
    return True


def _db_path(org: str | None = None) -> str | None:
    """Resolve graph DB path for the given ``org``.

    ``GRAPH_DB`` env var wins (test/override path); otherwise routes to
    ``data/orgs/<slug>.db`` where ``slug`` is the resolved caller org
    (explicit kwarg → ``GRAPH_ORG`` env → scopeless default ``personal``).
    See :func:`db.resolve_caller_db_path`.
    """
    env_db = os.environ.get("GRAPH_DB")
    if env_db:
        return env_db
    return str(resolve_caller_db_path(_resolve_org(org)))


def _open(org: str | None = None) -> GraphDB:
    """Open a GraphDB for ``org``'s routed path.

    Returns a fresh (non-pooled) connection. Callers are expected to use
    the existing ``try: ... finally: db.close()`` pattern. Process-
    lifetime pooling is available via :meth:`GraphDB.for_org` for the
    dashboard's server-side handlers that want to amortise connection
    cost across requests.
    """
    return GraphDB(_db_path(org))


def _iter_org_dbs() -> "list[tuple[str, GraphDB]]":
    """Pooled read handles for every known org DB. Used by global-scope
    read paths — when no caller_org is set (no explicit kwarg, no
    contextvar, no env), reads sweep every org's full surface as if each
    were the caller's own DB. See graph://bcce359d-a1d § Global scope.

    The handles come from :func:`cross_org.open_peer_db` which uses the
    process-lifetime pool — **callers MUST NOT close them.**
    """
    from .cross_org import list_org_slugs
    out: list[tuple[str, GraphDB]] = []
    for slug in sorted(list_org_slugs()):
        db = open_peer_db(slug)
        if db is None:
            continue
        out.append((slug, db))
    return out


def _scan_orgs_for_row(fn) -> "dict | None":
    """Scan every org DB for the first ID-lookup hit (scopeless callers).

    ``fn`` is a ``(GraphDB) -> dict | None`` that runs a single-row lookup
    against one org DB. The first non-None hit is annotated with ``org``
    and returned; no state filter is applied because the caller has no
    org seat (operator UI, host terminal). See user directive 2026-04-22:
    scopeless = see all orgs equally.
    """
    for slug, slug_db in _iter_org_dbs():
        row = fn(slug_db)
        if row is None:
            continue
        if isinstance(row, dict):
            row.setdefault("org", slug)
        return row
    return None


def _union_across_orgs(fn) -> list[dict]:
    """Union a list query across every org DB (scopeless callers).

    ``fn`` is a ``(GraphDB) -> list[dict]`` that runs the listing against
    one org DB. Rows are concatenated in the order orgs are iterated
    (alphabetical by slug); callers that need a specific ordering are
    responsible for re-sorting the union. Every row is annotated with
    ``org`` unless it already carries one.
    """
    out: list[dict] = []
    for slug, slug_db in _iter_org_dbs():
        rows = fn(slug_db) or []
        for r in rows:
            if isinstance(r, dict):
                r.setdefault("org", slug)
        out.extend(rows)
    return out


# ── Read paths ───────────────────────────────────────────────


def search(
    q: str,
    *,
    org: str | None = None,
    peers: list[str] | None = None,
    only_org: str | None = None,
    limit: int = 25,
    project: str | None = None,
    or_mode: bool = False,
    tag: str | None = None,
    states: list[str] | None = None,
    include_raw: bool = False,
    session_source_ids: list[str] | None = None,
    session_author_pattern: str | None = None,
) -> list[dict]:
    """Full-text search across the graph with cross-org RRF merge.

    Returns a list of result dicts (sources, edges, thoughts) — see
    ``GraphDB.search`` for the base shape. Every row is annotated with
    ``org`` (origin slug) and ``rrf_score`` (see graph://bcce359d-a1d
    § Merge algorithms).

    ``states`` / ``include_raw`` / ``session_*`` tune the publication_state
    filter on the caller's own DB (full surface). Peer rows are always
    clamped to ``published``/``canonical`` regardless — that's the
    public surface contract.

    ``only_org`` pins the search to a single org (own slug or a peer
    slug), bypassing peer merge. Useful for ``graph search --only-org``.
    ``peers`` overrides the resolved peer set (empty list = isolated,
    None = default from subscription Setting or every sibling DB).
    """
    resolved_org = _resolve_org(org)

    if _global_scope_active(resolved_org, only_org):
        # GLOBAL caller: search every org as own-surface, no peer filter.
        # Annotate each row with origin slug; merge with org-agnostic RRF.
        # Handles from _iter_org_dbs are pooled — must NOT close them.
        org_lists: list[tuple[str, list[dict]]] = []
        for slug, slug_db in _iter_org_dbs():
            rows = slug_db.search(
                q, limit=limit, project=project, or_mode=or_mode, tag=tag,
                states=states, include_raw=include_raw,
            )
            for r in rows:
                r["org"] = slug
            org_lists.append((slug, rows))
        if not org_lists:
            return []
        return rrf_merge(org_lists, limit=limit, own_org=None, key="id")

    # Single-org pin: skip peer resolution entirely.
    if only_org is not None:
        if only_org == resolved_org:
            db = _open(org)
            try:
                rows = db.search(
                    q, limit=limit, project=project, or_mode=or_mode, tag=tag,
                    states=states, include_raw=include_raw,
                    session_source_ids=session_source_ids,
                    session_author_pattern=session_author_pattern,
                )
            finally:
                db.close()
            for r in rows:
                r.setdefault("org", resolved_org or "")
            return rows
        # Peer-only search — force public-surface states.
        peer_db = open_peer_db(only_org)
        if peer_db is None:
            return []
        rows = peer_db.search(
            q, limit=limit, project=project, or_mode=or_mode, tag=tag,
            states=list(PEER_VISIBLE_STATES), include_raw=False,
        )
        for r in rows:
            r.setdefault("org", only_org)
        return rows

    resolved_peers = resolve_peers(resolved_org, peers)

    def fetch_own(db: GraphDB) -> list[dict]:
        return db.search(
            q, limit=limit, project=project, or_mode=or_mode, tag=tag,
            states=states, include_raw=include_raw,
            session_source_ids=session_source_ids,
            session_author_pattern=session_author_pattern,
        )

    def fetch_peer(db: GraphDB, _slug: str) -> list[dict]:
        return db.search(
            q, limit=limit, project=project, or_mode=or_mode, tag=tag,
            states=list(PEER_VISIBLE_STATES), include_raw=False,
        )

    org_lists = run_across_orgs(
        resolved_org, resolved_peers, fetch_own, fetch_peer,
    )
    if not resolved_peers:
        # Own-org only → preserve legacy shape (no RRF re-ranking) so
        # existing tests keep passing. Still annotate ``org`` for
        # consumers that expect it.
        rows = org_lists[0][1] if org_lists else []
        for r in rows:
            r.setdefault("org", resolved_org or "")
        return rows[:limit]
    return rrf_merge(
        org_lists, limit=limit, own_org=resolved_org, key="id",
    )


def get_source(
    source_id: str,
    *,
    org: str | None = None,
    peers: list[str] | None = None,
) -> dict | None:
    """Resolve a source by exact ID or prefix, own-first then peers.

    Own-org is queried first with the full surface. If no hit, every
    subscribed peer is queried in alphabetical order with the public
    surface filter (``published``/``canonical`` only). First hit wins;
    the returned dict carries an ``org`` field identifying the origin.
    """
    resolved_org = _resolve_org(org)

    if _global_scope_active(resolved_org, None):
        # GLOBAL caller (no explicit org, no contextvar, no env, no
        # GRAPH_DB pin). Treat every org as own-surface — scan all org
        # DBs without applying the peer-visible-states filter. Used by
        # dashboard URL handlers and any operator UI where the caller
        # isn't acting from an org seat.
        from .cross_org import list_org_slugs
        for slug in sorted(list_org_slugs()):
            slug_db = open_peer_db(slug)
            if slug_db is None:
                continue
            src = slug_db.get_source(source_id)
            if src is None:
                continue
            src["org"] = slug
            return src
        return None

    db = _open(org)
    try:
        src = db.get_source(source_id)
    finally:
        db.close()
    if src is not None:
        src.setdefault("org", resolved_org or "")
        return src

    for peer in sorted(resolve_peers(resolved_org, peers)):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        src = peer_db.get_source(source_id)
        if src is None:
            continue
        if src.get("publication_state") not in PEER_VISIBLE_STATES:
            continue
        src["org"] = peer
        return src
    return None


def resolve_source_strict(
    source_id: str,
    *,
    org: str | None = None,
    peers: list[str] | None = None,
) -> dict | list[dict] | None:
    """Strict resolver — own-first, then peers (public surface only).

    Returns the source dict, ``None`` (not found), or a list (ambiguous
    prefix on one scope). Ambiguity is resolved per-scope: an exact hit
    in own-org wins over a peer prefix match; ambiguity within a single
    scope surfaces as a list like in the single-DB world.

    Scopeless callers (dashboard operator UI, host terminal — no org
    kwarg / contextvar / GRAPH_ORG / GRAPH_DB) scan every org as
    own-surface, no publication_state filter. Scoped callers keep the
    peer-public-surface contract.
    """
    resolved_org = _resolve_org(org)

    if _global_scope_active(resolved_org, None):
        return _scan_orgs_for_row(lambda d: d.resolve_source_strict(source_id))

    db = _open(org)
    try:
        own = db.resolve_source_strict(source_id)
    finally:
        db.close()
    if own is not None:
        if isinstance(own, dict):
            own.setdefault("org", resolved_org or "")
            return own
        # Ambiguous in own org — surface the list untouched so the caller
        # can ask the user to disambiguate without surfacing peer data.
        for r in own:
            r.setdefault("org", resolved_org or "")
        return own

    for peer in sorted(resolve_peers(resolved_org, peers)):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        hit = peer_db.resolve_source_strict(source_id)
        if hit is None:
            continue
        if isinstance(hit, dict):
            if hit.get("publication_state") not in PEER_VISIBLE_STATES:
                continue
            hit["org"] = peer
            return hit
        visible = [
            h for h in hit
            if h.get("publication_state") in PEER_VISIBLE_STATES
        ]
        if not visible:
            continue
        for h in visible:
            h["org"] = peer
        if len(visible) == 1:
            return visible[0]
        return visible
    return None


def get_attachment(
    attachment_id: str,
    *,
    org: str | None = None,
    peers: list[str] | None = None,
) -> dict | None:
    """Get an attachment row by ID prefix — own-first, then peers.

    Own-org returns the attachment unconditionally. Peer attachments are
    only visible when their parent source's ``publication_state`` is in
    :data:`PEER_VISIBLE_STATES` (orphan attachments with no ``source_id``
    are skipped cross-org — their visibility can't be derived).

    Scopeless callers scan every org as own-surface; no parent-state
    filter (operator UI sees all).
    """
    resolved_org = _resolve_org(org)

    if _global_scope_active(resolved_org, None):
        return _scan_orgs_for_row(lambda d: d.get_attachment(attachment_id))

    db = _open(org)
    try:
        att = db.get_attachment(attachment_id)
    finally:
        db.close()
    if att is not None:
        att.setdefault("org", resolved_org or "")
        return att

    for peer in sorted(resolve_peers(resolved_org, peers)):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        att = peer_db.get_attachment(attachment_id)
        if att is None:
            continue
        parent_id = att.get("source_id")
        if not parent_id:
            # Orphan attachment cross-org: no way to derive visibility.
            continue
        parent = peer_db.get_source(parent_id)
        if parent is None:
            continue
        if parent.get("publication_state") not in PEER_VISIBLE_STATES:
            continue
        att["org"] = peer
        return att
    return None


def list_attachments(
    source_id: str | None = None,
    *,
    org: str | None = None,
    peers: list[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    """List attachments, optionally filtered by source_id.

    Scopeless callers union across every org DB.
    """
    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        return _union_across_orgs(
            lambda d: d.list_attachments(source_id=source_id, limit=limit)
        )
    db = _open(org)
    try:
        return db.list_attachments(source_id=source_id, limit=limit)
    finally:
        db.close()


_CROSS_ORG_LIST_SLOP = 10


def list_sources(
    *,
    org: str | None = None,
    peers: list[str] | None = None,
    only_org: str | None = None,
    limit: int = 50,
    project: str | None = None,
    source_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    author: str | None = None,
    tags: list[str] | None = None,
    states: list[str] | None = None,
    include_raw: bool = False,
    session_source_ids: list[str] | None = None,
    session_author_pattern: str | None = None,
) -> list[dict]:
    """List sources, merged chronologically across own + peers.

    Per graph://bcce359d-a1d § Merge algorithms: fetch ``limit + slop``
    rows from each DB, merge-sort by ``created_at`` DESC, truncate to
    limit. Each row is annotated with ``org`` (origin slug). Peer DBs
    are always clamped to ``published``/``canonical``.

    ``only_org`` pins the listing to a single org (own slug or a peer).
    """
    resolved_org = _resolve_org(org)
    per_db_limit = limit + _CROSS_ORG_LIST_SLOP

    if _global_scope_active(resolved_org, only_org):
        # GLOBAL caller: list every org as own-surface, no peer filter,
        # chronological merge across all of them. Pooled handles — don't
        # close them.
        merged: list[dict] = []
        for slug, slug_db in _iter_org_dbs():
            rows = slug_db.list_sources(
                project=project, source_type=source_type, limit=per_db_limit,
                since=since, until=until, author=author, tags=tags,
                states=states, include_raw=include_raw,
                session_source_ids=session_source_ids,
                session_author_pattern=session_author_pattern,
            )
            for r in rows:
                r["org"] = slug
            merged.extend(rows)
        merged.sort(key=lambda r: (r.get("created_at") or ""), reverse=True)
        return merged[:limit]

    if only_org is not None:
        if only_org == resolved_org:
            db = _open(org)
            try:
                rows = db.list_sources(
                    project=project, source_type=source_type, limit=limit,
                    since=since, until=until, author=author, tags=tags,
                    states=states, include_raw=include_raw,
                    session_source_ids=session_source_ids,
                    session_author_pattern=session_author_pattern,
                )
            finally:
                db.close()
            for r in rows:
                r.setdefault("org", resolved_org or "")
            return rows
        peer_db = open_peer_db(only_org)
        if peer_db is None:
            return []
        rows = peer_db.list_sources(
            project=project, source_type=source_type, limit=limit,
            since=since, until=until, author=author, tags=tags,
            states=list(PEER_VISIBLE_STATES),
        )
        for r in rows:
            r.setdefault("org", only_org)
        return rows

    resolved_peers = resolve_peers(resolved_org, peers)

    def fetch_own(db: GraphDB) -> list[dict]:
        return db.list_sources(
            project=project, source_type=source_type, limit=per_db_limit,
            since=since, until=until, author=author, tags=tags,
            states=states, include_raw=include_raw,
            session_source_ids=session_source_ids,
            session_author_pattern=session_author_pattern,
        )

    def fetch_peer(db: GraphDB, _slug: str) -> list[dict]:
        return db.list_sources(
            project=project, source_type=source_type, limit=per_db_limit,
            since=since, until=until, author=author, tags=tags,
            states=list(PEER_VISIBLE_STATES),
        )

    org_lists = run_across_orgs(
        resolved_org, resolved_peers, fetch_own, fetch_peer,
    )
    if not resolved_peers:
        rows = org_lists[0][1] if org_lists else []
        for r in rows:
            r.setdefault("org", resolved_org or "")
        return rows[:limit]
    return chronological_merge(org_lists, limit=limit, time_field="created_at")


def list_notes(
    *,
    org: str | None = None,
    peers: list[str] | None = None,
    only_org: str | None = None,
    since: str | None = None,
    tags: list[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    """List note-type sources (chronological, optional time/tag filters).

    Peers only contribute ``published``/``canonical`` rows — raw notes
    (pitfalls, everyday session notes) stay org-internal. This is the
    "ship-hot-takes-to-peers only when curated" contract.
    """
    return list_sources(
        org=org,
        peers=peers,
        only_org=only_org,
        limit=limit,
        source_type="note",
        since=since,
        tags=tags,
    )


def list_collab_sources(
    *,
    org: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List collab-tagged sources ranked by activity.

    Scopeless callers union across every org DB.
    """
    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        return _union_across_orgs(lambda d: d.list_collab_sources(limit=limit))
    db = _open(org)
    try:
        return db.list_collab_sources(limit=limit)
    finally:
        db.close()


def list_collab_topics(
    *,
    org: str | None = None,
) -> list[dict]:
    """List tag taxonomy entries (name + description + counts).

    Today returns rows from the ``tags`` table. Empty list if table absent.
    Scopeless callers union across every org DB.
    """
    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        return _union_across_orgs(lambda d: d.list_tags(limit=200))
    db = _open(org)
    try:
        return db.list_tags(limit=200)
    finally:
        db.close()


def list_attention(
    *,
    org: str | None = None,
    since: str | None = None,
    search: str | None = None,
    last: int | None = None,
    session: str | None = None,
    context: int = 0,
) -> list[dict]:
    """List human input ('attention') across sessions.

    Scopeless callers union across every org DB. Scoped callers read
    only their own org.
    """
    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        return _union_across_orgs(
            lambda d: _query_attention(
                d, since=since, search=search, last=last,
                session=session, context=context,
            )
        )
    db = _open(org)
    try:
        return _query_attention(
            db,
            since=since,
            search=search,
            last=last,
            session=session,
            context=context,
        )
    finally:
        db.close()


def get_recent_turns(
    source_id: str,
    *,
    org: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Get recent turns of a source. Used by primer/context flows.

    Scopeless callers scan every org DB for the source.
    """
    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        # Unique source IDs live in one org; scan and return the first hit's turns.
        for _slug, slug_db in _iter_org_dbs():
            if slug_db.get_source(source_id) is not None:
                return slug_db.get_recent_turns(source_id, limit=limit)
        return []
    db = _open(org)
    try:
        return db.get_recent_turns(source_id, limit=limit)
    finally:
        db.close()


def get_session(
    session_id: str,
    *,
    org: str | None = None,
) -> dict | None:
    """Get a session-type source by id or session_id metadata.

    Scopeless callers scan every org. ``session_id`` may be the source
    id, a tmux name, or a session_uuid.
    """
    def _probe(db: GraphDB) -> dict | None:
        src = db.get_source(session_id)
        if src and src.get("type") == "session":
            return src
        row = db.conn.execute(
            "SELECT * FROM sources WHERE type = 'session' AND ("
            "  json_extract(metadata, '$.session_id') = ?"
            "  OR json_extract(metadata, '$.session_uuid') = ?"
            "  OR json_extract(metadata, '$.tmux_session') = ?"
            ") LIMIT 1",
            (session_id, session_id, session_id),
        ).fetchone()
        return dict(row) if row else None

    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        return _scan_orgs_for_row(_probe)
    db = _open(org)
    try:
        # session_id may be the source id, a tmux name, or a session_uuid.
        # Try direct source first; fall back to metadata lookup.
        src = db.get_source(session_id)
        if src and src.get("type") == "session":
            return src
        row = db.conn.execute(
            "SELECT * FROM sources WHERE type = 'session' AND ("
            "  json_extract(metadata, '$.session_id') = ?"
            "  OR json_extract(metadata, '$.session_uuid') = ?"
            "  OR json_extract(metadata, '$.tmux_session') = ?"
            ") LIMIT 1",
            (session_id, session_id, session_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


def stream_get(
    tag: str,
    *,
    org: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Return notes tagged with ``tag`` as a chronological feed.

    Each item carries the projection the dashboard streams view expects:
    id, title (cleaned), created_at, author, tags, source_type, preview.
    """
    sources = list_sources(
        org=org,
        tags=[tag],
        limit=limit,
        source_type="note",
    )
    items = []
    for s in sources:
        meta = s.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        raw_title = s.get("title") or ""
        clean_title = raw_title.lstrip("# ").split("\n")[0][:80]
        items.append({
            "id": s["id"],
            "title": clean_title,
            "created_at": s.get("created_at", ""),
            "author": meta.get("author", ""),
            "tags": meta.get("tags", []),
            "source_type": s.get("type", "note"),
            "preview": raw_title[:200],
        })
    return items[offset:]


def count_active_streams(
    *,
    org: str | None = None,
) -> int:
    """Return the count of distinct tags across all notes.

    Scopeless callers union the tag set across every org (counting
    distinct tags post-union, not summing per-org).
    """
    sql = (
        "SELECT DISTINCT value AS tag "
        "FROM sources, json_each(json_extract(sources.metadata, '$.tags')) "
        "WHERE sources.type = 'note'"
    )
    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        tags: set[str] = set()
        for _slug, slug_db in _iter_org_dbs():
            for r in slug_db.conn.execute(sql):
                t = r["tag"]
                if t:
                    tags.add(t)
        return len(tags)
    db = _open(org)
    try:
        rows = db.conn.execute(sql).fetchall()
        return len({r["tag"] for r in rows if r["tag"]})
    finally:
        db.close()


def streams_summary(
    *,
    org: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Summarize active tag streams: tag, count, description, last_active.

    Scopeless callers aggregate across every org (count and last_active
    are summed / taken-as-max over all orgs; description wins by
    first-seen).
    """
    def _collect(db: GraphDB) -> tuple[list, dict[str, str]]:
        rows = db.conn.execute(
            """SELECT metadata, created_at FROM sources
               WHERE type = 'note' AND json_extract(metadata, '$.tags') IS NOT NULL"""
        ).fetchall()
        descs: dict[str, str] = {}
        try:
            for r2 in db.conn.execute("SELECT name, description FROM tags"):
                descs[r2["name"]] = r2["description"] or ""
        except Exception:
            pass
        return [dict(r) for r in rows], descs

    resolved_org = _resolve_org(org)
    tag_desc: dict[str, str] = {}
    rows: list = []
    if _global_scope_active(resolved_org, None):
        for _slug, slug_db in _iter_org_dbs():
            r_rows, r_desc = _collect(slug_db)
            rows.extend(r_rows)
            for k, v in r_desc.items():
                tag_desc.setdefault(k, v)
    else:
        db = _open(org)
        try:
            rows, tag_desc = _collect(db)
        finally:
            db.close()

    tag_counts: dict[str, int] = {}
    tag_last: dict[str, str] = {}
    for r in rows:
        try:
            meta = json.loads(r["metadata"]) if isinstance(r["metadata"], str) else (r["metadata"] or {})
            created = r["created_at"] or ""
            for t in meta.get("tags", []):
                if isinstance(t, str):
                    tag_counts[t] = tag_counts.get(t, 0) + 1
                    if created > tag_last.get(t, ""):
                        tag_last[t] = created
        except (json.JSONDecodeError, TypeError):
            continue
    streams = sorted(tag_counts.items(), key=lambda x: -x[1])[:limit]
    return [
        {"tag": t, "count": c, "description": tag_desc.get(t, ""), "last_active": tag_last.get(t, "")}
        for t, c in streams
    ]


def resolve_embed(
    embed_id: str,
    *,
    org: str | None = None,
    version: str | None = None,
) -> dict | None:
    """Resolve a ![[id]] embed reference for the dashboard renderer.

    Returns a dict describing the embed (rich-content note, plain note, or
    attachment), or ``None`` if not found. See ``api_resolve_embed`` for the
    response shape.

    Scopeless callers scan every org DB; embed IDs are UUIDs so the
    first hit wins.
    """
    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        # Find which org owns the source or attachment; then call ourselves
        # pinned to that org so the rich-content / version lookup runs
        # against the correct DB.
        for slug, slug_db in _iter_org_dbs():
            if slug_db.get_source(embed_id) is not None or slug_db.get_attachment(embed_id) is not None:
                return resolve_embed(embed_id, org=slug, version=version)
        return None
    db = _open(org)
    try:
        source = db.get_source(embed_id)
        if source:
            meta = source.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            if meta.get("rich_content"):
                if version:
                    versioned_key = f"{source['id']}@{version}"
                else:
                    row = db.conn.execute(
                        "SELECT * FROM attachments WHERE source_id LIKE ? ORDER BY created_at DESC LIMIT 1",
                        (f"{source['id']}@%",),
                    ).fetchone()
                    versioned_key = dict(row)["source_id"] if row else None
                att = None
                if versioned_key:
                    att_row = db.conn.execute(
                        "SELECT * FROM attachments WHERE source_id = ? LIMIT 1",
                        (versioned_key,),
                    ).fetchone()
                    if att_row:
                        att = dict(att_row)
                entries = db.get_source_content(source["id"])
                alt_text = entries[0]["content"] if entries else ""
                return {
                    "type": "rich-content",
                    "id": source["id"],
                    "title": source.get("title", ""),
                    "attachment_url": f"/api/attachment/{att['id'][:12]}" if att else None,
                    "alt_text": alt_text,
                    "mime_type": "text/html",
                }
            entries = db.get_source_content(source["id"])
            content = entries[0]["content"] if entries else ""
            return {
                "type": "note",
                "id": source["id"],
                "title": source.get("title", ""),
                "content": content,
            }
        att = db.get_attachment(embed_id)
        if att:
            return {
                "type": "attachment",
                "id": att["id"],
                "filename": att["filename"],
                "attachment_url": f"/api/attachment/{att['id'][:12]}",
                "alt_text": att.get("alt_text") or "",
                "mime_type": att.get("mime_type") or "application/octet-stream",
            }
        return None
    finally:
        db.close()


def list_journal_entries(
    *,
    org: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List journal-type sources with the dashboard's three-zoom-level shape.

    Scopeless callers union across every org DB.
    """
    def _entries(db: GraphDB) -> list[dict]:
        sources = db.list_sources(source_type="journal", since=since, limit=limit)
        out = []
        for s in sources:
            meta = s.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            if not isinstance(meta, dict):
                meta = {}
            rows = db.conn.execute(
                "SELECT content FROM thoughts WHERE source_id = ? AND turn_number = 1",
                (s["id"],),
            ).fetchall()
            normal = rows[0]["content"] if rows else ""
            out.append({
                "id": s["id"],
                "compact": s.get("title", ""),
                "normal": normal,
                "expanded": meta.get("expanded", ""),
                "timestamp_start": meta.get("timestamp_start", ""),
                "timestamp_end": meta.get("timestamp_end", ""),
                "entry_type": meta.get("entry_type", ""),
                "created_at": s.get("created_at", ""),
            })
        return out

    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        return _union_across_orgs(_entries)
    db = _open(org)
    try:
        return _entries(db)
    finally:
        db.close()


def get_comment(
    comment_id: str,
    *,
    org: str | None = None,
) -> dict | None:
    """Look up a note comment by id (or id prefix).

    Scopeless callers scan every org DB.
    """
    def _probe(db: GraphDB) -> dict | None:
        row = db.conn.execute(
            "SELECT * FROM note_comments WHERE id = ? OR id LIKE ?",
            (comment_id, f"{comment_id}%"),
        ).fetchone()
        return dict(row) if row else None

    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        return _scan_orgs_for_row(_probe)
    db = _open(org)
    try:
        return _probe(db)
    finally:
        db.close()


# ── Write paths ──────────────────────────────────────────────


def _resolve_source_home(
    source_id: str,
    *,
    org: str | None,
) -> str | None:
    """Find which org owns ``source_id``. Returns slug or ``None`` if absent.

    Per graph://bcce359d-a1d § Cross-org write semantics: writes always
    target the source's home org. Scopeless callers auto-derive; explicit
    callers that mismatch raise :class:`CrossOrgWriteError`. This helper
    returns the home slug so the callers can route the write accordingly.
    """
    resolved_org = _resolve_org(org) or ""
    db = _open(org)
    try:
        if db.get_source(source_id) is not None:
            return resolved_org
    finally:
        db.close()

    for peer in sorted(resolve_peers(resolved_org or None, None)):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        if peer_db.get_source(source_id) is not None:
            return peer
    return None


def _assert_own_source(
    source_id: str,
    *,
    org: str | None,
) -> None:
    """Refuse mutations against peer-origin sources when caller is
    explicit (graph://bcce359d-a1d).

    * Scopeless caller + peer source → silently permitted (auto-derive).
    * Explicit caller + peer source → :class:`CrossOrgWriteError`.
    * Source absent everywhere → silently fall through; downstream write
      will raise its own not-found error.
    """
    resolved_org = _resolve_org(org)
    home = _resolve_source_home(source_id, org=org)
    if home is None:
        return
    # Caller-explicit mismatch → reject. Scopeless falls through so the
    # caller can redirect the write to the home DB.
    if resolved_org and home != resolved_org:
        raise CrossOrgWriteError(source_id, home)


def _write_org_for_source(
    source_id: str,
    *,
    org: str | None,
) -> str | None:
    """Compute the effective write org for mutations targeting ``source_id``.

    Mirrors the spec's cross-org write semantics:

    * Explicit caller mismatching the source's home → raise.
    * Scopeless caller + peer source → peer slug (auto-derive).
    * Own-org source → caller slug (or ``None`` for pinned ``GRAPH_DB``).
    * Source absent everywhere → return caller's slug unchanged so the
      downstream write raises its own not-found.
    """
    resolved_org = _resolve_org(org)
    home = _resolve_source_home(source_id, org=org)
    if home is None:
        return org
    if resolved_org and home != resolved_org:
        raise CrossOrgWriteError(source_id, home)
    # Scopeless caller: auto-derive to home. Own-org hit: stick with caller.
    if not resolved_org and home:
        return home
    return org


def _assert_own_comment(
    comment_id: str,
    *,
    org: str | None,
) -> None:
    """Same rule as :func:`_assert_own_source` but for note comments."""
    resolved_org = _resolve_org(org)
    db = _open(org)
    try:
        row = db.conn.execute(
            "SELECT 1 FROM note_comments WHERE id = ? OR id LIKE ? LIMIT 1",
            (comment_id, f"{comment_id}%"),
        ).fetchone()
        if row is not None:
            return
    finally:
        db.close()

    for peer in sorted(resolve_peers(resolved_org, None)):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        peer_row = peer_db.conn.execute(
            "SELECT 1 FROM note_comments WHERE id = ? OR id LIKE ? LIMIT 1",
            (comment_id, f"{comment_id}%"),
        ).fetchone()
        if peer_row is not None:
            raise CrossOrgWriteError(comment_id, peer)


def add_tag(
    source_id: str,
    tag: str,
    *,
    org: str | None = None,
) -> bool:
    """Add a tag to a source. Returns True if newly added, False if already present.

    Scopeless caller: auto-derive write to the source's home org.
    Explicit caller + peer source: :class:`CrossOrgWriteError`.
    """
    write_org = _write_org_for_source(source_id, org=org)
    db = _open(write_org)
    try:
        return db.add_source_tag(source_id, tag)
    finally:
        db.close()


def remove_tag(
    source_id: str,
    tag: str,
    *,
    org: str | None = None,
) -> bool:
    """Remove a tag from a source. Returns True if removed, False if not present.

    Cross-org semantics identical to :func:`add_tag`.
    """
    write_org = _write_org_for_source(source_id, org=org)
    db = _open(write_org)
    try:
        return db.remove_source_tag(source_id, tag)
    finally:
        db.close()


def add_comment(
    source_id: str,
    content: str,
    *,
    org: str | None = None,
    actor: str = "user",
) -> dict:
    """Add a comment to a note. Returns the inserted row.

    Scopeless caller: auto-derive to the note's home org (so comments
    land where the note author can see them). Explicit caller + peer note:
    :class:`CrossOrgWriteError`.
    """
    write_org = _write_org_for_source(source_id, org=org)
    db = _open(write_org)
    try:
        return db.insert_comment(source_id, content, actor=actor)
    finally:
        db.close()


def integrate_comment(
    comment_id: str,
    *,
    org: str | None = None,
) -> bool:
    """Mark a comment as integrated. Returns True if state changed.

    Peer-origin comments raise :class:`CrossOrgWriteError`.
    """
    _assert_own_comment(comment_id, org=org)
    db = _open(org)
    try:
        return db.integrate_comment(comment_id)
    finally:
        db.close()


def tag_merge(
    from_tag: str,
    to_tag: str,
    *,
    org: str | None = None,
    reason: str = "",
    force: bool = False,
) -> dict:
    """Merge ``from_tag`` into ``to_tag``.

    Drops the tag from every source, applies the target tag, writes a
    merge-log note, marks the deprecated tag in the tags table.

    Returns ``{"ok": True, "count": N, "note_id": str}`` on success, or
    ``{"error": str, "status": int}`` on validation failure.
    """
    from .models import Source, Thought, new_id  # local import: writes only
    db = _open(org)
    try:
        from_sources = db.sources_with_tag(from_tag)
        to_sources = db.sources_with_tag(to_tag)
        from_count = len(from_sources)
        to_count = len(to_sources)
        if from_count == 0:
            return {"error": f"no sources tagged '{from_tag}'", "status": 404}
        if from_count > to_count and not force:
            return {
                "error": (
                    f"'{from_tag}' has {from_count} sources, '{to_tag}' has "
                    f"{to_count}. Use force=true to merge majority into minority."
                ),
                "status": 409,
            }
        retagged = 0
        for src in from_sources:
            db.remove_source_tag(src["id"], from_tag)
            db.add_source_tag(src["id"], to_tag)
            retagged += 1
        note_text = f"Tag merge: {from_tag} → {to_tag}\nRetagged {retagged} sources.\n"
        if reason:
            note_text += f"Reason: {reason}\n"
        note_source = Source(
            type="note",
            platform="local",
            project="autonomy",
            title=f"Tag merge: {from_tag} → {to_tag}",
            file_path=f"note:{new_id()}",
            metadata={"tags": ["taxonomy", "tag-merge"], "author": "api"},
        )
        db.insert_source(note_source)
        db.insert_thought(Thought(
            source_id=note_source.id,
            content=note_text,
            role="user",
            turn_number=1,
            tags=["taxonomy", "tag-merge"],
        ))
        db.update_tag_description(
            from_tag,
            f"Deprecated — see graph://{note_source.id[:12]}",
            actor="api",
        )
        db.commit()
        return {"ok": True, "count": retagged, "note_id": note_source.id}
    finally:
        db.close()


def update_tag_description(
    tag_name: str,
    description: str,
    *,
    org: str | None = None,
    actor: str = "user",
) -> bool:
    """Set or update a tag description in the tags table."""
    db = _open(org)
    try:
        return db.update_tag_description(tag_name, description, actor=actor)
    finally:
        db.close()


def insert_capture(
    capture_id: str,
    content: str,
    *,
    org: str | None = None,
    source_id: str | None = None,
    turn_number: int | None = None,
    thread_id: str | None = None,
    actor: str = "user",
) -> None:
    """Insert a thought capture. ``thread_id`` must be the full UUID."""
    db = _open(org)
    try:
        db.insert_capture(
            capture_id, content,
            source_id=source_id,
            turn_number=turn_number,
            thread_id=thread_id,
            actor=actor,
        )
    finally:
        db.close()


def insert_thread(
    thread_id: str,
    title: str,
    *,
    org: str | None = None,
    priority: int = 1,
    created_by: str = "user",
) -> None:
    """Insert a new thought thread."""
    db = _open(org)
    try:
        db.insert_thread(thread_id, title, priority=priority, created_by=created_by)
    finally:
        db.close()


def update_thread_status(
    thread_id: str,
    status: str,
    *,
    org: str | None = None,
) -> dict | None:
    """Update a thread's status. Returns the updated thread row (for title display)."""
    db = _open(org)
    try:
        db.update_thread_status(thread_id, status)
        return db.get_thread(thread_id)
    finally:
        db.close()


def assign_capture_to_thread(
    capture_id: str,
    thread_id: str,
    *,
    org: str | None = None,
) -> None:
    """Assign a capture to a thread."""
    db = _open(org)
    try:
        db.assign_capture_to_thread(capture_id, thread_id)
    finally:
        db.close()


def get_thread(
    thread_id: str,
    *,
    org: str | None = None,
) -> dict | None:
    """Get a thread by id (or id prefix).

    Scopeless callers scan every org DB.
    """
    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        return _scan_orgs_for_row(lambda d: d.get_thread(thread_id))
    db = _open(org)
    try:
        return db.get_thread(thread_id)
    finally:
        db.close()


def thread_action(
    action: str,
    thread_id: str,
    *,
    target: str | None = None,
    org: str | None = None,
) -> dict:
    """Apply a thread action: ``park`` / ``done`` / ``active`` update the
    thread status; ``assign`` / ``attach`` bind a capture to a thread.

    Mirrors the ``POST /api/graph/thread/action`` dashboard endpoint so
    CLI handlers can call ``get_client().thread_action(...)`` uniformly.
    """
    if action in ("park", "done", "active"):
        status = "parked" if action == "park" else action
        update_thread_status(thread_id, status, org=org)
        return {"ok": True, "action": action, "thread_id": thread_id}
    if action in ("assign", "attach"):
        if not target:
            raise ValueError(f"{action} requires a target thread id")
        assign_capture_to_thread(thread_id, target, org=org)
        return {"ok": True, "action": action, "capture_id": thread_id, "thread_id": target}
    raise ValueError(f"unknown thread action: {action!r}")


def list_captures(
    *,
    org: str | None = None,
    thread_id: str | None = None,
    status: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List thought captures with optional filters.

    Scopeless callers union across every org DB.
    """
    def _query(db: GraphDB) -> list[dict]:
        try:
            return db.list_captures(
                thread_id=thread_id, status=status, since=since, limit=limit,
            )
        except Exception:
            return []  # captures table may not exist on older DBs

    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        return _union_across_orgs(_query)
    db = _open(org)
    try:
        return _query(db)
    finally:
        db.close()


def list_threads(
    *,
    org: str | None = None,
    status: str | None = "active",
    include_all: bool = False,
    limit: int = 20,
) -> list[dict]:
    """List threads with optional status filter. ``include_all=True``
    overrides ``status`` and returns every status.

    Scopeless callers union across every org DB.
    """
    if include_all:
        status = None

    def _query(db: GraphDB) -> list[dict]:
        try:
            return db.list_threads(status=status, limit=limit)
        except Exception:
            return []  # threads table may not exist on older DBs

    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        return _union_across_orgs(_query)
    db = _open(org)
    try:
        return _query(db)
    finally:
        db.close()


def update_source_title(
    source_id: str,
    title: str,
    *,
    org: str | None = None,
) -> None:
    """Update a source title. Last write wins.

    Cross-org: scopeless caller auto-derives; explicit caller mismatch
    raises :class:`CrossOrgWriteError`.
    """
    write_org = _write_org_for_source(source_id, org=org)
    db = _open(write_org)
    try:
        db.update_source_title(source_id, title)
    finally:
        db.close()


# ── Publication state transitions ────────────────────────────
# Narrow primitive that backs the bootstrap curation runner
# (tools/graph/curation/) and will back the ``graph promote`` CLI from
# auto-j1l9 once it lands. Mirrors ``promote_setting`` shape for the Settings
# primitive (see ``settings_ops.promote_setting``).

_SOURCE_VALID_STATES = ("raw", "curated", "published", "canonical")


def promote_source(
    source_id: str,
    to_state: str,
    *,
    org: str | None = None,
) -> dict:
    """Transition ``sources.publication_state`` for the given id.

    Returns a transition record ``{id, prev_state, new_state, ts}``. Raises
    ``ValueError`` on invalid state, ``LookupError`` on unknown id,
    :class:`CrossOrgWriteError` on peer-origin targets.
    """
    if to_state not in _SOURCE_VALID_STATES:
        raise ValueError(
            f"invalid publication_state {to_state!r}; valid: {_SOURCE_VALID_STATES}"
        )
    db = _open(org)
    try:
        row = db.conn.execute(
            "SELECT id, publication_state FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        if not row:
            # Distinguish peer-origin from not-found so operators get a
            # useful message ("act in origin org X") instead of bare 404.
            resolved_org = _resolve_org(org)
            for peer in sorted(resolve_peers(resolved_org, None)):
                peer_db = open_peer_db(peer)
                if peer_db is None:
                    continue
                if peer_db.conn.execute(
                    "SELECT 1 FROM sources WHERE id = ?", (source_id,)
                ).fetchone() is not None:
                    raise CrossOrgWriteError(source_id, peer)
            raise LookupError(f"source not found: {source_id!r}")
        prev = row["publication_state"]
        now = db.conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
        ).fetchone()[0]
        if prev == to_state:
            return {"id": row["id"], "prev_state": prev, "new_state": to_state, "ts": now,
                    "changed": False}
        db.conn.execute(
            "UPDATE sources SET publication_state = ? WHERE id = ?",
            (to_state, row["id"]),
        )
        db.conn.commit()
        return {"id": row["id"], "prev_state": prev, "new_state": to_state, "ts": now,
                "changed": True}
    finally:
        db.close()


def checkpoint(
    *,
    org: str | None = None,
) -> None:
    """Force a WAL checkpoint so immutable=1 readers see current writes.

    Best-effort — silently swallows errors (read-only mounts, etc.).
    """
    try:
        db = _open(org)
        try:
            db.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        finally:
            db.close()
    except Exception:
        pass


# ── Note create / update (migrated from cli.py cmd_note / cmd_note_update) ──


def _store_attachment_db(
    db: GraphDB,
    file_path_str: str,
    *,
    source_id: str | None = None,
    turn_number: int | None = None,
    alt_text: str | None = None,
    original_filename: str | None = None,
):
    """Hash, dedup, store a file and insert an attachment record.

    Returns the ``Attachment`` dataclass, or raises :class:`FileNotFoundError`
    when the source path does not exist. Shared by :func:`attach_file`,
    :func:`create_note`, and :func:`update_note` so all three use the same
    dedup semantics.

    ``original_filename`` overrides the on-disk basename — needed when
    callers stream uploads into a tempfile and want to preserve the
    caller-supplied name (e.g. dashboard multipart handlers).
    """
    import hashlib
    import mimetypes
    import shutil
    from .models import Attachment

    file_path = Path(file_path_str)
    if not file_path.is_file():
        raise FileNotFoundError(f"{file_path} not found or not a file")

    file_data = file_path.read_bytes()
    file_hash = hashlib.sha256(file_data).hexdigest()
    size_bytes = len(file_data)
    filename = original_filename or file_path.name

    existing = db.get_attachment_by_hash(file_hash)
    if existing:
        updates, params = [], []
        if source_id and not existing.get("source_id"):
            updates.append("source_id = ?")
            params.append(source_id)
        if alt_text and not existing.get("alt_text"):
            updates.append("alt_text = ?")
            params.append(alt_text)
        if updates:
            params.append(existing["id"])
            db.conn.execute(
                f"UPDATE attachments SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            db.conn.commit()
        return Attachment(
            id=existing["id"], hash=file_hash, filename=filename,
            mime_type=existing.get("mime_type"), size_bytes=size_bytes,
            file_path=existing["file_path"],
            source_id=source_id or existing.get("source_id"),
            alt_text=alt_text or existing.get("alt_text"),
        )

    mime_type, _ = mimetypes.guess_type(filename)
    ext = file_path.suffix or ""
    store_dir = db.db_path.parent.parent / "attachments" / file_hash[:2]
    store_path = store_dir / f"{file_hash}{ext}"
    store_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(file_path), str(store_path))

    att = Attachment(
        hash=file_hash,
        filename=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        file_path=str(store_path),
        source_id=source_id,
        turn_number=int(turn_number) if turn_number else None,
        alt_text=alt_text,
    )
    db.insert_attachment(att)
    return att


def create_note(
    content: str,
    *,
    tags: list[str] | None = None,
    author: str | None = None,
    project: str | None = None,
    attachments: list[str] | None = None,
    html_path: str | None = None,
    auto_provenance_source_id: str | None = None,
    auto_provenance_turn: int | None = None,
    org: str | None = None,
) -> dict:
    """Create a note source + turn-1 thought in ``org``'s DB.

    Returns a dict with ``id``, ``source_id`` (same as ``id``), ``title``,
    ``org``, ``lines``, ``chars``, ``attachments`` (list of attachment
    dicts), ``rich_content`` (bool), ``auto_provenance`` (optional dict
    ``{source_id, turn}``). Mirrors :func:`cli.cmd_note` output fields so
    callers can render the same echo lines.

    ``html_path`` enables rich-content mode — the HTML is stored as a
    version-paired attachment keyed by ``<source-id>@1``. ``attachments``
    files are stored and ``{1}``/``{2}`` positional placeholders in
    ``content`` are rewritten to ``graph://<id>`` markers.
    """
    from .models import Source, Thought, Edge, new_id
    from .ingest import extract_entities

    tags = list(tags or [])
    is_rich = bool(html_path)
    meta: dict = {"tags": tags, "author": author or os.environ.get("BD_ACTOR", "user")}
    if is_rich:
        meta["rich_content"] = True

    source_key = f"note:{new_id()}"
    # publication_state explicit even though the Source dataclass default is
    # "curated" — long-running server processes may have imported an older
    # Source class, and the intent ("graph note is cross-session visible")
    # shouldn't hinge on process restart.
    source = Source(
        type="note",
        platform="local",
        project=project or "autonomy",
        title=content[:80],
        file_path=source_key,
        metadata=meta,
        publication_state="curated",
    )

    db = _open(org)
    try:
        db.insert_source(source)

        att_records: list[dict] = []

        if is_rich:
            html_att = _store_attachment_db(
                db, html_path, source_id=f"{source.id}@1",
            )
            att_records.append({
                "id": html_att.id, "filename": html_att.filename,
                "kind": "html", "source_id": f"{source.id}@1",
            })

        if attachments:
            att_ids = []
            for fp in attachments:
                att = _store_attachment_db(db, fp, source_id=source.id)
                att_ids.append(att.id)
                att_records.append({
                    "id": att.id, "filename": att.filename,
                    "kind": "file", "source_id": source.id,
                })
            for i, att_id in enumerate(att_ids, 1):
                content = content.replace(
                    '{' + str(i) + '}', f'graph://{att_id[:12]}',
                )

        thought = Thought(
            source_id=source.id,
            content=content,
            role="user",
            turn_number=1,
            tags=tags,
        )
        db.insert_thought(thought)
        db.insert_note_version(source.id, 1, content)

        for name, etype in extract_entities(content):
            eid = db.upsert_entity(name, etype)
            db.add_mention(eid, thought.id, "thought")

        if auto_provenance_source_id and auto_provenance_turn:
            db.insert_edge(Edge(
                source_id=source.id,
                source_type="source",
                target_id=auto_provenance_source_id,
                target_type="source",
                relation="conceived_at",
                metadata={
                    "turns": {
                        "from": auto_provenance_turn,
                        "to": auto_provenance_turn,
                    },
                },
            ))

        db.commit()
    finally:
        db.close()

    lines = content.count("\n") + (1 if content else 0)
    return {
        "id": source.id,
        "source_id": source.id,
        "title": source.title,
        "org": _resolve_org(org) or "",
        "lines": lines,
        "chars": len(content),
        "content": content,
        "attachments": att_records,
        "rich_content": is_rich,
        "auto_provenance": (
            {"source_id": auto_provenance_source_id, "turn": auto_provenance_turn}
            if auto_provenance_source_id and auto_provenance_turn else None
        ),
    }


def update_note(
    source_id: str,
    content: str,
    *,
    integrate_comments: list[str] | None = None,
    attachments: list[str] | None = None,
    html_path: str | None = None,
    org: str | None = None,
) -> dict:
    """Append a new version to an existing note.

    Cross-org resolves ``source_id``; refuses peer-origin targets with
    :class:`CrossOrgWriteError`. Returns a dict with ``new_version``,
    ``source_id``, ``org``, ``lines``, ``chars``, ``integrated``
    (list of integrated comment ids), ``rich_content`` (post-update),
    ``attachments`` (list of new attachment records).

    Rich-content notes (``metadata.rich_content == True``) require
    ``html_path``; the dual HTML/markdown update keeps the version
    pair consistent.
    """
    from .ingest import extract_entities

    # Resolve source cross-org. Scopeless callers auto-derive the write
    # target to the source's home org. Callers with an explicit caller
    # org that mismatches the source's home raise CrossOrgWriteError
    # (per graph://bcce359d-a1d § Cross-org write semantics).
    caller = _resolve_org(org) or ""

    # Try own-org first; if that misses, scan peers (including raw rows
    # so the auto-derive path works even for internal-state peer notes).
    own_db = _open(org)
    try:
        resolved: dict | None = own_db.get_source(source_id)
    finally:
        own_db.close()

    origin_org = caller if resolved else ""
    if resolved is None:
        for peer in sorted(resolve_peers(caller or None, None)):
            peer_db = open_peer_db(peer)
            if peer_db is None:
                continue
            hit = peer_db.get_source(source_id)
            if hit is not None:
                resolved = hit
                origin_org = peer
                break

    if resolved is None:
        raise LookupError(f"No source found matching '{source_id}'")

    # Explicit-mismatch → refuse. Scopeless caller falls through and writes
    # to the source's home.
    if caller and origin_org and origin_org != caller:
        raise CrossOrgWriteError(resolved["id"], origin_org)

    if resolved.get("type") != "note":
        raise ValueError(
            f"can only update notes (source is type {resolved.get('type')!r})"
        )

    src_id = resolved["id"]
    meta = resolved.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    is_rich = bool(meta.get("rich_content"))

    if is_rich and not html_path:
        raise ValueError(
            "rich-content note requires html_path on update "
            "(both markdown and HTML must be updated together)"
        )

    # Write to the source's home org (auto-derive). For scopeless callers
    # the resolved peer DB IS the home. Only when caller + origin are
    # identical does the explicit-org path apply.
    write_org = origin_org or None
    db = _open(write_org)
    try:
        att_records: list[dict] = []
        if attachments:
            att_ids = []
            for fp in attachments:
                att = _store_attachment_db(db, fp, source_id=src_id)
                att_ids.append(att.id)
                att_records.append({
                    "id": att.id, "filename": att.filename,
                    "kind": "file", "source_id": src_id,
                })
            for i, att_id in enumerate(att_ids, 1):
                content = content.replace(
                    '{' + str(i) + '}', f'graph://{att_id[:12]}',
                )

        thoughts = db.get_thoughts_by_source(src_id)
        if not thoughts:
            raise LookupError(f"no thought found for source {src_id[:12]}")
        thought = thoughts[0]

        current_max = db.get_max_note_version(src_id)
        if current_max == 0:
            db.insert_note_version(src_id, 1, thought["content"])
            next_version = 2
        else:
            next_version = current_max + 1

        db.insert_note_version(src_id, next_version, content)

        if html_path:
            html_att = _store_attachment_db(
                db, html_path, source_id=f"{src_id}@{next_version}",
            )
            att_records.append({
                "id": html_att.id, "filename": html_att.filename,
                "kind": "html", "source_id": f"{src_id}@{next_version}",
            })
            if not is_rich:
                meta["rich_content"] = True
                db.conn.execute(
                    "UPDATE sources SET metadata = ? WHERE id = ?",
                    (json.dumps(meta), src_id),
                )
                is_rich = True

        db.update_thought_content(thought["id"], content)
        db.conn.execute(
            "UPDATE sources SET title = ? WHERE id = ?",
            (content[:80], src_id),
        )

        for name, etype in extract_entities(content):
            eid = db.upsert_entity(name, etype)
            db.add_mention(eid, thought["id"], "thought")

        integrated: list[str] = []
        not_found: list[str] = []
        for cid in (integrate_comments or []):
            row = db.conn.execute(
                "SELECT id FROM note_comments "
                "WHERE (id = ? OR id LIKE ?) AND source_id = ?",
                (cid, f"{cid}%", src_id),
            ).fetchone()
            if row:
                db.integrate_comment(row["id"])
                integrated.append(row["id"])
            else:
                not_found.append(cid)

        db.commit()
    finally:
        db.close()

    lines = content.count("\n") + (1 if content else 0)
    return {
        "new_version": next_version,
        "source_id": src_id,
        "org": origin_org or caller or "",
        "lines": lines,
        "chars": len(content),
        "content": content,
        "integrated": integrated,
        "not_found_comments": not_found,
        "rich_content": is_rich,
        "attachments": att_records,
    }


def attach_file(
    file_path: str,
    *,
    source_id: str | None = None,
    turn_number: int | None = None,
    alt_text: str | None = None,
    original_filename: str | None = None,
    org: str | None = None,
) -> dict:
    """Store a file attachment in ``org``'s DB (hash-dedup).

    Returns a dict shaped like the ``attachments`` row: ``id``, ``hash``,
    ``filename``, ``mime_type``, ``size_bytes``, ``file_path``,
    ``source_id``, ``turn_number``, ``alt_text``. Raises
    :class:`FileNotFoundError` if the file does not exist.

    Cross-org: scopeless caller auto-derives to the source's home org;
    explicit caller mismatch raises :class:`CrossOrgWriteError`.
    Attachments travel with their source.
    """
    if source_id:
        write_org = _write_org_for_source(source_id, org=org)
    else:
        write_org = org
    db = _open(write_org)
    try:
        att = _store_attachment_db(
            db,
            file_path,
            source_id=source_id,
            turn_number=turn_number,
            alt_text=alt_text,
            original_filename=original_filename,
        )
    finally:
        db.close()
    return {
        "id": att.id,
        "hash": att.hash,
        "filename": att.filename,
        "mime_type": att.mime_type,
        "size_bytes": att.size_bytes,
        "file_path": att.file_path,
        "source_id": att.source_id,
        "turn_number": att.turn_number,
        "alt_text": att.alt_text,
    }


def create_edge(
    from_id: str,
    to_id: str,
    *,
    from_type: str = "source",
    to_type: str = "source",
    relation: str = "references",
    turns: tuple[int, int] | None = None,
    note: str | None = None,
    metadata: dict | None = None,
    org: str | None = None,
) -> dict:
    """Create a graph edge in ``org``'s DB.

    Used by :func:`cli.cmd_link` and ``api_graph_link``. Edges live
    in the caller's own org DB even when the target source is peer-origin
    — per the signpost (graph://bcce359d-a1d § Cross-org write semantics),
    edges/beads are caller-owned artifacts that may reference peer IDs.

    Returns the inserted edge row as a dict: ``id``, ``source_id``,
    ``source_type``, ``target_id``, ``target_type``, ``relation``,
    ``metadata``, ``created_at``.
    """
    from .models import Edge as _Edge

    meta = dict(metadata or {})
    if turns is not None:
        meta["turns"] = {"from": int(turns[0]), "to": int(turns[1])}
    if note:
        meta["note"] = note

    edge = _Edge(
        source_id=from_id,
        source_type=from_type,
        target_id=to_id,
        target_type=to_type,
        relation=relation,
        metadata=meta,
    )

    db = _open(org)
    try:
        db.insert_edge(edge)
        db.commit()
    finally:
        db.close()
    return {
        "id": edge.id,
        "source_id": edge.source_id,
        "source_type": edge.source_type,
        "target_id": edge.target_id,
        "target_type": edge.target_type,
        "relation": edge.relation,
        "metadata": edge.metadata,
        "created_at": edge.created_at,
    }


def get_turn_content(
    source_id: str,
    turn_number: int,
    *,
    org: str | None = None,
) -> str | None:
    """Return the content of a single turn of ``source_id`` (or None).

    Scopeless callers scan every org DB for the source.
    """
    def _probe(db: GraphDB) -> dict | None:
        row = db.conn.execute(
            "SELECT content FROM thoughts WHERE source_id = ? AND turn_number = ? "
            "LIMIT 1",
            (source_id, turn_number),
        ).fetchone()
        # Return as a dict so _scan_orgs_for_row can annotate + return it.
        return {"content": row["content"]} if row else None

    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        hit = _scan_orgs_for_row(_probe)
        return hit["content"] if hit else None
    db = _open(org)
    try:
        row = db.conn.execute(
            "SELECT content FROM thoughts WHERE source_id = ? AND turn_number = ? "
            "LIMIT 1",
            (source_id, turn_number),
        ).fetchone()
        return row["content"] if row else None
    finally:
        db.close()


def stats(
    *,
    org: str | None = None,
) -> dict:
    """Return DB statistics (table counts).

    Scopeless callers sum counts across every org DB.
    """
    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        merged: dict[str, int] = {}
        for _slug, slug_db in _iter_org_dbs():
            for k, v in slug_db.stats().items():
                if isinstance(v, int):
                    merged[k] = merged.get(k, 0) + v
                else:
                    merged.setdefault(k, v)
        return merged
    db = _open(org)
    try:
        return db.stats()
    finally:
        db.close()


def get_tree(
    root: str | None = None,
    *,
    depth: int = 3,
    org: str | None = None,
) -> list[dict]:
    """Return hierarchy tree nodes rooted at ``root`` (or full tree).

    Scopeless callers union across every org DB (trees are org-local, so
    the union preserves per-org structure with each node carrying its
    origin slug).
    """
    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        return _union_across_orgs(lambda d: d.get_tree(root, depth=depth))
    db = _open(org)
    try:
        return db.get_tree(root, depth=depth)
    finally:
        db.close()


def search_entities(
    query: str,
    *,
    limit: int = 20,
    org: str | None = None,
) -> list[dict]:
    """Full-text search entities by name.

    Scopeless callers union across every org DB, then truncate to limit.
    """
    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        rows = _union_across_orgs(
            lambda d: d.search_entities(query, limit=limit)
        )
        return rows[:limit]
    db = _open(org)
    try:
        return db.search_entities(query, limit=limit)
    finally:
        db.close()


def list_entities(
    *,
    entity_type: str | None = None,
    limit: int = 20,
    org: str | None = None,
) -> list[dict]:
    """List entities, optionally filtered by type.

    Scopeless callers union across every org DB, then truncate to limit.
    """
    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        rows = _union_across_orgs(
            lambda d: d.list_entities(entity_type=entity_type, limit=limit)
        )
        return rows[:limit]
    db = _open(org)
    try:
        return db.list_entities(entity_type=entity_type, limit=limit)
    finally:
        db.close()


def entity_thoughts(
    entity_id: str,
    *,
    limit: int = 20,
    org: str | None = None,
) -> list[dict]:
    """Thoughts mentioning a given entity id.

    Scopeless callers union across every org DB.
    """
    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        rows = _union_across_orgs(lambda d: d.entity_thoughts(entity_id))
        return rows[:limit]
    db = _open(org)
    try:
        return db.entity_thoughts(entity_id)[:limit]
    finally:
        db.close()


def entity_mention_count(
    entity_id: str,
    *,
    org: str | None = None,
) -> int:
    """Total mentions of an entity across all sources.

    Scopeless callers sum counts across every org DB.
    """
    sql = "SELECT SUM(count) AS total FROM entity_mentions WHERE entity_id = ?"
    resolved_org = _resolve_org(org)
    if _global_scope_active(resolved_org, None):
        total = 0
        for _slug, slug_db in _iter_org_dbs():
            row = slug_db.conn.execute(sql, (entity_id,)).fetchone()
            if row and row["total"]:
                total += int(row["total"])
        return total
    db = _open(org)
    try:
        row = db.conn.execute(sql, (entity_id,)).fetchone()
        return int(row["total"] or 0) if row else 0
    finally:
        db.close()


def write_journal_entry(
    data: dict,
    *,
    org: str | None = None,
) -> dict:
    """Write a structured journal entry to ``org``'s DB.

    ``data`` must include ``compact``, ``normal``, ``timestamp_start``,
    ``timestamp_end``. Optional: ``expanded``, ``entry_type``, ``edges``
    (list of ``{target, relation, turn}``). Mirrors ``cli.cmd_journal_write``.

    Returns ``{"source_id": str, "edge_count": int, "org": str}``.
    """
    from .models import Source, Thought, Edge as _Edge, new_id

    for field in ("compact", "normal", "timestamp_start", "timestamp_end"):
        if field not in data:
            raise ValueError(f"missing required field: {field}")

    project = data.get("project") or "autonomy"
    source_key = f"journal:{new_id()}"
    src = Source(
        type="journal",
        platform="autonomy",
        project=project,
        title=data["compact"],
        file_path=source_key,
        metadata={
            "expanded": data.get("expanded", ""),
            "timestamp_start": data["timestamp_start"],
            "timestamp_end": data["timestamp_end"],
            "entry_type": data.get("entry_type", "attention"),
        },
        created_at=data["timestamp_start"],
    )

    db = _open(org)
    try:
        db.insert_source(src)
        db.insert_thought(Thought(
            source_id=src.id, content=data["normal"], role="user", turn_number=1,
        ))
        edge_count = 0
        for edge_data in data.get("edges", []) or []:
            target = edge_data.get("target")
            if not target:
                continue
            resolved_src = db.get_source(target)
            resolved = resolved_src["id"] if resolved_src else target
            db.insert_edge(_Edge(
                source_id=src.id,
                source_type="source",
                target_id=resolved,
                target_type="source",
                relation=edge_data.get("relation", "drew_from"),
                metadata={"turn": edge_data.get("turn")},
            ))
            edge_count += 1
        db.commit()
    finally:
        db.close()

    return {
        "source_id": src.id,
        "edge_count": edge_count,
        "org": _resolve_org(org) or "",
    }


def get_context(
    source_id: str,
    turn_number: int,
    *,
    window: int = 3,
    org: str | None = None,
) -> dict | None:
    """Return the content of ``turn_number`` plus ``window`` turns on
    either side, cross-org resolved.

    Mirrors the ``graph context`` CLI output in structured form so the
    dashboard ``/api/context/*`` handler and future consumers can stop
    parsing plain-text output.

    Returns ``None`` if the source is not found. Otherwise:

        {"source": {...}, "center_turn": int,
         "turns": [{"turn_number", "role", "content", "created_at"}, ...]}
    """
    source = get_source(source_id, org=org)
    if source is None:
        return None
    origin = source.get("org") or ""
    caller = _resolve_org(org) or ""

    if not origin or origin == caller:
        db: GraphDB | None = _open(org)
        own = True
    else:
        db = open_peer_db(origin)
        own = False
    if db is None:
        return None

    try:
        # ``derivations`` stores the model in ``model`` (not ``role``) so
        # project it to a uniform ``role`` label for the merged result.
        rows = db.conn.execute(
            """SELECT turn_number, role, content, created_at FROM thoughts
               WHERE source_id = ? AND turn_number BETWEEN ? AND ?
               UNION ALL
               SELECT turn_number, COALESCE(model, 'assistant') as role,
                      content, created_at FROM derivations
               WHERE source_id = ? AND turn_number BETWEEN ? AND ?
               ORDER BY turn_number""",
            (source["id"], turn_number - window, turn_number + window,
             source["id"], turn_number - window, turn_number + window),
        ).fetchall()
    finally:
        if own:
            db.close()

    return {
        "source": source,
        "center_turn": turn_number,
        "turns": [
            {
                "turn_number": r["turn_number"],
                "role": r["role"],
                "content": r["content"] or "",
                "created_at": r["created_at"] or "",
            }
            for r in rows
        ],
    }


def read_source_full(
    source_id: str,
    *,
    max_chars: int = 50000,
    org: str | None = None,
    peers: list[str] | None = None,
) -> dict | None:
    """Return a dashboard-ready full read of a source.

    Mirrors the ``graph read --json --first`` response shape so the
    dashboard ``/api/graph/resolve/<id>`` handler (and any other
    full-read consumer) can drop the subprocess fork:

        {"source": {...}, "entries": [{turn, role, content, created_at}, ...],
         "truncated": bool, "total_chars": int}

    Cross-org: own-org full surface first, then peer public surface.
    """
    resolved = _resolve_org(org)
    source = get_source(source_id, org=org, peers=peers)
    if source is None:
        return None
    origin = source.get("org") or ""
    caller = resolved or ""

    # Empty/matching origin → own-org DB. Peer origin → peer pool handle.
    if not origin or origin == caller:
        db: GraphDB | None = _open(org)
        own = True
    else:
        db = open_peer_db(origin)
        own = False
    if db is None:
        return None

    try:
        entries_src = db.get_source_content(source["id"])
    finally:
        if own:
            db.close()

    total_chars = 0
    truncated = False
    out_entries: list[dict] = []
    for e in entries_src:
        c = e.get("content") or ""
        remaining = max_chars - total_chars
        if remaining <= 0:
            truncated = True
            break
        if len(c) > remaining:
            c = c[:remaining]
            truncated = True
        out_entries.append({
            "turn_number": e.get("turn_number"),
            "role": e.get("role"),
            "content": c,
            "created_at": e.get("created_at"),
        })
        total_chars += len(c)

    return {
        "source": source,
        "entries": out_entries,
        "truncated": truncated,
        "total_chars": total_chars,
    }


# ── Helpers ──────────────────────────────────────────────────


def _query_attention(
    db: GraphDB,
    *,
    since: str | None = None,
    search: str | None = None,
    last: int | None = None,
    session: str | None = None,
    context: int = 0,
) -> list[dict]:
    """Internal: scan thoughts for human input across sessions."""
    conditions = [
        "s.type = 'session'",
        "s.platform = 'claude-code'",
        "t.role = 'user'",
        """(json_extract(s.metadata, '$.session_type') IN ('terminal', 'chatwith')
            OR json_extract(s.metadata, '$.session_type') IS NULL)""",
        "t.content NOT LIKE '<crosstalk %'",
        "t.content NOT LIKE '<system-%'",
        "t.content NOT LIKE '<local-command%'",
        "t.content NOT LIKE '<task-notification%'",
        "t.content NOT LIKE '<command-name>%'",
    ]
    params: list = []

    if since:
        conditions.append("t.created_at >= ?")
        params.append(since)
    if search:
        conditions.append("t.content LIKE ?")
        params.append(f"%{search}%")
    if session:
        conditions.append("json_extract(s.metadata, '$.session_id') = ?")
        params.append(session)

    where = " AND ".join(conditions)
    limit_val = last if last else 500
    limit_clause = f"LIMIT {int(limit_val)}"

    rows = db.conn.execute(f"""
        SELECT t.created_at, t.content, t.source_id, t.turn_number,
               json_extract(t.metadata, '$.queued') as is_queued,
               json_extract(s.metadata, '$.session_id') as session_name,
               json_extract(s.metadata, '$.session_type') as session_type
        FROM thoughts t
        JOIN sources s ON s.id = t.source_id
        WHERE {where}
        ORDER BY t.created_at DESC
        {limit_clause}
    """, params).fetchall()

    results = list(reversed(rows)) if last else list(rows)
    items = [
        {
            "created_at": r[0] or "",
            "content": r[1] or "",
            "source_id": r[2] or "",
            "turn_number": r[3],
            "is_queued": bool(r[4]),
            "session_name": r[5] or "host",
            "session_type": r[6] or "host",
        }
        for r in results
    ]

    if context and context > 0:
        for item in items:
            src_id = item["source_id"]
            turn = item["turn_number"]
            if turn is None:
                item["context"] = []
                continue
            derivs = db.conn.execute("""
                SELECT turn_number, content, created_at
                FROM derivations
                WHERE source_id = ? AND turn_number BETWEEN ? AND ?
                ORDER BY turn_number
            """, (src_id, turn, turn + context)).fetchall()
            item["context"] = [
                {"turn": d[0], "content": d[1] or "", "created_at": d[2] or ""}
                for d in derivs
            ]
    return items
