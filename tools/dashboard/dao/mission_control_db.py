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

-- P2 identity shim: a token is the bearer secret (never displayed back);
-- participant_id is the independent, display-safe identifier used in
-- conversation history and (future, additive) Presence rows. Keeping
-- them distinct is what makes the cookie-vs-storage separation in
-- mission_conversation below sound -- see ask_question()'s docstring.
CREATE TABLE IF NOT EXISTS visitor_tokens (
    token           TEXT PRIMARY KEY,
    participant_id  TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS mission_conversation (
    entry_id                  TEXT PRIMARY KEY,
    mission_id                 TEXT NOT NULL,
    question                   TEXT NOT NULL,
    asked_by_participant_id   TEXT NOT NULL,
    asked_by_label             TEXT NOT NULL,
    answer                     TEXT,
    answered_by_session       TEXT,
    answered_at                REAL,
    relay_status                TEXT NOT NULL DEFAULT 'pending',
    created_at                  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mission_conversation_mission
    ON mission_conversation(mission_id);
"""


def _db_path(db_path: Path | str | None = None) -> Path:
    return Path(db_path) if db_path is not None else DB_PATH


def _get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with the schema guaranteed present.

    Schema-ensure lives here, not just on write paths — a fresh deployment's
    very first request can be a read (list/get), and a bare "no such table"
    from sqlite3 surfaces to the caller as an unhandled 500. CREATE TABLE
    IF NOT EXISTS is cheap enough to run on every connection.
    """
    path = _db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(CREATE_TABLES)
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    _get_conn(db_path).close()


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


# ── Visitor identity shim (P2) ────────────────────────────────────
#
# resolve_visitor() is the one narrow interface: everything downstream
# (Q&A attribution today, Presence integration if/when approved) calls
# through it rather than knowing about tokens directly, so swapping the
# shim for real multi-user identities later is one function deep.


def create_visitor_token(
    display_name: str, *, db_path: Path | str | None = None,
) -> dict:
    """Mint a token for a person the operator is handing a share link to.

    Global, not mission-scoped: a person is a person regardless of which
    mission site they're looking at. ``participant_id`` is minted
    independently of the token -- safe to display/store (conversation
    history, future Presence rows); the raw token is the bearer secret
    and is returned here ONCE, never again.
    """
    token = uuid.uuid4().hex + uuid.uuid4().hex  # 256 bits, unguessable
    participant_id = f"guest:{uuid.uuid4()}"
    created_at = time.time()
    conn = _get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO visitor_tokens (token, participant_id, display_name, created_at)"
            " VALUES (?, ?, ?, ?)",
            (token, participant_id, display_name, created_at),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "token": token,
        "participant_id": participant_id,
        "display_name": display_name,
    }


def resolve_visitor(
    token: str, *, db_path: Path | str | None = None,
) -> dict | None:
    """token -> {participant_id, participant_label}, or None if unknown.

    Never returns the token back. Callers (the cookie-setting flow, the
    ask-question route) must not persist or display the raw token
    anywhere outside this lookup -- it is the ONLY thing that
    authenticates a visitor; participant_id is display/storage only.
    """
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT participant_id, display_name FROM visitor_tokens WHERE token = ?",
            (token,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "participant_id": row["participant_id"],
        "participant_label": row["display_name"],
    }


# ── Mission conversation (P2 Q&A) ─────────────────────────────────


def ask_question(
    mission_id: str,
    question: str,
    participant_id: str,
    participant_label: str,
    *,
    db_path: Path | str | None = None,
) -> dict | None:
    """Record a visitor's question. Returns None if the mission doesn't exist.

    Attribution is a LABEL SNAPSHOT at ask time (asked_by_label), not a
    live join against visitor_tokens: this is a historical record of what
    the asker was called when they asked, not a live view that changes
    if a display name is edited later. relay_status starts 'pending' --
    the caller is expected to attempt CrossTalk delivery as a background
    step and update it via mark_question_relay_status.
    """
    entry_id = str(uuid.uuid4())
    created_at = time.time()
    conn = _get_conn(db_path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM missions WHERE mission_id = ?", (mission_id,)
        ).fetchone()
        if not exists:
            return None
        conn.execute(
            "INSERT INTO mission_conversation"
            " (entry_id, mission_id, question, asked_by_participant_id,"
            "  asked_by_label, answer, answered_by_session, answered_at,"
            "  relay_status, created_at)"
            " VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, 'pending', ?)",
            (entry_id, mission_id, question, participant_id, participant_label, created_at),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "entry_id": entry_id,
        "mission_id": mission_id,
        "question": question,
        "asked_by_participant_id": participant_id,
        "asked_by_label": participant_label,
        "answer": None,
        "answered_by_session": None,
        "answered_at": None,
        "relay_status": "pending",
        "created_at": created_at,
    }


def get_question(
    mission_id: str, entry_id: str, *, db_path: Path | str | None = None,
) -> dict | None:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM mission_conversation WHERE mission_id = ? AND entry_id = ?",
            (mission_id, entry_id),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_conversation(
    mission_id: str, *, db_path: Path | str | None = None,
) -> list[dict]:
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM mission_conversation WHERE mission_id = ?"
            " ORDER BY created_at ASC",
            (mission_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def mark_question_relay_status(
    mission_id: str, entry_id: str, status: str, *, db_path: Path | str | None = None,
) -> None:
    """Record whether CrossTalk delivery was attempted successfully.

    'sent' means the send call completed without raising -- tmux_send
    degrades silently for a dead/missing target session (see
    tools.dashboard.surface_actions.CrosstalkService), so this is NOT
    proof the coordinator session received or read the question. It
    exists to make delivery failures observable rather than silent; the
    durable source of truth is the conversation list itself (GET .../questions),
    which the coordinator polls as the actual backstop.
    """
    assert status in ("pending", "sent", "failed"), status
    conn = _get_conn(db_path)
    try:
        conn.execute(
            "UPDATE mission_conversation SET relay_status = ?"
            " WHERE mission_id = ? AND entry_id = ?",
            (status, mission_id, entry_id),
        )
        conn.commit()
    finally:
        conn.close()


def answer_question(
    mission_id: str,
    entry_id: str,
    answer: str,
    answered_by_session: str,
    *,
    db_path: Path | str | None = None,
) -> dict | None:
    """Record the coordinator's final answer. Returns None if the entry
    doesn't exist.

    answered_by_session is a snapshot of who actually answered, taken
    fresh at answer time by the caller -- coordinator_session on the
    mission row is mutable by design (a mission can change hands), so
    history must record the real answerer, not whatever the mission row
    says later.
    """
    answered_at = time.time()
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE mission_conversation"
            " SET answer = ?, answered_by_session = ?, answered_at = ?"
            " WHERE mission_id = ? AND entry_id = ?",
            (answer, answered_by_session, answered_at, mission_id, entry_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM mission_conversation WHERE mission_id = ? AND entry_id = ?",
            (mission_id, entry_id),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None
