"""Database operations for the Autonomy Knowledge Graph."""

from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import Source, Thought, Derivation, Entity, Claim, Edge, Node, Attachment, new_id

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DEFAULT_DB = Path(__file__).parents[2] / "data" / "graph.db"


import re as _re

_SOURCE_ID_RE = _re.compile(r'^[0-9a-f]{6,}(-[0-9a-f]+)?$', _re.IGNORECASE)


def _is_source_id(query: str) -> bool:
    """Return True if *query* looks like a source ID (hex prefix, 6+ chars)."""
    return bool(_SOURCE_ID_RE.match(query.strip()))


def _sanitize_fts_query(query: str, or_mode: bool = False) -> str:
    """Sanitize a user query for safe use in FTS5 MATCH expressions.

    - Preserves existing double-quoted phrases as-is.
    - Strips FTS5 operator characters (-, :, (, ), *, ^, ~) from bare words.
    - Wraps each bare word in double quotes to prevent operator interpretation.
    - Joins terms with OR when or_mode=True, otherwise implicit AND (space-separated).

    Examples:
        'one-time architect'  -> '"one" "time" "architect"'
        '"exact phrase" other' -> '"exact phrase" "other"'
    """
    import re
    tokens = []
    # Pull out already-quoted phrases first, then split remaining bare words
    parts = re.split(r'("(?:[^"\\]|\\.)*")', query)
    for part in parts:
        if part.startswith('"') and part.endswith('"') and len(part) >= 2:
            # Already a quoted phrase — keep as-is
            tokens.append(part)
        else:
            # Strip FTS5 operator chars from bare text, then split into words
            cleaned = re.sub(r'[-:()*^~]', ' ', part)
            for word in cleaned.split():
                if word:
                    tokens.append(f'"{word}"')
    if not tokens:
        return '""'
    joiner = " OR " if or_mode else " "
    return joiner.join(tokens)


