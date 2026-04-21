"""Graph service layer.

All graph DB access flows through this module. CLI commands (via
``client.LocalClient``) and dashboard API handlers both call into ``ops.*``;
neither imports ``GraphDB`` directly outside this layer.

Per-org write routing is live (auto-txg5.3): every write lands in the
caller's own org DB. ``caller_org`` resolution cascade:

  1. Explicit ``caller_org=`` kwarg (API handlers, tests, container-aware
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

    Exceptions (link/bead primitives) stay write-routed to caller_org
    even when the *target* source lives in a peer DB — the edge/bead
    row is owned by caller_org, so it's a local write that happens to
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


def _resolve_caller_org(caller_org: str | None) -> str | None:
    """Apply the caller_org resolution cascade (explicit → env → default).

    Returns ``None`` only when ``GRAPH_DB`` is pinned (tests/override),
    because the resolver short-circuits on that env var before it cares
    about org scope. Otherwise returns a concrete slug — explicit kwarg
    wins, then ``GRAPH_ORG`` env, then :func:`resolve_caller_db_path`
    applies the scopeless default (``personal``).
    """
    if caller_org is not None:
        return caller_org
    env_org = os.environ.get("GRAPH_ORG")
    if env_org:
        return env_org
    return None


def _db_path(caller_org: str | None = None) -> str | None:
    """Resolve graph DB path for the given ``caller_org``.

    ``GRAPH_DB`` env var wins (test/override path); otherwise routes to
    ``data/orgs/<slug>.db`` where ``slug`` is the resolved caller org
    (explicit kwarg → ``GRAPH_ORG`` env → scopeless default ``personal``).
    See :func:`db.resolve_caller_db_path`.
    """
    env_db = os.environ.get("GRAPH_DB")
    if env_db:
        return env_db
    return str(resolve_caller_db_path(_resolve_caller_org(caller_org)))


def _open(caller_org: str | None = None) -> GraphDB:
    """Open a GraphDB for ``caller_org``'s routed path.

    Returns a fresh (non-pooled) connection. Callers are expected to use
    the existing ``try: ... finally: db.close()`` pattern. Process-
    lifetime pooling is available via :meth:`GraphDB.for_org` for the
    dashboard's server-side handlers that want to amortise connection
    cost across requests.
    """
    return GraphDB(_db_path(caller_org))


# ── Read paths ───────────────────────────────────────────────


def search(
    q: str,
    *,
    caller_org: str | None = None,
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
    resolved_org = _resolve_caller_org(caller_org)

    # Single-org pin: skip peer resolution entirely.
    if only_org is not None:
        if only_org == resolved_org:
            db = _open(caller_org)
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
    caller_org: str | None = None,
    peers: list[str] | None = None,
) -> dict | None:
    """Resolve a source by exact ID or prefix, own-first then peers.

    Own-org is queried first with the full surface. If no hit, every
    subscribed peer is queried in alphabetical order with the public
    surface filter (``published``/``canonical`` only). First hit wins;
    the returned dict carries an ``org`` field identifying the origin.
    """
    resolved_org = _resolve_caller_org(caller_org)
    db = _open(caller_org)
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
    caller_org: str | None = None,
    peers: list[str] | None = None,
) -> dict | list[dict] | None:
    """Strict resolver — own-first, then peers (public surface only).

    Returns the source dict, ``None`` (not found), or a list (ambiguous
    prefix on one scope). Ambiguity is resolved per-scope: an exact hit
    in own-org wins over a peer prefix match; ambiguity within a single
    scope surfaces as a list like in the single-DB world.
    """
    resolved_org = _resolve_caller_org(caller_org)
    db = _open(caller_org)
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
    caller_org: str | None = None,
    peers: list[str] | None = None,
) -> dict | None:
    """Get an attachment row by ID prefix. ``None`` if not found."""
    db = _open(caller_org)
    try:
        return db.get_attachment(attachment_id)
    finally:
        db.close()


def list_attachments(
    source_id: str | None = None,
    *,
    caller_org: str | None = None,
    peers: list[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    """List attachments, optionally filtered by source_id."""
    db = _open(caller_org)
    try:
        return db.list_attachments(source_id=source_id, limit=limit)
    finally:
        db.close()


_CROSS_ORG_LIST_SLOP = 10


def list_sources(
    *,
    caller_org: str | None = None,
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
    resolved_org = _resolve_caller_org(caller_org)
    per_db_limit = limit + _CROSS_ORG_LIST_SLOP

    if only_org is not None:
        if only_org == resolved_org:
            db = _open(caller_org)
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
    caller_org: str | None = None,
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
        caller_org=caller_org,
        peers=peers,
        only_org=only_org,
        limit=limit,
        source_type="note",
        since=since,
        tags=tags,
    )


def list_collab_sources(
    *,
    caller_org: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List collab-tagged sources ranked by activity."""
    db = _open(caller_org)
    try:
        return db.list_collab_sources(limit=limit)
    finally:
        db.close()


