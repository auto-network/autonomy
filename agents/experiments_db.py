"""SQLite storage for UI experiments.

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

CREATE_EXPERIMENTS = """\
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

CREATE_VARIANTS = """\
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
        init_db()
        _initialized = True


def _get_conn() -> sqlite3.Connection:
    _ensure_init()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = _get_conn()
    try:
        conn.execute(CREATE_EXPERIMENTS)
        conn.execute(CREATE_VARIANTS)
        # Migrations: add series columns if they don't exist yet
        for stmt in [
            "ALTER TABLE experiments ADD COLUMN series_id TEXT",
            "ALTER TABLE experiments ADD COLUMN series_seq INTEGER",
            "ALTER TABLE experiments ADD COLUMN alpine INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # Column already exists
        # Migrate existing rows: standalone experiments get series_id = id, series_seq = 1
        conn.execute(
            "UPDATE experiments SET series_id = id, series_seq = 1 WHERE series_id IS NULL"
        )
        conn.commit()
    finally:
        conn.close()


def create_experiment(
    *,
    title: str,
    description: str | None = None,
    fixture: str | None = None,
    variants: list[dict],
    series_id: str | None = None,
    alpine: bool = False,
) -> str:
    """Create an experiment with variants. Returns the experiment UUID.

    If series_id is provided the new experiment is appended to that series
    with series_seq = MAX(series_seq) + 1.  If omitted the experiment is
    standalone: series_id = its own id, series_seq = 1.
    """
    exp_id = str(uuid.uuid4())
    conn = _get_conn()
    try:
        if series_id:
            row = conn.execute(
                "SELECT MAX(series_seq) FROM experiments WHERE series_id = ?",
                (series_id,),
            ).fetchone()
            series_seq = (row[0] or 0) + 1
        else:
            series_id = exp_id
            series_seq = 1
        conn.execute(
            "INSERT INTO experiments (id, title, description, fixture, series_id, series_seq, alpine)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (exp_id, title, description, fixture, series_id, series_seq, int(alpine)),
        )
        for v in variants:
            conn.execute(
                "INSERT INTO experiment_variants (id, experiment_id, html) VALUES (?, ?, ?)",
                (v["id"], exp_id, v["html"]),
            )
        conn.commit()
        return exp_id
    finally:
        conn.close()


def resolve_experiment_prefix(partial_id: str) -> tuple[str | None, list[str] | None]:
    """Resolve a partial experiment UUID to a full UUID via prefix match.

    Returns (full_id, None) on unique match, (None, [matches]) on ambiguous,
    (None, None) on no match. Full UUIDs (>=36 chars) pass through unchanged.
    """
    if len(partial_id) >= 36:
        return partial_id, None
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id FROM experiments WHERE id LIKE ?", (f"{partial_id}%",)
        ).fetchall()
        if len(rows) == 1:
            return rows[0]["id"], None
        if len(rows) > 1:
            return None, [r["id"] for r in rows]
        return None, None
    finally:
        conn.close()


def get_experiment(exp_id: str) -> dict | None:
    """Get experiment with its variants, series info, and sibling_ids."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM experiments WHERE id = ?", (exp_id,)
        ).fetchone()
        if not row:
            return None
        exp = {k: row[k] for k in row.keys()}
        variants = conn.execute(
            "SELECT * FROM experiment_variants WHERE experiment_id = ? ORDER BY id",
            (exp_id,),
        ).fetchall()
        exp["variants"] = [{k: v[k] for k in v.keys()} for v in variants]
        # Populate sibling_ids (all experiments in same series, ordered by seq)
        sid = exp.get("series_id") or exp_id
        siblings = conn.execute(
            "SELECT id FROM experiments WHERE series_id = ? ORDER BY series_seq",
            (sid,),
        ).fetchall()
        exp["sibling_ids"] = [s["id"] for s in siblings]
        return exp
    finally:
        conn.close()


def submit_results(exp_id: str, selections: list[dict]) -> bool:
    """Submit ranking results. Each selection has id and rank.

    Returns True if the experiment was found and updated.
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT status FROM experiments WHERE id = ?", (exp_id,)
        ).fetchone()
        if not row:
            return False

        # Reset all variants first
        conn.execute(
            "UPDATE experiment_variants SET selected = 0, rank = NULL WHERE experiment_id = ?",
            (exp_id,),
        )
        # Set selected variants with ranks
        for sel in selections:
            conn.execute(
                "UPDATE experiment_variants SET selected = 1, rank = ? WHERE id = ? AND experiment_id = ?",
                (sel.get("rank"), sel["id"], exp_id),
            )
        # Mark experiment completed
        conn.execute(
            "UPDATE experiments SET status = 'completed' WHERE id = ?",
            (exp_id,),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def dismiss_experiment(exp_id: str) -> bool:
    """Mark an experiment and all pending siblings in its series as dismissed.

    Returns True if the experiment was found.
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT series_id FROM experiments WHERE id = ?", (exp_id,)
        ).fetchone()
        if not row:
            return False
        series_id = row["series_id"] or exp_id
        conn.execute(
            "UPDATE experiments SET status = 'dismissed'"
            " WHERE series_id = ? AND status = 'pending'",
            (series_id,),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def list_pending() -> list[dict]:
    """List pending experiments grouped by series.

    Returns one entry per series with:
      - id: the latest (highest series_seq) pending experiment ID
      - series_id: the series identifier
      - iteration_count: number of pending experiments in the series
      - title/description/status/created_at from the latest entry
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, title, description, status, created_at, series_id, series_seq"
            " FROM experiments WHERE status = 'pending' ORDER BY series_seq DESC, created_at DESC"
        ).fetchall()
        # Group by series_id; first row encountered per series is the latest (highest seq)
        series_map: dict = {}
        for r in rows:
            r_dict = {k: r[k] for k in r.keys()}
            sid = r_dict.get("series_id") or r_dict["id"]
            if sid not in series_map:
                series_map[sid] = {
                    "id": r_dict["id"],
                    "series_id": sid,
                    "title": r_dict["title"],
                    "description": r_dict["description"],
                    "status": r_dict["status"],
                    "created_at": r_dict["created_at"],
                    "iteration_count": 0,
                }
            series_map[sid]["iteration_count"] += 1
        return list(series_map.values())
    finally:
        conn.close()


