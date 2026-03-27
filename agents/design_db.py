"""SQLite storage for Design Studio designs.

Agents POST design variants with a shared fixture. Humans rank them
via the dashboard gallery. Results are polled back by the agent.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("EXPERIMENTS_DB", str(REPO_ROOT / "data" / "experiments.db")))

CREATE_DESIGNS = """\
CREATE TABLE IF NOT EXISTS designs (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT,
  fixture TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  design_id TEXT,
  revision_seq INTEGER,
  created_at DATETIME DEFAULT (datetime('now'))
)
"""

CREATE_VARIANTS = """\
CREATE TABLE IF NOT EXISTS revision_variants (
  id TEXT NOT NULL,
  revision_id TEXT NOT NULL,
  html TEXT NOT NULL,
  selected INTEGER NOT NULL DEFAULT 0,
  rank INTEGER,
  PRIMARY KEY (id, revision_id),
  FOREIGN KEY (revision_id) REFERENCES designs(id)
)
"""

# Legacy table DDL — used only during migration to detect old schema
_CREATE_EXPERIMENTS_LEGACY = """\
CREATE TABLE IF NOT EXISTS experiments (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT,
  fixture TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  series_id TEXT,
  series_seq INTEGER,
  created_at DATETIME DEFAULT (datetime('now'))
)
"""

_CREATE_VARIANTS_LEGACY = """\
CREATE TABLE IF NOT EXISTS experiment_variants (
  id TEXT NOT NULL,
  experiment_id TEXT NOT NULL,
  html TEXT NOT NULL,
  selected INTEGER NOT NULL DEFAULT 0,
  rank INTEGER,
  PRIMARY KEY (id, experiment_id),
  FOREIGN KEY (experiment_id) REFERENCES experiments(id)
)
"""


_initialized = False


def _ensure_init() -> None:
    global _initialized
    if not _initialized:
        _initialized = True
        init_db()


def _get_conn() -> sqlite3.Connection:
    _ensure_init()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _migrate_from_experiments(conn: sqlite3.Connection) -> None:
    """Migrate legacy experiments/experiment_variants tables to designs/revision_variants.

    Uses the create-copy-drop pattern since SQLite doesn't support ALTER COLUMN RENAME.
    """
    # Ensure legacy columns exist before migrating
    for stmt in [
        "ALTER TABLE experiments ADD COLUMN series_id TEXT",
        "ALTER TABLE experiments ADD COLUMN series_seq INTEGER",
        "ALTER TABLE experiments ADD COLUMN alpine INTEGER NOT NULL DEFAULT 0",
    ]:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass
    # Backfill standalone experiments
    conn.execute(
        "UPDATE experiments SET series_id = id, series_seq = 1 WHERE series_id IS NULL"
    )

    # Create new designs table
    conn.execute(CREATE_DESIGNS)
    # Add alpine column if not in DDL (it's not in the base CREATE — add it)
    try:
        conn.execute("ALTER TABLE designs ADD COLUMN alpine INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # Copy data: series_id → design_id, series_seq → revision_seq
    conn.execute("""\
        INSERT OR IGNORE INTO designs (id, title, description, fixture, status, design_id, revision_seq, created_at, alpine)
        SELECT id, title, description, fixture, status, series_id, series_seq, created_at, alpine
        FROM experiments
    """)

    # Create new revision_variants table
    conn.execute(CREATE_VARIANTS)
    # Copy data: experiment_id → revision_id
    if _table_exists(conn, "experiment_variants"):
        conn.execute("""\
            INSERT OR IGNORE INTO revision_variants (id, revision_id, html, selected, rank)
            SELECT id, experiment_id, html, selected, rank
            FROM experiment_variants
        """)
        conn.execute("DROP TABLE experiment_variants")

    conn.execute("DROP TABLE experiments")
    conn.commit()


def init_db() -> None:
    conn = _get_conn()
    try:
        # Check if legacy tables exist and need migration
        if _table_exists(conn, "experiments"):
            _migrate_from_experiments(conn)
            return

        # Fresh install — create new schema directly
        conn.execute(CREATE_DESIGNS)
        conn.execute(CREATE_VARIANTS)
        # Add alpine column if needed
        try:
            conn.execute("ALTER TABLE designs ADD COLUMN alpine INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # Backfill standalone designs
        conn.execute(
            "UPDATE designs SET design_id = id, revision_seq = 1 WHERE design_id IS NULL"
        )
        conn.commit()
    finally:
        conn.close()


def create_design(
    *,
    title: str,
    description: str | None = None,
    fixture: str | None = None,
    variants: list[dict],
    design_id: str | None = None,
    alpine: bool = False,
) -> str:
    """Create a design revision with variants. Returns the revision UUID.

    If design_id is provided the new revision is appended to that design
    with revision_seq = MAX(revision_seq) + 1.  If omitted the revision is
    standalone: design_id = its own id, revision_seq = 1.
    """
    rev_id = str(uuid.uuid4())
    conn = _get_conn()
    try:
        if design_id:
            row = conn.execute(
                "SELECT MAX(revision_seq) FROM designs WHERE design_id = ?",
                (design_id,),
            ).fetchone()
            revision_seq = (row[0] or 0) + 1
        else:
            design_id = rev_id
            revision_seq = 1
        conn.execute(
            "INSERT INTO designs (id, title, description, fixture, design_id, revision_seq, alpine)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rev_id, title, description, fixture, design_id, revision_seq, int(alpine)),
        )
        for v in variants:
            conn.execute(
                "INSERT INTO revision_variants (id, revision_id, html) VALUES (?, ?, ?)",
                (v["id"], rev_id, v["html"]),
            )
        conn.commit()
        return rev_id
    finally:
        conn.close()


def resolve_design_prefix(partial_id: str) -> tuple[str | None, list[str] | None]:
    """Resolve a partial design/revision UUID to a full UUID via prefix match.

    Returns (full_id, None) on unique match, (None, [matches]) on ambiguous,
    (None, None) on no match. Full UUIDs (>=36 chars) pass through unchanged.
    """
    if len(partial_id) >= 36:
        return partial_id, None
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id FROM designs WHERE id LIKE ?", (f"{partial_id}%",)
        ).fetchall()
        if len(rows) == 1:
            return rows[0]["id"], None
        if len(rows) > 1:
            return None, [r["id"] for r in rows]
        return None, None
    finally:
        conn.close()


def get_design(rev_id: str) -> dict | None:
    """Get design revision with its variants, design info, and revisions list."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM designs WHERE id = ?", (rev_id,)
        ).fetchone()
        if not row:
            return None
        exp = {k: row[k] for k in row.keys()}
        variants = conn.execute(
            "SELECT * FROM revision_variants WHERE revision_id = ? ORDER BY id",
            (rev_id,),
        ).fetchall()
        exp["variants"] = [{k: v[k] for k in v.keys()} for v in variants]
        # Populate revisions (all revisions in same design, ordered by seq)
        did = exp.get("design_id") or rev_id
        siblings = conn.execute(
            "SELECT id FROM designs WHERE design_id = ? ORDER BY revision_seq",
            (did,),
        ).fetchall()
        exp["revisions"] = [s["id"] for s in siblings]
        return exp
    finally:
        conn.close()