def list_collab_topics(
    *,
    caller_org: str | None = None,
) -> list[dict]:
    """List tag taxonomy entries (name + description + counts).

    Today returns rows from the ``tags`` table. Empty list if table absent.
    """
    db = _open(caller_org)
    try:
        return db.list_tags(limit=200)
    finally:
        db.close()


def list_attention(
    *,
    caller_org: str | None = None,
    since: str | None = None,
    search: str | None = None,
    last: int | None = None,
    session: str | None = None,
    context: int = 0,
) -> list[dict]:
    """List human input ('attention') across sessions. Always own-org.

    Mirrors ``cli._query_attention`` so callers can drop the inline SQL.
    """
    db = _open(caller_org)
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
    caller_org: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Get recent turns of a source. Used by primer/context flows."""
    db = _open(caller_org)
    try:
        return db.get_recent_turns(source_id, limit=limit)
    finally:
        db.close()


def get_session(
    session_id: str,
    *,
    caller_org: str | None = None,
) -> dict | None:
    """Get a session-type source by id or session_id metadata. Always own-org."""
    db = _open(caller_org)
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
    caller_org: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Return notes tagged with ``tag`` as a chronological feed.

    Each item carries the projection the dashboard streams view expects:
    id, title (cleaned), created_at, author, tags, source_type, preview.
    """
    sources = list_sources(
        caller_org=caller_org,
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
    caller_org: str | None = None,
) -> int:
    """Return the count of distinct tags across all notes."""
    db = _open(caller_org)
    try:
        row = db.conn.execute(
            "SELECT COUNT(DISTINCT value) AS cnt "
            "FROM sources, json_each(json_extract(sources.metadata, '$.tags')) "
            "WHERE sources.type = 'note'"
        ).fetchone()
        return row["cnt"] if row else 0
    finally:
        db.close()


