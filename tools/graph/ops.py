"""Graph service layer.

All graph DB access flows through this module. CLI commands (via
``client.LocalClient``) and dashboard API handlers both call into ``ops.*``;
neither imports ``GraphDB`` directly outside this layer.

The ``caller_org`` and ``peers`` parameters are placeholders for the per-org
DB world (auto-txg5). Today they are ignored — every call lands on the single
``GraphDB(db_path)`` selected by ``GRAPH_DB`` env var. Once per-org routing
ships, only this module changes; call sites stay put.

Design reference: graph://bcce359d-a1d (Cross-Org Search Architecture).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .db import GraphDB, resolve_caller_db_path
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


# ── DB selection ─────────────────────────────────────────────


def _db_path(caller_org: str | None = None) -> str | None:
    """Resolve graph DB path for the given ``caller_org``.

    ``GRAPH_DB`` env var wins (test/override path); otherwise routes to
    ``data/orgs/<caller_org>.db`` if present, falling back to the legacy
    ``data/graph.db`` when the per-org DB has not yet been materialised
    (autonomy pre-migration). See :func:`db.resolve_caller_db_path`.
    """
    env_db = os.environ.get("GRAPH_DB")
    if env_db:
        return env_db
    return str(resolve_caller_db_path(caller_org))


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
    limit: int = 25,
    project: str | None = None,
    or_mode: bool = False,
    tag: str | None = None,
    states: list[str] | None = None,
    include_raw: bool = False,
    session_source_ids: list[str] | None = None,
    session_author_pattern: str | None = None,
) -> list[dict]:
    """Full-text search across the graph.

    Returns a list of result dicts (sources, edges, thoughts) — see
    ``GraphDB.search`` for shape. ``peers`` is reserved for cross-org search.
    ``states`` / ``include_raw`` / ``session_*`` tune the publication_state
    filter (see graph://8cf067e3-ca3).
    """
    db = _open(caller_org)
    try:
        return db.search(
            q, limit=limit, project=project, or_mode=or_mode, tag=tag,
            states=states, include_raw=include_raw,
            session_source_ids=session_source_ids,
            session_author_pattern=session_author_pattern,
        )
    finally:
        db.close()


def get_source(
    source_id: str,
    *,
    caller_org: str | None = None,
    peers: list[str] | None = None,
) -> dict | None:
    """Resolve a source by exact ID or prefix. ``None`` if not found."""
    db = _open(caller_org)
    try:
        return db.get_source(source_id)
    finally:
        db.close()


def resolve_source_strict(
    source_id: str,
    *,
    caller_org: str | None = None,
    peers: list[str] | None = None,
) -> dict | list[dict] | None:
    """Strict resolver — returns the source dict, ``None`` (not found), or a
    list (ambiguous prefix). Used by bead/link commands.
    """
    db = _open(caller_org)
    try:
        return db.resolve_source_strict(source_id)
    finally:
        db.close()


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


def list_sources(
    *,
    caller_org: str | None = None,
    peers: list[str] | None = None,
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
    """List sources with optional filters (project, type, time, tags, author)."""
    db = _open(caller_org)
    try:
        return db.list_sources(
            project=project,
            source_type=source_type,
            limit=limit,
            since=since,
            until=until,
            author=author,
            tags=tags,
            states=states,
            include_raw=include_raw,
            session_source_ids=session_source_ids,
            session_author_pattern=session_author_pattern,
        )
    finally:
        db.close()


def list_notes(
    *,
    caller_org: str | None = None,
    peers: list[str] | None = None,
    since: str | None = None,
    tags: list[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    """List note-type sources (chronological, optional time/tag filters)."""
    return list_sources(
        caller_org=caller_org,
        peers=peers,
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


def add_tag(
    source_id: str,
    tag: str,
    *,
    caller_org: str | None = None,
) -> bool:
    """Add a tag to a source. Returns True if newly added, False if already present.
    """
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
    """Remove a tag from a source. Returns True if removed, False if not present."""
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
    """Add a comment to a note. Returns the inserted row."""
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
    """Mark a comment as integrated. Returns True if state changed."""
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
    """Update a source title. Last write wins."""
    db = _open(caller_org)
    try:
        db.update_source_title(source_id, title)
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
