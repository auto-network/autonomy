"""Mission Control store — missions and their site revision history.

Mission Control's own storage, deliberately not a foreign key into Design
Studio's ``designs``/``revision_variants`` tables. It copies that store's
*pattern* — immutable, append-only revisions under a stable id, one current
pointer, one obvious content route — without inheriting Design Studio's
duplicate-title guard, ``alpine`` flag, or slide-detection assumptions,
none of which apply to a chromeless mission site.

A push (``push_site_revision``) both stores AND publishes: there is no
second "mark shown" step. Present's deck library required exactly that
extra call, undocumented, because it was bolted on as a Settings-set
membership layer over a store it didn't own; Mission Control owns its
store outright, so "current" is just a column update in the same
transaction as the insert.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

from tools.data_paths import resolve_store

DB_PATH = resolve_store("mission_control")

CREATE_TABLES = """\
CREATE TABLE IF NOT EXISTS missions (
    mission_id            TEXT PRIMARY KEY,
    name                   TEXT NOT NULL,
    coordinator_session    TEXT NOT NULL DEFAULT '',
    created_at             REAL NOT NULL,
    current_revision_id   TEXT
);

CREATE TABLE IF NOT EXISTS mission_site_revisions (
    revision_id     TEXT PRIMARY KEY,
    mission_id      TEXT NOT NULL,
    revision_seq    INTEGER NOT NULL,
    html            TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    UNIQUE (mission_id, revision_seq)
);

CREATE INDEX IF NOT EXISTS idx_mission_site_revisions_mission
    ON mission_site_revisions(mission_id);
"""


def _db_path(db_path: Path | str | None = None) -> Path:
    return Path(db_path) if db_path is not None else DB_PATH


def _get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = _db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    conn = _get_conn(db_path)
    try:
        conn.executescript(CREATE_TABLES)
        conn.commit()
    finally:
        conn.close()


# ── Missions ─────────────────────────────────────────────────────


def create_mission(
    name: str,
    coordinator_session: str = "",
    *,
    db_path: Path | str | None = None,
) -> dict:
    """Create a mission. Entity is deliberately minimal: id, name,
    coordinator_session, created_at. No status, register, or conversation
    model — those arrive with their own phases, not guessed at here."""
    mission_id = str(uuid.uuid4())
    created_at = time.time()
    conn = _get_conn(db_path)
    try:
        init_schema_on_connection(conn)
        conn.execute(
            "INSERT INTO missions (mission_id, name, coordinator_session, created_at, current_revision_id)"
            " VALUES (?, ?, ?, ?, NULL)",
            (mission_id, name, coordinator_session, created_at),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "mission_id": mission_id,
        "name": name,
        "coordinator_session": coordinator_session,
        "created_at": created_at,
        "current_revision_id": None,
    }


def get_mission(mission_id: str, *, db_path: Path | str | None = None) -> dict | None:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM missions WHERE mission_id = ?", (mission_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_missions(*, db_path: Path | str | None = None) -> list[dict]:
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM missions ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def delete_mission(mission_id: str, *, db_path: Path | str | None = None) -> bool:
    """Hard-delete a mission and its full revision history.

    Returns True if a row was removed, False if the mission didn't exist.
    """
    conn = _get_conn(db_path)
    try:
        cur = conn.execute("DELETE FROM missions WHERE mission_id = ?", (mission_id,))
        conn.execute(
            "DELETE FROM mission_site_revisions WHERE mission_id = ?", (mission_id,)
        )
        conn.commit()
    finally:
        conn.close()
    return cur.rowcount > 0


# ── Site revisions ───────────────────────────────────────────────


def push_site_revision(
    mission_id: str,
    html: str,
    note: str = "",
    *,
    db_path: Path | str | None = None,
) -> dict | None:
    """Append a new immutable revision and make it current, atomically.

    One call does push AND publish — there is no separate step. Returns
    None if the mission doesn't exist.
    """
    revision_id = str(uuid.uuid4())
    created_at = time.time()
    conn = _get_conn(db_path)
    try:
        init_schema_on_connection(conn)
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute(
            "SELECT 1 FROM missions WHERE mission_id = ?", (mission_id,)
        ).fetchone()
        if not exists:
            conn.rollback()
            return None
        row = conn.execute(
            "SELECT MAX(revision_seq) FROM mission_site_revisions WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        revision_seq = (row[0] or 0) + 1
        conn.execute(
            "INSERT INTO mission_site_revisions"
            " (revision_id, mission_id, revision_seq, html, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (revision_id, mission_id, revision_seq, html, note, created_at),
        )
        conn.execute(
            "UPDATE missions SET current_revision_id = ? WHERE mission_id = ?",
            (revision_id, mission_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "revision_id": revision_id,
        "mission_id": mission_id,
        "revision_seq": revision_seq,
        "note": note,
        "created_at": created_at,
    }


def get_current_site(mission_id: str, *, db_path: Path | str | None = None) -> dict | None:
    """Current revision's full metadata + content in one read — no
    status/full split."""
    conn = _get_conn(db_path)
    try:
        mission = conn.execute(
            "SELECT current_revision_id FROM missions WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        if not mission or not mission["current_revision_id"]:
            return None
        row = conn.execute(
            "SELECT * FROM mission_site_revisions WHERE revision_id = ?",
            (mission["current_revision_id"],),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_site_revisions(mission_id: str, *, db_path: Path | str | None = None) -> list[dict]:
    """Revision history metadata (no HTML content — use get_site_revision
    for a specific one)."""
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT revision_id, mission_id, revision_seq, note, created_at,"
            " LENGTH(html) AS byte_size"
            " FROM mission_site_revisions WHERE mission_id = ?"
            " ORDER BY revision_seq DESC",
            (mission_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_site_revision(
    mission_id: str, revision_id: str, *, db_path: Path | str | None = None
) -> dict | None:
    """One immutable historical revision, full content included."""
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM mission_site_revisions WHERE mission_id = ? AND revision_id = ?",
            (mission_id, revision_id),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def activate_site_revision(
    mission_id: str, revision_id: str, *, db_path: Path | str | None = None
) -> bool:
    """Roll the current pointer back to an existing revision without
    re-pushing content — preserves honest history (the coordinator has
    pushed bad revisions before; re-pushing old content would fabricate a
    new revision rather than record what actually happened).

    Returns True on success, False if the mission or revision doesn't exist.
    """
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM mission_site_revisions WHERE mission_id = ? AND revision_id = ?",
            (mission_id, revision_id),
        ).fetchone()
        if not row:
            return False
        cur = conn.execute(
            "UPDATE missions SET current_revision_id = ? WHERE mission_id = ?",
            (revision_id, mission_id),
        )
        conn.commit()
    finally:
        conn.close()
    return cur.rowcount > 0


def init_schema_on_connection(conn: sqlite3.Connection) -> None:
    """Idempotent inline schema-ensure so a stray fresh DB (e.g. under a
    tmp_path in tests) never raises 'no such table' on first write."""
    conn.executescript(CREATE_TABLES)