def streams_summary(
    *,
    caller_org: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Summarize active tag streams: tag, count, description, last_active."""
    db = _open(caller_org)
    try:
        rows = db.conn.execute(
            """SELECT metadata, created_at FROM sources
               WHERE type = 'note' AND json_extract(metadata, '$.tags') IS NOT NULL"""
        ).fetchall()
        tag_desc: dict[str, str] = {}
        try:
            for r2 in db.conn.execute("SELECT name, description FROM tags"):
                tag_desc[r2["name"]] = r2["description"] or ""
        except Exception:
            pass
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
    caller_org: str | None = None,
    version: str | None = None,
) -> dict | None:
    """Resolve a ![[id]] embed reference for the dashboard renderer.

    Returns a dict describing the embed (rich-content note, plain note, or
    attachment), or ``None`` if not found. See ``api_resolve_embed`` for the
    response shape.
    """
    db = _open(caller_org)
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
    caller_org: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List journal-type sources with the dashboard's three-zoom-level shape."""
    db = _open(caller_org)
    try:
        sources = db.list_sources(source_type="journal", since=since, limit=limit)
        entries = []
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
            entries.append({
                "id": s["id"],
                "compact": s.get("title", ""),
                "normal": normal,
                "expanded": meta.get("expanded", ""),
                "timestamp_start": meta.get("timestamp_start", ""),
                "timestamp_end": meta.get("timestamp_end", ""),
                "entry_type": meta.get("entry_type", ""),
                "created_at": s.get("created_at", ""),
            })
        return entries
    finally:
        db.close()


def get_comment(
    comment_id: str,
    *,
    caller_org: str | None = None,
) -> dict | None:
    """Look up a note comment by id (or id prefix)."""
    db = _open(caller_org)
    try:
        row = db.conn.execute(
            "SELECT * FROM note_comments WHERE id = ? OR id LIKE ?",
            (comment_id, f"{comment_id}%"),
        ).fetchone()
        return dict(row) if row else None
    finally:
        db.close()


# ── Write paths ──────────────────────────────────────────────


def _assert_own_source(
    source_id: str,
    *,
    caller_org: str | None,
) -> None:
    """Refuse mutations against peer-origin sources (graph://bcce359d-a1d).

    If the source does not exist in caller_org's DB but *does* exist in
    a peer DB, raise :class:`CrossOrgWriteError` with the origin slug.
    If the source is absent from every scope the caller sees, fall
    through silently — the downstream write will raise its own
    not-found error (or no-op) as usual.
    """
    resolved_org = _resolve_caller_org(caller_org)
    db = _open(caller_org)
    try:
        if db.get_source(source_id) is not None:
            return
    finally:
        db.close()

    for peer in sorted(resolve_peers(resolved_org, None)):
        peer_db = open_peer_db(peer)
        if peer_db is None:
            continue
        peer_row = peer_db.get_source(source_id)
        if peer_row is not None:
            raise CrossOrgWriteError(source_id, peer)


def _assert_own_comment(
    comment_id: str,
    *,
    caller_org: str | None,
) -> None:
    """Same rule as :func:`_assert_own_source` but for note comments."""
    resolved_org = _resolve_caller_org(caller_org)
    db = _open(caller_org)
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
    caller_org: str | None = None,
) -> bool:
    """Add a tag to a source. Returns True if newly added, False if already present.

    Raises :class:`CrossOrgWriteError` when the target source lives in a
    peer DB — tags are metadata on the source row, so they must be
    authored in the origin org.
    """
    _assert_own_source(source_id, caller_org=caller_org)
    db = _open(caller_org)
    try:
        return db.add_source_tag(source_id, tag)
    finally:
        db.close()


def remove_tag(
    source_id: str,
    tag: str,
    *,
    caller_org: str | None = None,
) -> bool:
    """Remove a tag from a source. Returns True if removed, False if not present.

    Peer-origin sources raise :class:`CrossOrgWriteError` (see ``add_tag``).
    """
    _assert_own_source(source_id, caller_org=caller_org)
    db = _open(caller_org)
    try:
        return db.remove_source_tag(source_id, tag)
    finally:
        db.close()


def add_comment(
    source_id: str,
    content: str,
    *,
    caller_org: str | None = None,
    actor: str = "user",
) -> dict:
    """Add a comment to a note. Returns the inserted row.

    Peer-origin notes raise :class:`CrossOrgWriteError` — comments have
    to land in the origin org for anyone there to see them.
    """
    _assert_own_source(source_id, caller_org=caller_org)
    db = _open(caller_org)
    try:
        return db.insert_comment(source_id, content, actor=actor)
    finally:
        db.close()


