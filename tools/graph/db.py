"""Database operations for the Autonomy Knowledge Graph."""

from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import Source, Thought, Derivation, Entity, Claim, Edge, Node, new_id

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DEFAULT_DB = Path(__file__).parents[2] / "data" / "graph.db"


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
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self):
        schema = SCHEMA_PATH.read_text()
        self.conn.executescript(schema)

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

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

    def search(self, query: str, limit: int = 20, project: str | None = None, or_mode: bool = False) -> list[dict]:
        """Full-text search across thoughts and derivations. Optionally filter by project."""
        results = []
        fts_query = _sanitize_fts_query(query, or_mode=or_mode)

        if project:
            # Project-scoped search
            rows = self.conn.execute(
                """SELECT t.id, t.content, t.turn_number, t.tags, t.source_id,
                          s.title as source_title, s.platform, s.project,
                          'thought' as result_type,
                          rank
                   FROM thoughts_fts fts
                   JOIN thoughts t ON t.rowid = fts.rowid
                   JOIN sources s ON s.id = t.source_id
                   WHERE thoughts_fts MATCH ? AND s.project = ?
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, project, limit),
            ).fetchall()
            results.extend(dict(r) for r in rows)

            rows = self.conn.execute(
                """SELECT d.id, d.content, d.turn_number, d.thought_id, d.source_id,
                          s.title as source_title, s.platform, s.project,
                          'derivation' as result_type,
                          rank
                   FROM derivations_fts fts
                   JOIN derivations d ON d.rowid = fts.rowid
                   JOIN sources s ON s.id = d.source_id
                   WHERE derivations_fts MATCH ? AND s.project = ?
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, project, limit),
            ).fetchall()
            results.extend(dict(r) for r in rows)
        else:
            # Global search
            rows = self.conn.execute(
                """SELECT t.id, t.content, t.turn_number, t.tags, t.source_id,
                          s.title as source_title, s.platform, s.project,
                          'thought' as result_type,
                          rank
                   FROM thoughts_fts fts
                   JOIN thoughts t ON t.rowid = fts.rowid
                   JOIN sources s ON s.id = t.source_id
                   WHERE thoughts_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, limit),
            ).fetchall()
            results.extend(dict(r) for r in rows)

            rows = self.conn.execute(
                """SELECT d.id, d.content, d.turn_number, d.thought_id, d.source_id,
                          s.title as source_title, s.platform, s.project,
                          'derivation' as result_type,
                          rank
                   FROM derivations_fts fts
                   JOIN derivations d ON d.rowid = fts.rowid
                   JOIN sources s ON s.id = d.source_id
                   WHERE derivations_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, limit),
            ).fetchall()
            results.extend(dict(r) for r in rows)

        # Sort by rank
        results.sort(key=lambda r: r.get("rank", 0))
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

    def list_sources(self, project: str | None = None, source_type: str | None = None, limit: int = 20) -> list[dict]:
        """List sources with optional filters."""
        query = "SELECT * FROM sources WHERE 1=1"
        params = []
        if project:
            query += " AND project = ?"
            params.append(project)
        if source_type:
            query += " AND type = ?"
            params.append(source_type)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def commit(self):
        self.conn.commit()
