"""SQLite storage for UI experiments.

Agents POST design variants with a shared fixture. Humans rank them
via the dashboard gallery. Results are polled back by the agent.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "experiments.db"

CREATE_EXPERIMENTS = """\
CREATE TABLE IF NOT EXISTS experiments (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT,
  fixture TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
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


def _get_conn() -> sqlite3.Connection:
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
        conn.commit()
    finally:
        conn.close()


def create_experiment(
    *,
    title: str,
    description: str | None = None,
    fixture: str | None = None,
    variants: list[dict],
) -> str:
    """Create an experiment with variants. Returns the experiment UUID."""
    exp_id = str(uuid.uuid4())
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO experiments (id, title, description, fixture) VALUES (?, ?, ?, ?)",
            (exp_id, title, description, fixture),
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


def get_experiment(exp_id: str) -> dict | None:
    """Get experiment with its variants."""
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


def list_pending() -> list[dict]:
    """List experiments with status='pending'."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT id, title, description, status, created_at FROM experiments "
            "WHERE status = 'pending' ORDER BY created_at DESC"
        ).fetchall()
        return [{k: r[k] for k in r.keys()} for r in rows]
    finally:
        conn.close()


# Auto-init on import
init_db()