def integrate_comment(
    comment_id: str,
    *,
    caller_org: str | None = None,
) -> bool:
    """Mark a comment as integrated. Returns True if state changed.

    Peer-origin comments raise :class:`CrossOrgWriteError`.
    """
    _assert_own_comment(comment_id, caller_org=caller_org)
    db = _open(caller_org)
    try:
        return db.integrate_comment(comment_id)
    finally:
        db.close()


def tag_merge(
    from_tag: str,
    to_tag: str,
    *,
    caller_org: str | None = None,
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
    db = _open(caller_org)
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
    caller_org: str | None = None,
    actor: str = "user",
) -> bool:
    """Set or update a tag description in the tags table."""
    db = _open(caller_org)
    try:
        return db.update_tag_description(tag_name, description, actor=actor)
    finally:
        db.close()


def insert_capture(
    capture_id: str,
    content: str,
    *,
    caller_org: str | None = None,
    source_id: str | None = None,
    turn_number: int | None = None,
    thread_id: str | None = None,
    actor: str = "user",
) -> None:
    """Insert a thought capture. ``thread_id`` must be the full UUID."""
    db = _open(caller_org)
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
    caller_org: str | None = None,
    priority: int = 1,
    created_by: str = "user",
) -> None:
    """Insert a new thought thread."""
    db = _open(caller_org)
    try:
        db.insert_thread(thread_id, title, priority=priority, created_by=created_by)
    finally:
        db.close()


def update_thread_status(
    thread_id: str,
    status: str,
    *,
    caller_org: str | None = None,
) -> dict | None:
    """Update a thread's status. Returns the updated thread row (for title display)."""
    db = _open(caller_org)
    try:
        db.update_thread_status(thread_id, status)
        return db.get_thread(thread_id)
    finally:
        db.close()


def assign_capture_to_thread(
    capture_id: str,
    thread_id: str,
    *,
    caller_org: str | None = None,
) -> None:
    """Assign a capture to a thread."""
    db = _open(caller_org)
    try:
        db.assign_capture_to_thread(capture_id, thread_id)
    finally:
        db.close()


def get_thread(
    thread_id: str,
    *,
    caller_org: str | None = None,
) -> dict | None:
    """Get a thread by id (or id prefix)."""
    db = _open(caller_org)
    try:
        return db.get_thread(thread_id)
    finally:
        db.close()


def list_captures(
    *,
    caller_org: str | None = None,
    thread_id: str | None = None,
    status: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """List thought captures with optional filters."""
    db = _open(caller_org)
    try:
        return db.list_captures(
            thread_id=thread_id,
            status=status,
            since=since,
            limit=limit,
        )
    except Exception:
        return []  # captures table may not exist on older DBs
    finally:
        db.close()


def list_threads(
    *,
    caller_org: str | None = None,
    status: str | None = "active",
    limit: int = 20,
) -> list[dict]:
    """List threads with optional status filter."""
    db = _open(caller_org)
    try:
        return db.list_threads(status=status, limit=limit)
    except Exception:
        return []  # threads table may not exist on older DBs
    finally:
        db.close()


def update_source_title(
    source_id: str,
    title: str,
    *,
    caller_org: str | None = None,
) -> None:
    """Update a source title. Last write wins.

    Peer-origin sources raise :class:`CrossOrgWriteError`.
    """
    _assert_own_source(source_id, caller_org=caller_org)
    db = _open(caller_org)
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
    caller_org: str | None = None,
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
    db = _open(caller_org)
    try:
        row = db.conn.execute(
            "SELECT id, publication_state FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        if not row:
            # Distinguish peer-origin from not-found so operators get a
            # useful message ("act in origin org X") instead of bare 404.
            resolved_org = _resolve_caller_org(caller_org)
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
    caller_org: str | None = None,
) -> None:
    """Force a WAL checkpoint so immutable=1 readers see current writes.

    Best-effort — silently swallows errors (read-only mounts, etc.).
    """
    try:
        db = _open(caller_org)
        try:
            db.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        finally:
            db.close()
    except Exception:
        pass


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