def submit_results(rev_id: str, selections: list[dict]) -> bool:
    """Submit ranking results. Each selection has id and rank.

    Returns True if the revision was found and updated.
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT status FROM designs WHERE id = ?", (rev_id,)
        ).fetchone()
        if not row:
            return False

        # Reset all variants first
        conn.execute(
            "UPDATE revision_variants SET selected = 0, rank = NULL WHERE revision_id = ?",
            (rev_id,),
        )
        # Set selected variants with ranks
        for sel in selections:
            conn.execute(
                "UPDATE revision_variants SET selected = 1, rank = ? WHERE id = ? AND revision_id = ?",
                (sel.get("rank"), sel["id"], rev_id),
            )
        # Mark revision completed
        conn.execute(
            "UPDATE designs SET status = 'completed' WHERE id = ?",
            (rev_id,),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def dismiss_design(rev_id: str) -> bool:
    """Mark a design revision and all pending revisions in its design as dismissed.

    Returns True if the revision was found.
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT design_id FROM designs WHERE id = ?", (rev_id,)
        ).fetchone()
        if not row:
            return False
        design_id = row["design_id"] or rev_id
        conn.execute(
            "UPDATE designs SET status = 'dismissed'"
            " WHERE design_id = ? AND status = 'pending'",
            (design_id,),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def list_pending() -> list[dict]:
    """List pending designs grouped by design_id.

    Returns one entry per design with:
      - id: the latest (highest revision_seq) pending revision ID
      - design_id: the design identifier
      - iteration_count: number of pending revisions in the design
      - title/description/status/created_at from the latest entry
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, title, description, status, created_at, design_id, revision_seq"
            " FROM designs WHERE status = 'pending' ORDER BY revision_seq DESC, created_at DESC"
        ).fetchall()
        # Group by design_id; first row encountered per design is the latest (highest seq)
        design_map: dict = {}
        for r in rows:
            r_dict = {k: r[k] for k in r.keys()}
            did = r_dict.get("design_id") or r_dict["id"]
            if did not in design_map:
                design_map[did] = {
                    "id": r_dict["id"],
                    "design_id": did,
                    "title": r_dict["title"],
                    "description": r_dict["description"],
                    "status": r_dict["status"],
                    "created_at": r_dict["created_at"],
                    "iteration_count": 0,
                }
            design_map[did]["iteration_count"] += 1
        return list(design_map.values())
    finally:
        conn.close()