class GraphDB:
    def __init__(self, db_path: Path | str = DEFAULT_DB):
        self.db_path = Path(db_path)
        self.read_only = False
        self._immutable = False
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.db_path))
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA foreign_keys = ON")
            self._init_schema()
        except (sqlite3.OperationalError, OSError):
            # Read-only mount — try mode=ro first (WAL-visible), fall back to immutable
            try:
                self.conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
                self.conn.row_factory = sqlite3.Row
            except (sqlite3.OperationalError, OSError):
                self.conn = sqlite3.connect(f"file:{self.db_path}?immutable=1", uri=True)
                self.conn.row_factory = sqlite3.Row
                self._immutable = True
            self.read_only = True

    def _init_schema(self):
        schema = SCHEMA_PATH.read_text()
        self.conn.executescript(schema)
        self._seed_tags()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def is_immutable(self) -> bool:
        """True if opened with immutable=1 (no WAL visibility)."""
        return self.read_only and self._immutable

    # ── Sources ──────────────────────────────────────────────

    def insert_source(self, src: Source) -> Source:
        self.conn.execute(
            """INSERT INTO sources (id, type, platform, project, title, url, file_path, metadata, created_at, ingested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (src.id, src.type, src.platform, src.project, src.title, src.url,
             src.file_path, json.dumps(src.metadata), src.created_at, src.ingested_at),
        )
        self.conn.commit()
        return src

    def update_source_title(self, source_id: str, title: str):
        """Update the title of a source. Last write wins."""
        self.conn.execute(
            "UPDATE sources SET title = ? WHERE id = ?", (title, source_id)
        )
        self.conn.commit()

    def update_source_metadata(self, source_id: str, metadata: dict):
        self.conn.execute(
            "UPDATE sources SET metadata = ?, ingested_at = ? WHERE id = ?",
            (json.dumps(metadata), self.conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now')").fetchone()[0], source_id),
        )
        self.conn.commit()

    def get_max_turn(self, source_id: str) -> int:
        """Get the highest turn number already ingested for a source."""
        row = self.conn.execute(
            """SELECT MAX(turn_number) as max_turn FROM (
                SELECT turn_number FROM thoughts WHERE source_id = ?
                UNION ALL
                SELECT turn_number FROM derivations WHERE source_id = ?
            )""",
            (source_id, source_id),
        ).fetchone()
        return row["max_turn"] or 0

    def get_source_by_path(self, file_path: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM sources WHERE file_path = ?", (file_path,)
        ).fetchone()
        return dict(row) if row else None

    def delete_source(self, source_id: str):
        self.conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        self.conn.commit()

    # ── Thoughts ─────────────────────────────────────────────

    def insert_thought(self, t: Thought) -> Thought:
        self.conn.execute(
            """INSERT INTO thoughts (id, source_id, content, role, turn_number, message_id, tags, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (t.id, t.source_id, t.content, t.role, t.turn_number, t.message_id,
             json.dumps(t.tags), json.dumps(t.metadata), t.created_at),
        )
        return t

    def get_thoughts_by_source(self, source_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM thoughts WHERE source_id = ? ORDER BY turn_number", (source_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Derivations ──────────────────────────────────────────

    def insert_derivation(self, d: Derivation) -> Derivation:
        self.conn.execute(
            """INSERT INTO derivations (id, source_id, thought_id, content, model, turn_number, message_id, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (d.id, d.source_id, d.thought_id, d.content, d.model, d.turn_number,
             d.message_id, json.dumps(d.metadata), d.created_at),
        )
        return d

    # ── Entities ─────────────────────────────────────────────

    def upsert_entity(self, name: str, entity_type: str = "concept", description: str | None = None) -> str:
        canonical = name.lower().strip()
        row = self.conn.execute(
            "SELECT id FROM entities WHERE canonical_name = ?", (canonical,)
        ).fetchone()
        if row:
            return row["id"]
        eid = new_id()
        self.conn.execute(
            """INSERT INTO entities (id, name, canonical_name, type, description)
               VALUES (?, ?, ?, ?, ?)""",
            (eid, name, canonical, entity_type, description),
        )
        return eid

    def get_entity(self, canonical_name: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM entities WHERE canonical_name = ?", (canonical_name.lower().strip(),)
        ).fetchone()
        return dict(row) if row else None

    def list_entities(self, entity_type: str | None = None, limit: int = 100) -> list[dict]:
        if entity_type:
            rows = self.conn.execute(
                "SELECT * FROM entities WHERE type = ? ORDER BY name LIMIT ?",
                (entity_type, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM entities ORDER BY name LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Entity Mentions ──────────────────────────────────────

    def add_mention(self, entity_id: str, content_id: str, content_type: str, count: int = 1):
        self.conn.execute(
            """INSERT INTO entity_mentions (entity_id, content_id, content_type, count)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(entity_id, content_id)
               DO UPDATE SET count = count + excluded.count""",
            (entity_id, content_id, content_type, count),
        )

    # ── Claims ───────────────────────────────────────────────

    def insert_claim(self, c: Claim) -> Claim:
        self.conn.execute(
            """INSERT INTO claims (id, subject_id, predicate, object_id, object_val, source_id,
                                   asserted_by, confidence, status, evidence, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (c.id, c.subject_id, c.predicate, c.object_id, c.object_val, c.source_id,
             c.asserted_by, c.confidence, c.status, c.evidence, json.dumps(c.metadata), c.created_at),
        )
        return c

    # ── Edges ────────────────────────────────────────────────

    def insert_edge(self, e: Edge) -> Edge:
        self.conn.execute(
            """INSERT OR IGNORE INTO edges (id, source_id, source_type, target_id, target_type, relation, weight, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (e.id, e.source_id, e.source_type, e.target_id, e.target_type,
             e.relation, e.weight, json.dumps(e.metadata), e.created_at),
        )
        return e

    # ── Collab / Tags ─────────────────────────────────────────

    def add_source_tag(self, source_id: str, tag: str) -> bool:
        """Append a tag to the source's metadata.tags array. Returns True if added, False if already present."""
        row = self.conn.execute("SELECT metadata FROM sources WHERE id = ?", (source_id,)).fetchone()
        if not row:
            return False
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        tags = meta.get("tags", [])
        if tag in tags:
            return False
        tags.append(tag)
        meta["tags"] = tags
        self.update_source_metadata(source_id, meta)
        return True

    def _ensure_note_reads_table(self):
        """Auto-migration: create note_reads table if missing. No-op on read-only DBs."""
        if self.read_only:
            return
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS note_reads (
                source_id TEXT NOT NULL,
                actor     TEXT NOT NULL,
                ts        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                PRIMARY KEY (source_id, actor, ts)
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_note_reads_source ON note_reads(source_id)")

    def record_read(self, source_id: str, actor: str):
        """Record a read event for a collab note. Silently skips on read-only DBs."""
        if self.read_only:
            return
        try:
            self._ensure_note_reads_table()
            self.conn.execute(
                "INSERT OR IGNORE INTO note_reads (source_id, actor) VALUES (?, ?)",
                (source_id, actor),
            )
            self.conn.commit()
        except sqlite3.OperationalError:
            pass

    def _has_table(self, name: str) -> bool:
        """Check if a table exists in the database."""
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        return row is not None

    def list_collab_sources(self, limit: int = 50) -> list[dict]:
        """List sources tagged 'collab', ranked by activity (comments*3 + reads*1)."""
        self._ensure_note_reads_table()
        has_reads = self._has_table("note_reads")
        if has_reads:
            reads_join = "LEFT JOIN (SELECT source_id, COUNT(*) AS cnt FROM note_reads GROUP BY source_id) r ON r.source_id = s.id"
            reads_col = "COALESCE(r.cnt, 0)"
        else:
            reads_join = ""
            reads_col = "0"
        query = f"""
            SELECT s.*,
                   COALESCE(c.cnt, 0) AS comment_count,
                   {reads_col} AS read_count
            FROM sources s
            LEFT JOIN (SELECT source_id, COUNT(*) AS cnt FROM note_comments GROUP BY source_id) c
                ON c.source_id = s.id
            {reads_join}
            WHERE json_extract(s.metadata, '$.tags') LIKE '%"collab"%'
            ORDER BY ({reads_col} * 1 + COALESCE(c.cnt, 0) * 3) DESC, s.created_at DESC
            LIMIT ?
        """
        rows = self.conn.execute(query, (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ── Nodes (hierarchy) ────────────────────────────────────

    def insert_node(self, n: Node) -> Node:
        self.conn.execute(
            """INSERT INTO nodes (id, parent_id, type, title, description, status, sort_order, metadata, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (n.id, n.parent_id, n.type, n.title, n.description, n.status,
             n.sort_order, json.dumps(n.metadata), n.created_at, n.updated_at),
        )
        self.conn.commit()
        return n

    def get_children(self, parent_id: str | None) -> list[dict]:
        if parent_id is None:
            rows = self.conn.execute(
                "SELECT * FROM nodes WHERE parent_id IS NULL ORDER BY sort_order, title"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM nodes WHERE parent_id = ? ORDER BY sort_order, title",
                (parent_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_node(self, node_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return dict(row) if row else None

    def get_tree(self, root_id: str | None = None, depth: int = 10) -> list[dict]:
        """Get subtree as flat list with depth info."""
        results = []
        self._walk_tree(root_id, 0, depth, results)
        return results

    def _walk_tree(self, parent_id: str | None, current_depth: int, max_depth: int, results: list):
        if current_depth > max_depth:
            return
        children = self.get_children(parent_id)
        for child in children:
            child["_depth"] = current_depth
            results.append(child)
            self._walk_tree(child["id"], current_depth + 1, max_depth, results)

    def add_node_ref(self, node_id: str, ref_id: str, ref_type: str, metadata: dict | None = None):
        self.conn.execute(
            """INSERT OR IGNORE INTO node_refs (node_id, ref_id, ref_type, metadata)
               VALUES (?, ?, ?, ?)""",
            (node_id, ref_id, ref_type, json.dumps(metadata or {})),
        )
        self.conn.commit()

    # ── Search ───────────────────────────────────────────────

    def search(self, query: str, limit: int = 20, project: str | None = None, or_mode: bool = False, tag: str | None = None) -> list[dict]:
        """Full-text search across thoughts and derivations. Optionally filter by project.

        If *query* looks like a hex source ID (6+ hex chars), resolves it
        directly via prefix lookup and returns the source plus linked sources
        before falling back to FTS for content mentions.
        """
        if _is_source_id(query):
            return self._search_source_id(query.strip(), limit=limit, project=project, tag=tag)

        results = []
        fts_query = _sanitize_fts_query(query, or_mode=or_mode)

        tag_clause = ""
        tag_params: list[str] = []
        if tag:
            tag_clause = " AND json_extract(s.metadata, '$.tags') LIKE ?"
            tag_params = [f'%"{tag}"%']

        if project:
            # Project-scoped search
            rows = self.conn.execute(
                f"""SELECT t.id, t.content, t.turn_number, t.tags, t.source_id,
                          s.title as source_title, s.platform, s.project,
                          'thought' as result_type,
                          rank
                   FROM thoughts_fts fts
                   JOIN thoughts t ON t.rowid = fts.rowid
                   JOIN sources s ON s.id = t.source_id
                   WHERE thoughts_fts MATCH ? AND s.project = ?{tag_clause}
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, project, *tag_params, limit),
            ).fetchall()
            results.extend(dict(r) for r in rows)

            rows = self.conn.execute(
                f"""SELECT d.id, d.content, d.turn_number, d.thought_id, d.source_id,
                          s.title as source_title, s.platform, s.project,
                          'derivation' as result_type,
                          rank
                   FROM derivations_fts fts
                   JOIN derivations d ON d.rowid = fts.rowid
                   JOIN sources s ON s.id = d.source_id
                   WHERE derivations_fts MATCH ? AND s.project = ?{tag_clause}
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, project, *tag_params, limit),
            ).fetchall()
            results.extend(dict(r) for r in rows)
        else:
            # Global search
            rows = self.conn.execute(
                f"""SELECT t.id, t.content, t.turn_number, t.tags, t.source_id,
                          s.title as source_title, s.platform, s.project,
                          'thought' as result_type,
                          rank
                   FROM thoughts_fts fts
                   JOIN thoughts t ON t.rowid = fts.rowid
                   JOIN sources s ON s.id = t.source_id
                   WHERE thoughts_fts MATCH ?{tag_clause}
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, *tag_params, limit),
            ).fetchall()
            results.extend(dict(r) for r in rows)

            rows = self.conn.execute(
                f"""SELECT d.id, d.content, d.turn_number, d.thought_id, d.source_id,
                          s.title as source_title, s.platform, s.project,
                          'derivation' as result_type,
                          rank
                   FROM derivations_fts fts
                   JOIN derivations d ON d.rowid = fts.rowid
                   JOIN sources s ON s.id = d.source_id
                   WHERE derivations_fts MATCH ?{tag_clause}
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, *tag_params, limit),
            ).fetchall()
            results.extend(dict(r) for r in rows)

        # Sort by rank
        results.sort(key=lambda r: r.get("rank", 0))
        return results[:limit]

    def _search_source_id(self, query: str, limit: int = 20, project: str | None = None, tag: str | None = None) -> list[dict]:
        """Resolve a source-ID-shaped query directly.

        Returns:
            1. The source itself (prefix match)
            2. Sources linked TO it (edges where target_id matches)
            3. Sources linked FROM it (edges where source_id matches)
            4. FTS fallback — thoughts/derivations whose content mentions the ID
        """
        results: list[dict] = []
        seen_source_ids: set[str] = set()

        # 1. Direct prefix lookup
        source = self.get_source(query)
        if source:
            if project and source.get("project") != project:
                source = None  # skip if wrong project
            if source and tag:
                meta = source.get("metadata")
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except (json.JSONDecodeError, TypeError):
                        meta = {}
                if tag not in (meta.get("tags") or []):
                    source = None

        if source:
            sid = source["id"]
            seen_source_ids.add(sid)
            meta = source.get("metadata")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            results.append({
                "id": sid,
                "content": source.get("title") or sid,
                "turn_number": None,
                "source_id": sid,
                "source_title": source.get("title") or sid,
                "platform": source.get("platform"),
                "project": source.get("project"),
                "result_type": "source",
                "rank": -1000,  # always first
                "source_type": source.get("type"),
                "created_at": source.get("created_at"),
            })

            # 2 & 3. Linked sources via edges (both directions)
            edges = self.neighbors(sid, limit=50)
            for edge in edges:
                other_id = edge["target_id"] if edge["source_id"] == sid else edge["source_id"]
                direction = "→" if edge["source_id"] == sid else "←"
                if other_id in seen_source_ids:
                    continue

                # Resolve the other end — it could be a source, thought, or derivation
                other_source = None
                other_type = edge["target_type"] if edge["source_id"] == sid else edge["source_type"]

                if other_type == "source":
                    other_source = self.get_source(other_id)
                else:
                    # Edge points to a thought/derivation — look up its parent source
                    row = self.conn.execute(
                        "SELECT source_id FROM thoughts WHERE id = ? "
                        "UNION SELECT source_id FROM derivations WHERE id = ?",
                        (other_id, other_id),
                    ).fetchone()
                    if row:
                        parent_sid = row[0]
                        if parent_sid not in seen_source_ids:
                            other_source = self.get_source(parent_sid)
                            other_id = parent_sid

                if other_source and other_id not in seen_source_ids:
                    if project and other_source.get("project") != project:
                        continue
                    if tag:
                        ometa = other_source.get("metadata")
                        if isinstance(ometa, str):
                            try:
                                ometa = json.loads(ometa)
                            except (json.JSONDecodeError, TypeError):
                                ometa = {}
                        if tag not in (ometa.get("tags") or []):
                            continue
                    seen_source_ids.add(other_id)
                    edge_meta = edge.get("metadata", "{}")
                    if isinstance(edge_meta, str):
                        try:
                            edge_meta = json.loads(edge_meta)
                        except (json.JSONDecodeError, TypeError):
                            edge_meta = {}
                    turn = edge_meta.get("turn") or edge_meta.get("turn_number")
                    relation = edge.get("relation", "linked")
                    results.append({
                        "id": other_id,
                        "content": f"{direction} {relation}: {other_source.get('title') or other_id}",
                        "turn_number": turn,
                        "source_id": other_id,
                        "source_title": other_source.get("title") or other_id,
                        "platform": other_source.get("platform"),
                        "project": other_source.get("project"),
                        "result_type": "edge",
                        "rank": -500,
                        "relation": relation,
                        "direction": direction,
                        "source_type": other_source.get("type"),
                    })

        # 4. FTS fallback — content that mentions this ID string
        remaining = limit - len(results)
        if remaining > 0:
            try:
                fts_query = _sanitize_fts_query(query)
                tag_clause = ""
                tag_params: list[str] = []
                if tag:
                    tag_clause = " AND json_extract(s.metadata, '$.tags') LIKE ?"
                    tag_params = [f'%"{tag}"%']
                for table, content_table, rtype in [
                    ("thoughts_fts", "thoughts", "thought"),
                    ("derivations_fts", "derivations", "derivation"),
                ]:
                    if project:
                        rows = self.conn.execute(
                            f"""SELECT t.id, t.content, t.turn_number, t.source_id,
                                       s.title as source_title, s.platform, s.project,
                                       '{rtype}' as result_type, rank
                                FROM {table} fts
                                JOIN {content_table} t ON t.rowid = fts.rowid
                                JOIN sources s ON s.id = t.source_id
                                WHERE {table} MATCH ? AND s.project = ?{tag_clause}
                                ORDER BY rank LIMIT ?""",
                            (fts_query, project, *tag_params, remaining),
                        ).fetchall()
                    else:
                        rows = self.conn.execute(
                            f"""SELECT t.id, t.content, t.turn_number, t.source_id,
                                       s.title as source_title, s.platform, s.project,
                                       '{rtype}' as result_type, rank
                                FROM {table} fts
                                JOIN {content_table} t ON t.rowid = fts.rowid
                                JOIN sources s ON s.id = t.source_id
                                WHERE {table} MATCH ?{tag_clause}
                                ORDER BY rank LIMIT ?""",
                            (fts_query, *tag_params, remaining),
                        ).fetchall()
                    for r in rows:
                        rd = dict(r)
                        if rd["source_id"] not in seen_source_ids:
                            results.append(rd)
            except Exception:
                pass  # FTS may not match hex strings — that's fine

        return results[:limit]

    def search_entities(self, query: str, limit: int = 20) -> list[dict]:
        """Search entities by name."""
        rows = self.conn.execute(
            "SELECT * FROM entities WHERE canonical_name LIKE ? ORDER BY name LIMIT ?",
            (f"%{query.lower()}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Stats ────────────────────────────────────────────────

    def stats(self) -> dict:
        result = {}
        for table in ["sources", "thoughts", "derivations", "entities", "claims", "edges", "entity_mentions", "nodes"]:
            row = self.conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
            result[table] = row["cnt"]
        return result

    # ── Graph Queries ────────────────────────────────────────

    def neighbors(self, node_id: str, relation: str | None = None, limit: int = 50) -> list[dict]:
        """Find all edges from/to a given node."""
        if relation:
            rows = self.conn.execute(
                """SELECT * FROM edges
                   WHERE (source_id = ? OR target_id = ?) AND relation = ?
                   LIMIT ?""",
                (node_id, node_id, relation, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT * FROM edges
                   WHERE source_id = ? OR target_id = ?
                   LIMIT ?""",
                (node_id, node_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def entity_thoughts(self, entity_id: str) -> list[dict]:
        """Find all thoughts that mention a given entity."""
        rows = self.conn.execute(
            """SELECT t.*, s.title as source_title, s.platform
               FROM entity_mentions em
               JOIN thoughts t ON t.id = em.content_id AND em.content_type = 'thought'
               JOIN sources s ON s.id = t.source_id
               WHERE em.entity_id = ?
               ORDER BY t.created_at""",
            (entity_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Source Reading ─────────────────────────────────────────

    def get_source(self, source_id: str) -> dict | None:
        """Look up a source by ID, prefix, or session UUID (stored in metadata)."""
        # Exact match
        row = self.conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row:
            return dict(row)
        # Prefix match
        row = self.conn.execute("SELECT * FROM sources WHERE id LIKE ? LIMIT 1", (f"{source_id}%",)).fetchone()
        if row:
            return dict(row)
        # Session UUID match (stored in metadata JSON)
        row = self.conn.execute(
            """SELECT * FROM sources WHERE json_extract(metadata, '$.session_id') LIKE ? LIMIT 1""",
            (f"{source_id}%",),
        ).fetchone()
        return dict(row) if row else None

    def resolve_source_strict(self, value: str) -> dict | list[dict] | None:
        """Strict source resolution: exact → prefix → session_uuid → file_path.

        Returns:
            dict — single match (success)
            list[dict] — multiple matches (caller should error with candidates)
            None — no match
        """
        # 1. Exact source ID match
        row = self.conn.execute(
            "SELECT * FROM sources WHERE id = ?", (value,)
        ).fetchone()
        if row:
            return dict(row)

        # 2. Prefix match on source ID
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE id LIKE ?", (f"{value}%",)
        ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1:
            return [dict(r) for r in rows]

        # 3. Match by session_uuid in metadata (the JSONL filename stem)
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE json_extract(metadata, '$.session_uuid') LIKE ?",
            (f"{value}%",),
        ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1:
            return [dict(r) for r in rows]

        # 4. Match by session_id in metadata (legacy, same as session_uuid)
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE json_extract(metadata, '$.session_id') LIKE ?",
            (f"{value}%",),
        ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1:
            return [dict(r) for r in rows]

        # 5. Match by file_path containing the value (JSONL UUID in path)
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE file_path LIKE ?",
            (f"%/{value}%.jsonl",),
        ).fetchall()
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1:
            return [dict(r) for r in rows]

        return None


    def get_latest_turn(self, source_id: str) -> int | None:
        """Return the highest turn_number for a source, or None if no turns."""
        row = self.conn.execute(
            "SELECT MAX(turn_number) as max_turn FROM thoughts WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        return row["max_turn"] if row and row["max_turn"] is not None else None

    def get_recent_turns(self, source_id: str, limit: int = 50) -> list[dict]:
        """Return the most recent N turns for a source, newest first."""
        rows = self.conn.execute(
            """SELECT turn_number, content FROM thoughts
               WHERE source_id = ? ORDER BY turn_number DESC LIMIT ?""",
            (source_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_source_content(self, source_id: str) -> list[dict]:
        """Get all thoughts and derivations for a source, ordered by turn number."""
        rows = self.conn.execute(
            """SELECT id, content, role, turn_number, message_id, metadata, 'thought' as entry_type
               FROM thoughts WHERE source_id = ?
               UNION ALL
               SELECT id, content, model as role, turn_number, message_id, metadata, 'derivation' as entry_type
               FROM derivations WHERE source_id = ?
               ORDER BY turn_number""",
            (source_id, source_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def find_sources(self, query: str, limit: int = 20) -> list[dict]:
        """Search sources by title."""
        rows = self.conn.execute(
            "SELECT * FROM sources WHERE title LIKE ? ORDER BY created_at DESC LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_sources(self, project: str | None = None, source_type: str | None = None, limit: int = 20,
                     since: str | None = None, until: str | None = None, author: str | None = None,
                     tags: list[str] | None = None) -> list[dict]:
        """List sources with optional filters."""
        query = "SELECT * FROM sources WHERE 1=1"
        params = []
        if project:
            query += " AND project = ?"
            params.append(project)
        if source_type:
            query += " AND type = ?"
            params.append(source_type)
        if since:
            query += " AND created_at >= ?"
            params.append(since)
        if until:
            query += " AND created_at <= ?"
            params.append(until)
        if author:
            query += " AND json_extract(metadata, '$.author') = ?"
            params.append(author)
        if tags:
            for tag in tags:
                query += " AND json_extract(metadata, '$.tags') LIKE ?"
                params.append(f'%"{tag}"%')
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    # ── Note Comments ───────────────────────────────────────

    def insert_comment(self, source_id: str, content: str, actor: str = "user") -> dict:
        cid = new_id()
        self.conn.execute(
            """INSERT INTO note_comments (id, source_id, content, actor)
               VALUES (?, ?, ?, ?)""",
            (cid, source_id, content, actor),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM note_comments WHERE id = ?", (cid,)).fetchone()
        return dict(row)

    def get_comments(self, source_id: str, include_integrated: bool = False) -> list[dict]:
        if include_integrated:
            rows = self.conn.execute(
                "SELECT * FROM note_comments WHERE source_id = ? ORDER BY created_at ASC",
                (source_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM note_comments WHERE source_id = ? AND integrated = 0 ORDER BY created_at ASC",
                (source_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def integrate_comment(self, comment_id: str) -> bool:
        cur = self.conn.execute(
            "UPDATE note_comments SET integrated = 1 WHERE id = ? AND integrated = 0",
            (comment_id,),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # ── Note Versions ─────────────────────────────────────

    def insert_note_version(self, source_id: str, version: int, content: str):
        self.conn.execute(
            """INSERT INTO note_versions (source_id, version, content)
               VALUES (?, ?, ?)""",
            (source_id, version, content),
        )

    def get_note_version(self, source_id: str, version: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM note_versions WHERE source_id = ? AND version = ?",
            (source_id, version),
        ).fetchone()
        return dict(row) if row else None

    def list_note_versions(self, source_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM note_versions WHERE source_id = ? ORDER BY version ASC",
            (source_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_max_note_version(self, source_id: str) -> int:
        row = self.conn.execute(
            "SELECT MAX(version) as max_v FROM note_versions WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        return row["max_v"] or 0

    def update_thought_content(self, thought_id: str, content: str):
        self.conn.execute(
            "UPDATE thoughts SET content = ? WHERE id = ?",
            (content, thought_id),
        )

    # ── Attachments ─────────────────────────────────────────

    def insert_attachment(self, att: Attachment) -> Attachment:
        self.conn.execute(
            """INSERT INTO attachments (id, hash, filename, mime_type, size_bytes, file_path,
                                        source_id, turn_number, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (att.id, att.hash, att.filename, att.mime_type, att.size_bytes, att.file_path,
             att.source_id, att.turn_number, json.dumps(att.metadata), att.created_at),
        )
        self.conn.commit()
        return att

    def get_attachment(self, att_id: str) -> dict | None:
        """Look up attachment by ID or prefix."""
        row = self.conn.execute("SELECT * FROM attachments WHERE id = ?", (att_id,)).fetchone()
        if row:
            return dict(row)
        row = self.conn.execute("SELECT * FROM attachments WHERE id LIKE ? LIMIT 1", (f"{att_id}%",)).fetchone()
        return dict(row) if row else None

    def get_attachment_by_hash(self, hash: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM attachments WHERE hash = ?", (hash,)).fetchone()
        return dict(row) if row else None

    def list_attachments(self, source_id: str | None = None, limit: int = 50) -> list[dict]:
        if source_id:
            rows = self.conn.execute(
                "SELECT * FROM attachments WHERE source_id LIKE ? ORDER BY created_at DESC LIMIT ?",
                (f"{source_id}%", limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM attachments ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Tags ──────────────────────────────────────────────────

    def _seed_tags(self):
        """Seed tags table from existing note metadata (idempotent)."""
        rows = self.conn.execute(
            "SELECT metadata FROM sources WHERE type='note' AND metadata LIKE '%tags%'"
        ).fetchall()
        for row in rows:
            meta = json.loads(row["metadata"] or "{}")
            for tag in meta.get("tags", []):
                self.conn.execute(
                    "INSERT OR IGNORE INTO tags (name) VALUES (?)", (tag,)
                )
        self.conn.commit()

    def list_tags(self, limit: int = 100) -> list[dict]:
        """List tags with note counts, sorted by usage."""
        rows = self.conn.execute("""
            SELECT t.name, t.description, t.updated_at,
                   COUNT(s.id) as note_count
            FROM tags t
            LEFT JOIN sources s ON s.type = 'note'
                AND json_extract(s.metadata, '$.tags') LIKE '%' || t.name || '%'
            GROUP BY t.name
            ORDER BY note_count DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def update_tag_description(self, name: str, description: str, actor: str = "user") -> bool:
        """Set or update a tag's description. Creates the tag if it doesn't exist."""
        self.conn.execute("""
            INSERT INTO tags (name, description, created_by, updated_at)
            VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            ON CONFLICT(name) DO UPDATE SET
                description = excluded.description,
                updated_at = excluded.updated_at
        """, (name, description, actor))
        self.conn.commit()
        return True

    # ── Captures ──────────────────────────────────────────────

    def insert_capture(self, capture_id: str, content: str, *,
                       source_id: str | None = None, turn_number: int | None = None,
                       thread_id: str | None = None, actor: str = "user") -> None:
        """Insert a thought capture."""
        self.conn.execute(
            "INSERT INTO captures (id, content, source_id, turn_number, thread_id, actor)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (capture_id, content, source_id, turn_number, thread_id, actor),
        )
        self.conn.commit()

    def list_captures(self, thread_id: str | None = None, status: str | None = None,
                      since: str | None = None, limit: int = 20) -> list[dict]:
        """List captures, optionally filtered."""
        query = "SELECT * FROM captures WHERE 1=1"
        params: list = []
        if thread_id:
            query += " AND thread_id = ?"
            params.append(thread_id)
        if status:
            if status != "*":
                query += " AND status = ?"
                params.append(status)
        elif not thread_id:
            # Default: show unthreaded captures (inbox)
            query += " AND thread_id IS NULL"
        if since:
            query += " AND created_at >= ?"
            params.append(since)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(query, params).fetchall()]

    def assign_capture_to_thread(self, capture_id: str, thread_id: str) -> None:
        """Assign a capture to a thread."""
        self.conn.execute(
            "UPDATE captures SET thread_id = ?, status = 'threaded' WHERE id = ?",
            (thread_id, capture_id),
        )
        self.conn.commit()

    # ── Threads ───────────────────────────────────────────────

    def insert_thread(self, thread_id: str, title: str, *, priority: int = 1,
                      created_by: str | None = None) -> None:
        """Create a new thread."""
        self.conn.execute(
            "INSERT INTO threads (id, title, priority, created_by) VALUES (?, ?, ?, ?)",
            (thread_id, title, priority, created_by),
        )
        self.conn.commit()

    def list_threads(self, status: str | None = "active", limit: int = 20) -> list[dict]:
        """List threads with capture counts."""
        query = """
            SELECT t.*, COUNT(c.id) as capture_count
            FROM threads t
            LEFT JOIN captures c ON c.thread_id = t.id
        """
        params: list = []
        if status:
            query += " WHERE t.status = ?"
            params.append(status)
        query += " GROUP BY t.id ORDER BY t.priority, t.updated_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(query, params).fetchall()]

    def update_thread_status(self, thread_id: str, status: str) -> None:
        """Update thread status (active/parked/done)."""
        self.conn.execute(
            "UPDATE threads SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (status, thread_id),
        )
        self.conn.commit()

    def get_thread(self, thread_id: str) -> dict | None:
        """Get a single thread by ID or prefix."""
        row = self.conn.execute("SELECT * FROM threads WHERE id = ? OR id LIKE ?",
                                (thread_id, thread_id + '%')).fetchone()
        return dict(row) if row else None

    def commit(self):
        self.conn.commit()
