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

import os
import sqlite3
import time
import uuid
from pathlib import Path

from tools.data_paths import resolve_store

DB_PATH = resolve_store("mission_control")

# ``missions.org`` is the organization a mission belongs to, and is the only
# boundary a mission has: sessions in that organization reach it, sessions in
# another do not. Nothing finer exists -- a mission has no members and no
# per-session permissions, so that column is what "who may touch this" means,
# in full. Everything under a mission inherits it; a pillar carries no
# organization of its own, because a pillar in a different organization from
# its mission is not a thing that can exist.
#
# Keep comments out of the CREATE TABLE text itself. SQLite stores the
# statement verbatim and re-parses it when a column is dropped, and a comment
# inside the definition makes that fail with "incomplete input" -- so an
# annotated table is one nothing can ever alter again.
CREATE_TABLES = """\
CREATE TABLE IF NOT EXISTS missions (
    mission_id            TEXT PRIMARY KEY,
    name                   TEXT NOT NULL,
    coordinator_session    TEXT NOT NULL DEFAULT '',
    created_at             REAL NOT NULL,
    current_revision_id   TEXT,
    status                 TEXT NOT NULL DEFAULT 'active',
    org                    TEXT NOT NULL DEFAULT ''
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
    created_at                  REAL NOT NULL,
    -- Retired: the subject stopped being relevant. NOT deleted -- the record
    -- of what was asked and answered is the point of the log. A retired entry
    -- leaves the screen and stops counting as open; it stays readable.
    retired_at                   REAL,
    retired_note                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_mission_conversation_mission
    ON mission_conversation(mission_id);

-- Single implicit "last seen" watermark per mission -- not per-viewer.
-- There is no per-participant identity system yet (see
-- SKILL.md "Presence and what changed"), so whoever expands a mission's
-- detail panel first consumes the delta for every other viewer. Revisit
-- once real multi-viewer identity exists.
CREATE TABLE IF NOT EXISTS mission_last_seen (
    mission_id      TEXT PRIMARY KEY,
    last_seen_at    REAL NOT NULL
);

-- A pillar is a sub-mission: its own dedicated coordinator_session, its
-- own site-revision history, its own presence surface (pillar:<id>). A
-- structural copy of `missions` one level down -- same entity shape, same
-- lifecycle model -- not a new abstraction. mission_id is a plain FK, not
-- enforced (this store has never used foreign keys -- see missions/
-- mission_site_revisions above), cleaned up by delete_mission's cascade.
CREATE TABLE IF NOT EXISTS pillars (
    pillar_id             TEXT PRIMARY KEY,
    mission_id             TEXT NOT NULL,
    name                    TEXT NOT NULL,
    coordinator_session     TEXT NOT NULL DEFAULT '',
    color                    TEXT NOT NULL DEFAULT '',
    created_at               REAL NOT NULL,
    current_revision_id     TEXT,
    status                   TEXT NOT NULL DEFAULT 'active',
    -- The last productive thing this pillar's coordinator finished, in
    -- their own words. Current state, NOT a log: a new one replaces the
    -- previous, and nothing accumulates. NULL means never written, which
    -- the viewer renders as nothing rather than as a guess.
    last_done                TEXT,
    last_done_at             REAL
);

CREATE INDEX IF NOT EXISTS idx_pillars_mission
    ON pillars(mission_id);

CREATE TABLE IF NOT EXISTS pillar_site_revisions (
    revision_id     TEXT PRIMARY KEY,
    pillar_id       TEXT NOT NULL,
    revision_seq    INTEGER NOT NULL,
    html            TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    UNIQUE (pillar_id, revision_seq)
);

CREATE INDEX IF NOT EXISTS idx_pillar_site_revisions_pillar
    ON pillar_site_revisions(pillar_id);

-- Every status line a pillar has ever written, oldest to newest. The
-- pillars.last_done column keeps the CURRENT one because the card reads it
-- on every render and should not pay for a query; this table keeps the rest.
--
-- One line per thing that finished, so read together they are the account of
-- what happened that no single current-state field can give. The reason this
-- exists is that a reader could see what is true now and still have no way to
-- see what had been going on.
CREATE TABLE IF NOT EXISTS pillar_status_posts (
    post_id     TEXT PRIMARY KEY,
    pillar_id   TEXT NOT NULL,
    text        TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pillar_status_posts_pillar
    ON pillar_status_posts(pillar_id, created_at DESC);

-- Same single-implicit-watermark posture as mission_last_seen, one level
-- down.
CREATE TABLE IF NOT EXISTS pillar_last_seen (
    pillar_id       TEXT PRIMARY KEY,
    last_seen_at    REAL NOT NULL
);

-- Interim visibility on a still-open conversation entry ("still working --
-- capturing the new screenshot") without closing it out. Deliberately NOT
-- part of mission_conversation itself: an entry has exactly one `answer`
-- (or none yet); it can have any number of updates on the way there.
CREATE TABLE IF NOT EXISTS mission_conversation_updates (
    update_id    TEXT PRIMARY KEY,
    entry_id     TEXT NOT NULL,
    text         TEXT NOT NULL,
    created_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversation_updates_entry
    ON mission_conversation_updates(entry_id);

-- Idle-nag cooldown tracking for "you have outstanding Mission Control
-- questions", keyed by coordinator_session -- deliberately separate from
-- tmux_sessions' own nag_enabled/nag_message/nag_interval/nag_last_sent
-- columns (dashboard_db.py), which are a single session-owned slot for a
-- human-configured general check-in nag (`graph set-nag`). Writing this
-- feature's message into that slot would silently clobber whatever a
-- session already configured for itself.
CREATE TABLE IF NOT EXISTS coordinator_nag_state (
    coordinator_session    TEXT PRIMARY KEY,
    last_nagged_at          REAL NOT NULL
);
"""

#: Explicit lifecycle state, coordinator-set (never inferred from staleness --
#: a mission that's genuinely done looks identical to one that's stalled, so
#: guessing from recency would be actively misleading). Default 'active' on
#: creation.
VALID_MISSION_STATUSES = ("active", "paused", "complete")


def _default_org() -> str:
    """The organization a mission belongs to when the caller did not say.

    Sessions run under GRAPH_ORG; a host process without it is the
    dashboard's own organization. Falling back to the literal is
    deliberate: a mission with no organization has no boundary at all, and
    an unbounded mission is worse than one attributed to the wrong place,
    which is visible and correctable.
    """
    return os.environ.get("GRAPH_ORG") or "autonomy"


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
    # Migrate: add status column if missing (for databases created before
    # the mission lifecycle field existed).
    try:
        conn.execute("SELECT status FROM missions LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE missions ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
        conn.commit()
    # Migrate: add pillar_id/anchor columns if missing (for databases
    # created before pillars existed). NULL on every pre-existing row --
    # pillar_id IS NULL is exactly "mission-level", today's only meaning.
    try:
        conn.execute("SELECT pillar_id, anchor FROM mission_conversation LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE mission_conversation ADD COLUMN pillar_id TEXT")
        conn.execute("ALTER TABLE mission_conversation ADD COLUMN anchor TEXT")
        conn.commit()
    # Migrate: a guest's face, stored as a REFERENCE into the graph's
    # content-addressed attachment store -- never as bytes here. That
    # store already gives hash dedup (one copy however many participants
    # share a photo), a same-origin serving route, alt-text, and a
    # resumable relay fetch protocol the bootloader already speaks. NULL
    # is a real state: a participant without a photo renders the
    # initial-and-color avatar derived from participant_id.
    # Migrate: re-anchoring and retiring a conversation entry. Screens get
    # restructured, and a question must be able to follow its subject to a new
    # anchor or be retired when the subject stops mattering -- otherwise old
    # questions freeze the layout of the page they were asked about.
    try:
        conn.execute("SELECT retired_at, retired_note FROM mission_conversation LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE mission_conversation ADD COLUMN retired_at REAL")
        conn.execute("ALTER TABLE mission_conversation ADD COLUMN retired_note TEXT")
        conn.commit()
    # Migrate: the pillar's own status line (auto-fm22y). Deliberately not
    # derived from anything -- no summarising of revisions, no inference from
    # session activity. A coordinator writes it or it stays NULL.
    try:
        conn.execute("SELECT last_done, last_done_at FROM pillars LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE pillars ADD COLUMN last_done TEXT")
        conn.execute("ALTER TABLE pillars ADD COLUMN last_done_at REAL")
        conn.commit()
    # A question's wording can be corrected. The pair is read later by
    # someone working out where the mission stands, and a framing that cannot
    # be fixed stops being worth reading -- the same reason answers have
    # always been overwritable.
    try:
        conn.execute(
            "SELECT question_edited_at, question_edited_by_session"
            " FROM mission_conversation LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute(
            "ALTER TABLE mission_conversation ADD COLUMN question_edited_at REAL")
        conn.execute(
            "ALTER TABLE mission_conversation"
            " ADD COLUMN question_edited_by_session TEXT")
        conn.commit()
    try:
        conn.execute("SELECT avatar_attachment_id FROM visitor_tokens LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE visitor_tokens ADD COLUMN avatar_attachment_id TEXT")
        conn.commit()
    # CLOSED IS A FACT, NOT AN INFERENCE. It used to be read off the answer:
    # an entry with text in `answer` was closed and one without was open. That
    # conflates a thing arriving with a thing being finished, and it made
    # answering your own question close it using your own words -- the asker
    # and the reply were the same person and nothing could tell.
    #
    # Answering still closes in the same act, so a coordinator owes no extra
    # step. What it buys is the two states inference could not express: a
    # question its own asker has added to and left open, and one its asker
    # closed without anybody needing to answer it.
    #
    # Backfill matters: every entry that already has an answer is closed, and
    # reading `closed_at IS NULL` on those without this would silently reopen
    # the entire history.
    # WHAT IT IS ABOUT, IN THE AUTHOR'S WORDS. An anchor is a slug the screen's
    # own author picked -- "decision:settings-invariant-bump" -- and it was the
    # only context travelling with a question. A short answer to something the
    # screen raised ("Do it") reached its coordinator as that slug and two
    # words, and the reminder afterwards dropped even the slug. The page knows
    # the heading the anchor sits under; it just had nowhere to put it.
    try:
        conn.execute("SELECT anchor_title FROM mission_conversation LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE mission_conversation ADD COLUMN anchor_title TEXT")
        conn.commit()
    # WHAT PRECIPITATED THE QUESTION. A heading names the thing being replied
    # to; it does not say what it said. "(re: Now)" reaches a coordinator with
    # no indication of WHICH status report is being answered, which is the
    # same fault as sending the anchor slug and one level less abstract.
    try:
        conn.execute("SELECT anchor_excerpt FROM mission_conversation LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE mission_conversation ADD COLUMN anchor_excerpt TEXT")
        conn.commit()
    # A CONVERSATION, NOT A PAIR. A reply is not presumed to be the answer:
    # the first one is very often a question of its own or a partial, and
    # treating it as final ends an exchange that had barely started. So an
    # entry carries every round, each with who said it and what kind of thing
    # it was -- something said, volatile progress, or an automatic receipt --
    # and they are never dropped.
    #
    # Existing rows are progress written by the coordinator, which is what
    # they were: kind 'status', author unknown rather than invented.
    try:
        conn.execute(
            "SELECT author_participant_id, author_label, kind"
            " FROM mission_conversation_updates LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute(
            "ALTER TABLE mission_conversation_updates"
            " ADD COLUMN author_participant_id TEXT")
        conn.execute(
            "ALTER TABLE mission_conversation_updates ADD COLUMN author_label TEXT")
        conn.execute(
            "ALTER TABLE mission_conversation_updates"
            " ADD COLUMN kind TEXT NOT NULL DEFAULT 'status'")
        conn.commit()
    # Migrate: the organization a mission belongs to.
    #
    # Every mission that existed before this column was created by the
    # single organization running this dashboard, so they are backfilled to
    # it rather than left empty. An empty organization would read as "no
    # boundary" and is the one value that must never appear on a real row.
    try:
        conn.execute("SELECT org FROM missions LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE missions ADD COLUMN org TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "UPDATE missions SET org = ? WHERE org = ''", (_default_org(),))
        conn.commit()
    try:
        conn.execute("SELECT closed_at, closed_by FROM mission_conversation LIMIT 0")
    except sqlite3.OperationalError:
        conn.execute("ALTER TABLE mission_conversation ADD COLUMN closed_at REAL")
        conn.execute("ALTER TABLE mission_conversation ADD COLUMN closed_by TEXT")
        conn.execute(
            "UPDATE mission_conversation"
            "   SET closed_at = COALESCE(answered_at, created_at),"
            "       closed_by = answered_by_session"
            " WHERE answer IS NOT NULL")
        conn.commit()
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    _get_conn(db_path).close()


# ── Missions ─────────────────────────────────────────────────────


def create_mission(
    name: str,
    coordinator_session: str = "",
    *,
    org: str | None = None,
    db_path: Path | str | None = None,
) -> dict:
    """Create a mission. Entity is deliberately minimal: id, name,
    coordinator_session, created_at, status. No register or conversation
    model — those arrive with their own phases, not guessed at here.
    Status starts 'active' (the column default) — explicit, coordinator-set
    from here on, never inferred.

    *org* is the organization the mission belongs to, and is the only
    boundary it has: sessions in that organization reach it and sessions in
    another do not. Omitting it takes the caller's own organization, which
    is right for every caller that has one; it is never stored empty,
    because an empty organization is a mission nothing is bounded by."""
    mission_id = str(uuid.uuid4())
    created_at = time.time()
    org = (org or "").strip() or _default_org()
    conn = _get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO missions (mission_id, name, coordinator_session, created_at, current_revision_id, org)"
            " VALUES (?, ?, ?, ?, NULL, ?)",
            (mission_id, name, coordinator_session, created_at, org),
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
        "status": "active",
        "org": org,
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


def set_mission_status(
    mission_id: str, status: str, *, db_path: Path | str | None = None,
) -> bool:
    """Set a mission's lifecycle status. Coordinator-set, never inferred.

    Returns True on success, False if the mission doesn't exist.
    """
    assert status in VALID_MISSION_STATUSES, status
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE missions SET status = ? WHERE mission_id = ?",
            (status, mission_id),
        )
        conn.commit()
    finally:
        conn.close()
    return cur.rowcount > 0


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
        # Pre-existing gap, fixed opportunistically while touching this
        # function's cleanup list: conversation rows and the last-seen
        # watermark also key off mission_id and were never swept here.
        conn.execute(
            "DELETE FROM mission_conversation WHERE mission_id = ?", (mission_id,)
        )
        conn.execute(
            "DELETE FROM mission_last_seen WHERE mission_id = ?", (mission_id,)
        )
        # Cascade to pillars -- a pillar has no independent existence
        # outside its parent mission.
        pillar_ids = [
            r[0] for r in conn.execute(
                "SELECT pillar_id FROM pillars WHERE mission_id = ?", (mission_id,)
            ).fetchall()
        ]
        for pillar_id in pillar_ids:
            conn.execute("DELETE FROM pillar_site_revisions WHERE pillar_id = ?", (pillar_id,))
            conn.execute("DELETE FROM pillar_last_seen WHERE pillar_id = ?", (pillar_id,))
        conn.execute("DELETE FROM pillars WHERE mission_id = ?", (mission_id,))
        conn.commit()
    finally:
        conn.close()
    return cur.rowcount > 0


# ── Pillars ──────────────────────────────────────────────────────
#
# A pillar is a sub-mission: same entity shape as `missions` above (id,
# name, coordinator_session, created_at, current_revision_id, status),
# scoped under a parent mission_id instead of standing alone. Every
# function here is a structural mirror of its mission-level counterpart --
# same pattern, one level down -- not a new abstraction.


def create_pillar(
    mission_id: str,
    name: str,
    coordinator_session: str = "",
    color: str = "",
    *,
    db_path: Path | str | None = None,
) -> dict:
    """Create a pillar under a mission. Does not verify the mission exists
    -- mirrors create_mission's own lack of existence-checking (there is no
    parent to check at the mission level); callers that need the guarantee
    check via get_mission first, as the API layer does."""
    pillar_id = str(uuid.uuid4())
    created_at = time.time()
    conn = _get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO pillars"
            " (pillar_id, mission_id, name, coordinator_session, color, created_at, current_revision_id)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (pillar_id, mission_id, name, coordinator_session, color, created_at),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "pillar_id": pillar_id,
        "mission_id": mission_id,
        "name": name,
        "coordinator_session": coordinator_session,
        "color": color,
        "created_at": created_at,
        "current_revision_id": None,
        "status": "active",
        # Present and NULL, so this dict has the same shape a SELECT gives
        # back. A hand-built return that omits new columns is a KeyError
        # waiting in every caller that treats the two as interchangeable.
        "last_done": None,
        "last_done_at": None,
    }


def get_pillar(pillar_id: str, *, db_path: Path | str | None = None) -> dict | None:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM pillars WHERE pillar_id = ?", (pillar_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_pillars(mission_id: str, *, db_path: Path | str | None = None) -> list[dict]:
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM pillars WHERE mission_id = ? ORDER BY created_at ASC",
            (mission_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def set_pillar_status(
    pillar_id: str, status: str, *, db_path: Path | str | None = None,
) -> bool:
    """Same lifecycle values as a mission (VALID_MISSION_STATUSES) -- one
    shared vocabulary, not a parallel enum."""
    assert status in VALID_MISSION_STATUSES, status
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE pillars SET status = ? WHERE pillar_id = ?",
            (status, pillar_id),
        )
        conn.commit()
    finally:
        conn.close()
    return cur.rowcount > 0


def set_pillar_coordinator(
    pillar_id: str, coordinator_session: str, *, db_path: Path | str | None = None,
) -> bool:
    """Point a pillar at the session that coordinates it now.

    A pillar changes hands: a session is retired and another takes the seat.
    The field was always meant to move -- it is a pointer to whoever holds the
    work, not a fact about who started it -- and until now nothing could move
    it, so a handover left questions relaying to a session that was going away
    and presence naming the wrong holder.

    Writing this field decides who acts as this pillar:
    get_pillar_by_coordinator resolves a caller to a pillar through it. This
    layer stores what it is given and does not decide who may ask.
    """
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE pillars SET coordinator_session = ? WHERE pillar_id = ?",
            (coordinator_session, pillar_id),
        )
        conn.commit()
    finally:
        conn.close()
    return cur.rowcount > 0


def set_mission_coordinator(
    mission_id: str, coordinator_session: str, *, db_path: Path | str | None = None,
) -> bool:
    """Point a mission at the session that coordinates it now. Same contract
    and the same identity consequence as set_pillar_coordinator."""
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE missions SET coordinator_session = ? WHERE mission_id = ?",
            (coordinator_session, mission_id),
        )
        conn.commit()
    finally:
        conn.close()
    return cur.rowcount > 0


def set_pillar_last_done(
    pillar_id: str, text: str, *, db_path: Path | str | None = None,
) -> bool:
    """Record the last productive thing this pillar finished.

    APPENDS, and updates the current line in the same breath. The column is
    the newest post so the card can render without a query; the history table
    keeps every earlier one. Current state alone could say what is true now
    and still leave a reader unable to see what had been happening, which is
    the whole reason the history exists.

    Blank clears the current line back to "never written" -- the viewer then
    renders nothing -- and appends no post. Clearing is not an event.

    How to write one is not a matter of taste here: SKILL.md section 9 sets
    six rules (two sentences, past tense and finished, no identifiers, no
    jargon, no people or blockers, no context-free numbers). This layer
    stores what it is given -- the rules are enforced by the coordinator
    reading them, not by a validator that would reject good writing it
    failed to parse.
    """
    text = (text or "").strip()
    now = time.time()
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE pillars SET last_done = ?, last_done_at = ? WHERE pillar_id = ?",
            (text or None, now if text else None, pillar_id),
        )
        # Only when the pillar exists AND something was actually said. A write
        # against an unknown pillar must not leave an orphan post behind.
        if text and cur.rowcount > 0:
            conn.execute(
                "INSERT INTO pillar_status_posts (post_id, pillar_id, text, created_at)"
                " VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), pillar_id, text, now),
            )
        conn.commit()
    finally:
        conn.close()
    return cur.rowcount > 0


def list_mission_status_posts(
    mission_id: str, *, limit: int = 100, db_path: Path | str | None = None,
) -> list[dict]:
    """Every pillar's status posts for one mission, newest first.

    Carries the pillar's name and colour so a reader can tell at a glance who
    said what, which is the only thing that makes a merged feed legible.

    This is deliberately NOT the decision log. That log is what LANDED --
    revisions pushed and questions answered -- and its worth is that
    everything in it demonstrably happened. A status post is what a pillar
    SAYS is happening. Merging them would make a claim indistinguishable from
    a fact.
    """
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT s.post_id, s.pillar_id, s.text, s.created_at,
                   p.name AS pillar_name, p.color AS pillar_color
            FROM pillar_status_posts s
            JOIN pillars p ON p.pillar_id = s.pillar_id
            WHERE p.mission_id = ?
            ORDER BY s.created_at DESC, s.post_id DESC
            LIMIT ?
            """,
            (mission_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def delete_pillar(pillar_id: str, *, db_path: Path | str | None = None) -> bool:
    """Hard-delete one pillar and its own revision history. For deleting a
    whole mission (which cascades to all its pillars), see delete_mission."""
    conn = _get_conn(db_path)
    try:
        cur = conn.execute("DELETE FROM pillars WHERE pillar_id = ?", (pillar_id,))
        conn.execute("DELETE FROM pillar_site_revisions WHERE pillar_id = ?", (pillar_id,))
        conn.execute("DELETE FROM pillar_last_seen WHERE pillar_id = ?", (pillar_id,))
        conn.commit()
    finally:
        conn.close()
    return cur.rowcount > 0


# ── Pillar site revisions ───────────────────────────────────────


def push_pillar_site_revision(
    pillar_id: str,
    html: str,
    note: str = "",
    *,
    db_path: Path | str | None = None,
) -> dict | None:
    """Mirrors push_site_revision exactly, one level down: append + make
    current, atomically, in one call. Returns None if the pillar doesn't
    exist."""
    revision_id = str(uuid.uuid4())
    created_at = time.time()
    conn = _get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute(
            "SELECT 1 FROM pillars WHERE pillar_id = ?", (pillar_id,)
        ).fetchone()
        if not exists:
            conn.rollback()
            return None
        row = conn.execute(
            "SELECT MAX(revision_seq) FROM pillar_site_revisions WHERE pillar_id = ?",
            (pillar_id,),
        ).fetchone()
        revision_seq = (row[0] or 0) + 1
        conn.execute(
            "INSERT INTO pillar_site_revisions"
            " (revision_id, pillar_id, revision_seq, html, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (revision_id, pillar_id, revision_seq, html, note, created_at),
        )
        conn.execute(
            "UPDATE pillars SET current_revision_id = ? WHERE pillar_id = ?",
            (revision_id, pillar_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "revision_id": revision_id,
        "pillar_id": pillar_id,
        "revision_seq": revision_seq,
        "note": note,
        "created_at": created_at,
    }


def get_current_pillar_site(pillar_id: str, *, db_path: Path | str | None = None) -> dict | None:
    conn = _get_conn(db_path)
    try:
        pillar = conn.execute(
            "SELECT current_revision_id FROM pillars WHERE pillar_id = ?",
            (pillar_id,),
        ).fetchone()
        if not pillar or not pillar["current_revision_id"]:
            return None
        row = conn.execute(
            "SELECT * FROM pillar_site_revisions WHERE revision_id = ?",
            (pillar["current_revision_id"],),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_pillar_site_revisions(pillar_id: str, *, db_path: Path | str | None = None) -> list[dict]:
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT revision_id, pillar_id, revision_seq, note, created_at,"
            " LENGTH(html) AS byte_size"
            " FROM pillar_site_revisions WHERE pillar_id = ?"
            " ORDER BY revision_seq DESC",
            (pillar_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_pillar_site_revision(
    pillar_id: str, revision_id: str, *, db_path: Path | str | None = None
) -> dict | None:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM pillar_site_revisions WHERE pillar_id = ? AND revision_id = ?",
            (pillar_id, revision_id),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def activate_pillar_site_revision(
    pillar_id: str, revision_id: str, *, db_path: Path | str | None = None
) -> bool:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM pillar_site_revisions WHERE pillar_id = ? AND revision_id = ?",
            (pillar_id, revision_id),
        ).fetchone()
        if not row:
            return False
        cur = conn.execute(
            "UPDATE pillars SET current_revision_id = ? WHERE pillar_id = ?",
            (revision_id, pillar_id),
        )
        conn.commit()
    finally:
        conn.close()
    return cur.rowcount > 0


def get_pillar_last_seen(pillar_id: str, *, db_path: Path | str | None = None) -> float | None:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT last_seen_at FROM pillar_last_seen WHERE pillar_id = ?",
            (pillar_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["last_seen_at"] if row else None


def mark_pillar_seen(pillar_id: str, *, db_path: Path | str | None = None) -> float:
    seen_at = time.time()
    conn = _get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO pillar_last_seen (pillar_id, last_seen_at) VALUES (?, ?)"
            " ON CONFLICT(pillar_id) DO UPDATE SET last_seen_at = excluded.last_seen_at",
            (pillar_id, seen_at),
        )
        conn.commit()
    finally:
        conn.close()
    return seen_at


def list_pillar_site_revisions_since(
    pillar_id: str, since_at: float, *, db_path: Path | str | None = None,
) -> list[dict]:
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT revision_id, pillar_id, revision_seq, note, created_at,"
            " LENGTH(html) AS byte_size"
            " FROM pillar_site_revisions WHERE pillar_id = ? AND created_at > ?"
            " ORDER BY revision_seq DESC",
            (pillar_id, since_at),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


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
    display_name: str, *, avatar_attachment_id: str | None = None,
    db_path: Path | str | None = None,
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
            "INSERT INTO visitor_tokens"
            " (token, participant_id, display_name, created_at, avatar_attachment_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (token, participant_id, display_name, created_at, avatar_attachment_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "token": token,
        "participant_id": participant_id,
        "display_name": display_name,
        "avatar_attachment_id": avatar_attachment_id,
    }


def set_visitor_avatar(
    participant_id: str, avatar_attachment_id: str | None, *,
    db_path: Path | str | None = None,
) -> dict | None:
    """Point a guest at an attachment (or clear it). Stores the reference
    only -- the bytes live once in the graph's attachment store."""
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE visitor_tokens SET avatar_attachment_id = ? WHERE participant_id = ?",
            (avatar_attachment_id, participant_id),
        )
        conn.commit()
        changed = cur.rowcount
    finally:
        conn.close()
    if not changed:
        return None
    return get_visitor_by_participant_id(participant_id, db_path=db_path)


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


def get_visitor_by_participant_id(
    participant_id: str, *, db_path: Path | str | None = None,
) -> dict | None:
    """participant_id -> {participant_id, display_name}, or None if unknown.

    The lookup direction `resolve_visitor` doesn't cover -- that one goes
    token -> identity (authenticating a live visitor); this one goes
    display-safe id -> identity, for a caller (personalized relay-grant
    minting, auto-tp1v9) that already has a participant_id on hand and
    needs to confirm it's real before binding a grant to it. Never takes
    or returns a token -- participant_id is safe to pass around, the
    token never is.
    """
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT participant_id, display_name, avatar_attachment_id"
            " FROM visitor_tokens WHERE participant_id = ?",
            (participant_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "participant_id": row["participant_id"],
        "display_name": row["display_name"],
        "avatar_attachment_id": row["avatar_attachment_id"],
    }


# ── Mission conversation (P2 Q&A) ─────────────────────────────────


def ask_question(
    mission_id: str,
    question: str,
    participant_id: str,
    participant_label: str,
    *,
    pillar_id: str | None = None,
    anchor: str | None = None,
    anchor_title: str | None = None,
    anchor_excerpt: str | None = None,
    db_path: Path | str | None = None,
) -> dict | None:
    """Record a message -- no `kind` (question/proposal/comment): a message
    is a message, tracked along with a reply. Returns None if the target
    (the pillar, when pillar_id is given; else the mission) doesn't exist.

    mission_id is always required and always stored, even for a
    pillar-scoped entry -- denormalized on purpose so a mission-wide read
    (the decision log, in particular) never needs a join through pillars.
    pillar_id is NULL for mission-level entries -- today's only shape,
    unchanged. anchor is a free-text artifact id the pillar's own HTML
    defines (e.g. "table:oss_purl_resolution"); NULL means unanchored.

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
            "SELECT 1 FROM pillars WHERE pillar_id = ?" if pillar_id
            else "SELECT 1 FROM missions WHERE mission_id = ?",
            (pillar_id if pillar_id else mission_id,),
        ).fetchone()
        if not exists:
            return None
        conn.execute(
            "INSERT INTO mission_conversation"
            " (entry_id, mission_id, question, asked_by_participant_id,"
            "  asked_by_label, answer, answered_by_session, answered_at,"
            "  relay_status, created_at, pillar_id, anchor, anchor_title,"
            "  anchor_excerpt)"
            " VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, 'pending', ?, ?, ?, ?, ?)",
            (entry_id, mission_id, question, participant_id, participant_label,
             created_at, pillar_id, anchor, anchor_title, anchor_excerpt),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "entry_id": entry_id,
        "mission_id": mission_id,
        "pillar_id": pillar_id,
        "anchor": anchor,
        "anchor_title": anchor_title,
        "anchor_excerpt": anchor_excerpt,
        "question": question,
        "asked_by_participant_id": participant_id,
        "asked_by_label": participant_label,
        "answer": None,
        "answered_by_session": None,
        "answered_at": None,
        "relay_status": "pending",
        "created_at": created_at,
    }


def get_pillar_by_coordinator(
    coordinator_session: str, *, db_path: Path | str | None = None,
) -> dict | None:
    """The pillar a session coordinates, or None.

    coordinator_session is plain data and mutable by design -- a pillar can
    change hands -- so this is a live lookup, never cached. Newest first, so a
    session that has been handed two pillars acts as the one it most recently
    took over rather than one it has left behind.
    """
    if not coordinator_session:
        return None
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM pillars WHERE coordinator_session = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (coordinator_session,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def get_mission_by_coordinator(
    coordinator_session: str, *, db_path: Path | str | None = None,
) -> dict | None:
    """The mission a session coordinates, or None. Same contract as
    get_pillar_by_coordinator."""
    if not coordinator_session:
        return None
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM missions WHERE coordinator_session = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (coordinator_session,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


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
    """Mission-level conversation only (pillar_id IS NULL) -- unchanged
    behavior from before pillars existed. For a specific pillar's
    conversation, see list_pillar_conversation."""
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM mission_conversation WHERE mission_id = ? AND pillar_id IS NULL"
            " ORDER BY created_at ASC",
            (mission_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def list_whole_mission_conversation(
    mission_id: str, *, db_path: Path | str | None = None,
) -> list[dict]:
    """Every entry under this mission, mission-level AND pillar-scoped.

    Distinct from list_conversation, which is deliberately pillar_id IS
    NULL and keeps its pre-pillars meaning. This is the one a reader asking
    "what is still open here?" needs: open questions do not stop mattering
    because they were asked against a pillar, and hunting them pillar by
    pillar is exactly the navigation the viewer chrome removes.
    """
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


def list_pillar_conversation(
    pillar_id: str, *, db_path: Path | str | None = None,
) -> list[dict]:
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM mission_conversation WHERE pillar_id = ?"
            " ORDER BY created_at ASC",
            (pillar_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def count_open_questions(
    mission_id: str, *, db_path: Path | str | None = None,
) -> int:
    """Mission-level open questions only (pillar_id IS NULL) -- unchanged
    behavior from before pillars existed. The one activity signal worth a
    home-page badge (graph://97ace518-788 §0: cheaply derivable from a
    column already being written, not a new capability)."""
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM mission_conversation"
            " WHERE mission_id = ? AND pillar_id IS NULL AND answer IS NULL"
            " AND retired_at IS NULL",
            (mission_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


def count_open_pillar_questions(
    pillar_id: str, *, db_path: Path | str | None = None,
) -> int:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM mission_conversation"
            " WHERE pillar_id = ? AND answer IS NULL AND retired_at IS NULL",
            (pillar_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


def get_last_seen(mission_id: str, *, db_path: Path | str | None = None) -> float | None:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT last_seen_at FROM mission_last_seen WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["last_seen_at"] if row else None


def mark_seen(mission_id: str, *, db_path: Path | str | None = None) -> float:
    """Advance the mission's implicit last-seen watermark to now(). Upsert.

    Called deliberately (see the API's ``mark_mission_seen`` handler), never
    as a side effect of an incidental read -- callers that read a mission
    just to hydrate a list row must not silently consume its delta.
    """
    seen_at = time.time()
    conn = _get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO mission_last_seen (mission_id, last_seen_at) VALUES (?, ?)"
            " ON CONFLICT(mission_id) DO UPDATE SET last_seen_at = excluded.last_seen_at",
            (mission_id, seen_at),
        )
        conn.commit()
    finally:
        conn.close()
    return seen_at


def list_site_revisions_since(
    mission_id: str, since_at: float, *, db_path: Path | str | None = None,
) -> list[dict]:
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT revision_id, mission_id, revision_seq, note, created_at,"
            " LENGTH(html) AS byte_size"
            " FROM mission_site_revisions WHERE mission_id = ? AND created_at > ?"
            " ORDER BY revision_seq DESC",
            (mission_id, since_at),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def list_conversation_since(
    mission_id: str, since_at: float, *, db_path: Path | str | None = None,
) -> list[dict]:
    """Mission-level only (pillar_id IS NULL) -- matches list_conversation's
    scoping so a mission's since_last_visit never double-counts pillar
    activity that pillar's own since_last_visit already reports."""
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM mission_conversation"
            " WHERE mission_id = ? AND pillar_id IS NULL AND created_at > ?"
            " ORDER BY created_at ASC",
            (mission_id, since_at),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def list_pillar_conversation_since(
    pillar_id: str, since_at: float, *, db_path: Path | str | None = None,
) -> list[dict]:
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM mission_conversation WHERE pillar_id = ? AND created_at > ?"
            " ORDER BY created_at ASC",
            (pillar_id, since_at),
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


def rephrase_question(
    mission_id: str,
    entry_id: str,
    question: str,
    by_session: str,
    *,
    db_path: Path | str | None = None,
) -> dict | None:
    """Rewrite a question's text in place. Returns None if it doesn't exist.

    The same entry, the same id, the same answer and the same anchor -- only
    the wording changes. It is NOT a new question: a rephrase that started a
    fresh entry would strand the answer that already belongs to this one.

    Who last changed it is recorded, because a reader finding wording that
    does not match the answer beneath it needs to know whether the asker
    tightened their own question or somebody else rewrote it for them.
    """
    question = (question or "").strip()
    if not question:
        return None
    edited_at = time.time()
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE mission_conversation"
            " SET question = ?, question_edited_at = ?,"
            "     question_edited_by_session = ?"
            " WHERE mission_id = ? AND entry_id = ?",
            (question, edited_at, by_session, mission_id, entry_id),
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

    ANSWERING DOES NOT CLOSE ANYTHING. A reply is not presumed to be the end
    of the exchange: the person who asked may have more to say, and very often
    the first thing back is a question of its own. What is stored here is the
    current answer, which a later round can replace.

    A question asked by a screen stops being asked when the screen stops
    asking it -- the pillar removes it and pushes. One asked by a person ends
    when they close it (see close_question). Neither of those is this.
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


# ── Progress updates ─────────────────────────────────────────────
#
# Interim visibility on a still-open conversation entry -- "still working,
# capturing the new screenshot" -- without closing it out. Any number of
# these per entry; exactly one `answer` (or none yet) ultimately closes it.


def get_conversation_entry(
    entry_id: str, *, db_path: Path | str | None = None,
) -> dict | None:
    """One entry by its own id. get_question() requires the mission too,
    which callers holding only an entry_id (it is a primary key) do not have
    and should not have to look up first."""
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM mission_conversation WHERE entry_id = ?", (entry_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def set_question_anchor(
    entry_id: str, anchor: str | None, *, db_path: Path | str | None = None,
) -> bool:
    """Move a question to a different anchor, or to none at all.

    A screen gets restructured and a question has to be able to follow its
    subject. Without this, the only way to keep a conversation attached is to
    freeze the markup it was asked against, which makes last week's question
    an argument for keeping content nobody needs.
    """
    anchor = (anchor or "").strip() or None
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE mission_conversation SET anchor = ? WHERE entry_id = ?",
            (anchor, entry_id),
        )
        conn.commit()
    finally:
        conn.close()
    return cur.rowcount > 0


def retire_question(
    entry_id: str, note: str = "", *, db_path: Path | str | None = None,
) -> bool:
    """The subject stopped being relevant. Retires, never deletes.

    A retired entry leaves the screen and stops counting as open, and stays
    readable in the record: what was asked, and why it stopped mattering, is
    itself worth keeping. Passing an empty note un-retires it.
    """
    note = (note or "").strip()
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE mission_conversation SET retired_at = ?, retired_note = ?"
            " WHERE entry_id = ?",
            (time.time() if note else None, note or None, entry_id),
        )
        conn.commit()
    finally:
        conn.close()
    return cur.rowcount > 0


def add_conversation_message(
    entry_id: str,
    text: str,
    *,
    kind: str = "message",
    author_participant_id: str | None = None,
    author_label: str | None = None,
    db_path: Path | str | None = None,
) -> dict | None:
    """Append one round to a conversation. Returns the stored row.

    *kind* is what sort of round this is:

    ``message``  somebody said something and it is part of the exchange
    ``status``   volatile progress, posted while working
    ``echo``     an automatic receipt, written by the platform

    All three are kept and all three are shown, in the order they happened.
    The sequence is the thing a reader cannot reconstruct afterwards -- asked,
    delivered, working, replied -- and it is lost the moment any of them is
    treated as ephemeral.
    """
    text = (text or "").strip()
    if not text:
        return None
    row_id = str(uuid.uuid4())
    created_at = time.time()
    conn = _get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO mission_conversation_updates"
            " (update_id, entry_id, text, created_at, kind,"
            "  author_participant_id, author_label)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row_id, entry_id, text, created_at, kind,
             author_participant_id, author_label),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "update_id": row_id,
        "entry_id": entry_id,
        "text": text,
        "created_at": created_at,
        "kind": kind,
        "author_participant_id": author_participant_id,
        "author_label": author_label,
    }


def add_conversation_update(
    entry_id: str, text: str, *, db_path: Path | str | None = None,
) -> dict:
    """Does not verify the entry exists -- mirrors mark_question_relay_status's
    lack of existence-checking; the API layer checks via get_question first,
    same as it does before calling answer_question."""
    update_id = str(uuid.uuid4())
    created_at = time.time()
    conn = _get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO mission_conversation_updates (update_id, entry_id, text, created_at)"
            " VALUES (?, ?, ?, ?)",
            (update_id, entry_id, text, created_at),
        )
        conn.commit()
    finally:
        conn.close()
    return {"update_id": update_id, "entry_id": entry_id, "text": text, "created_at": created_at}


def list_conversation_updates(
    entry_id: str, *, db_path: Path | str | None = None,
) -> list[dict]:
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM mission_conversation_updates WHERE entry_id = ? ORDER BY created_at ASC",
            (entry_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def reopen_question(
    mission_id: str,
    entry_id: str,
    followup: str,
    participant_label: str,
    *,
    db_path: Path | str | None = None,
) -> dict | None:
    """Reopen an already-answered entry with a follow-up -- same entry_id,
    not a new row. Clears `answer`/`answered_by_session`/`answered_at` back
    to open (relay_status -> 'pending' so the API layer re-delivers) and
    folds the prior answer plus the new follow-up into the update trail as
    context for the responder's next, integrated answer.

    `question` is deliberately left untouched -- it stays the original ask;
    only the record's one `answer` slot changes as the discussion resolves.
    The prior answer and every update are ephemeral (see
    mission_conversation_updates' own docstring and the API layer's
    _question_payload, which drops `updates` once an entry is answered
    again) -- a reopened entry that gets re-answered leaves no trail of the
    intermediate back-and-forth, only the final question/answer pair. That
    is deliberate: the record is meant to read as the current, integrated
    decision, not a story of how the discussion got there.

    Returns None if the entry doesn't exist or is already open -- reopening an
    open entry is meaningless; the caller should rely on the still-open
    question, or post a plain update onto it.

    Gated on CLOSED, not on there being an answer. A question closed because
    it stopped mattering has no answer to clear, and gating on one made it the
    only kind of entry that could never be reopened -- the old model, in which
    those two were the same thing, surviving inside the new one.
    """
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM mission_conversation WHERE mission_id = ? AND entry_id = ?",
            (mission_id, entry_id),
        ).fetchone()
        # Reopenable when there is something to reopen FROM: an answered
        # entry (answering deliberately does NOT set closed_at — the
        # conversation stays open, per the API contract) OR one explicitly
        # closed. An entry that is neither answered nor closed has nothing
        # to reopen and is refused.
        if not row or (row["closed_at"] is None and row["answer"] is None):
            return None
        # Only when there was one. An entry closed because it stopped
        # mattering has no prior answer, and saying "Previous answer: None"
        # hands the responder a fact that is not true.
        if row["answer"] is not None:
            conn.execute(
                "INSERT INTO mission_conversation_updates (update_id, entry_id, text, created_at)"
                " VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), entry_id, f"Previous answer: {row['answer']}", time.time()),
            )
        conn.execute(
            "INSERT INTO mission_conversation_updates (update_id, entry_id, text, created_at)"
            " VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), entry_id, f"{participant_label} followed up: {followup}", time.time()),
        )
        conn.execute(
            "UPDATE mission_conversation"
            " SET answer = NULL, answered_by_session = NULL, answered_at = NULL,"
            "     closed_at = NULL, closed_by = NULL,"
            "     relay_status = 'pending'"
            " WHERE mission_id = ? AND entry_id = ?",
            (mission_id, entry_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM mission_conversation WHERE mission_id = ? AND entry_id = ?",
            (mission_id, entry_id),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def followup_question(
    mission_id: str,
    entry_id: str,
    followup: str,
    participant_label: str,
    *,
    db_path: Path | str | None = None,
) -> dict | None:
    """The ASKER adding to their own still-open question. Returns None if the
    entry doesn't exist or is already closed.

    This is the move that had nowhere to go. An asker with more to say about
    their own unanswered question was offered the only composer that existed
    -- the one that files an answer -- so saying more closed their question
    using their own words, with themselves recorded as having answered it.

    It is not a new entry and not a message in a thread: the record is one
    question and one answer, so more from the asker amends what is being
    asked. The follow-up is appended to `question` and stamped through the
    same columns a rephrase uses, because that is what it is -- the asker
    revising their own ask while it is still open. Relay goes back to pending
    so the coordinator is told again; the question they were sent is no
    longer the question on the record.
    """
    followup = (followup or "").strip()
    if not followup:
        return None
    now = time.time()
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM mission_conversation WHERE mission_id = ? AND entry_id = ?",
            (mission_id, entry_id),
        ).fetchone()
        if not row or row["closed_at"] is not None:
            return None
        conn.execute(
            "UPDATE mission_conversation"
            "   SET question = ?, question_edited_at = ?,"
            "       question_edited_by_session = ?, relay_status = 'pending'"
            " WHERE mission_id = ? AND entry_id = ?",
            (f"{row['question']}\n\n{followup}", now, participant_label,
             mission_id, entry_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM mission_conversation WHERE mission_id = ? AND entry_id = ?",
            (mission_id, entry_id),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def close_question(
    mission_id: str,
    entry_id: str,
    closed_by: str,
    *,
    db_path: Path | str | None = None,
) -> dict | None:
    """Close an entry without answering it. Returns None if it doesn't exist
    or is already closed.

    For the question that stopped needing an answer -- the asker worked it out,
    or it was overtaken. Inventing an answer to record that would put words in
    the record that nobody said, and leaving it open forever would keep asking
    someone for something no longer wanted.
    """
    now = time.time()
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE mission_conversation"
            "   SET closed_at = ?, closed_by = ?"
            " WHERE mission_id = ? AND entry_id = ? AND closed_at IS NULL",
            (now, closed_by, mission_id, entry_id),
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


# ── Cross-pillar decision log ────────────────────────────────────


def list_decision_log(
    mission_id: str, *, limit: int = 50, db_path: Path | str | None = None,
) -> list[dict]:
    """Every revision pushed (mission-level or by any pillar of this
    mission) plus every answered conversation entry, newest first.
    Computed on read, not a separate write path -- deliberately
    "everything that landed," not an editorial "these were the decisions"
    judgment the platform can't make on the coordinator's behalf. See the
    plan note (mission_control pillars design) for why a dedicated
    `decisions` table was left out of v1.

    pillar_id is NULL on mission-level entries -- the API layer resolves
    it to a pillar name via list_pillars, not baked in here.
    """
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT 'rev:' || revision_id AS log_id, mission_id, NULL AS pillar_id,
                   'revision' AS kind, revision_seq, note AS text, created_at
            FROM mission_site_revisions WHERE mission_id = ?
            UNION ALL
            SELECT 'prev:' || psr.revision_id AS log_id, p.mission_id, psr.pillar_id,
                   'revision' AS kind, psr.revision_seq, psr.note AS text, psr.created_at
            FROM pillar_site_revisions psr JOIN pillars p ON p.pillar_id = psr.pillar_id
            WHERE p.mission_id = ?
            UNION ALL
            SELECT 'ans:' || entry_id AS log_id, mission_id, pillar_id,
                   'answer' AS kind, NULL AS revision_seq, answer AS text, answered_at AS created_at
            FROM mission_conversation WHERE mission_id = ? AND answer IS NOT NULL
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (mission_id, mission_id, mission_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ── Idle-nag support for outstanding questions ───────────────────
#
# Deliberately separate from tmux_sessions' own nag_enabled/nag_message/
# nag_interval/nag_last_sent (dashboard_db.py) -- see coordinator_nag_state's
# schema comment for why reusing that slot would be wrong.


def get_coordinator_nag_state(
    coordinator_session: str, *, db_path: Path | str | None = None,
) -> float | None:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT last_nagged_at FROM coordinator_nag_state WHERE coordinator_session = ?",
            (coordinator_session,),
        ).fetchone()
    finally:
        conn.close()
    return row["last_nagged_at"] if row else None


def mark_coordinator_nagged(
    coordinator_session: str, *, db_path: Path | str | None = None,
) -> float:
    nagged_at = time.time()
    conn = _get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO coordinator_nag_state (coordinator_session, last_nagged_at) VALUES (?, ?)"
            " ON CONFLICT(coordinator_session) DO UPDATE SET last_nagged_at = excluded.last_nagged_at",
            (coordinator_session, nagged_at),
        )
        conn.commit()
    finally:
        conn.close()
    return nagged_at


def list_open_questions_for_session(
    coordinator_session: str, *, db_path: Path | str | None = None,
) -> list[dict]:
    """Every open (unanswered) question across every mission and pillar
    this session coordinates -- both the condition session_monitor.py's
    idle-nag branch checks before firing, and the actual nag message body."""
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT mc.* FROM mission_conversation mc
            WHERE mc.answer IS NULL AND (
                (mc.pillar_id IS NULL AND mc.mission_id IN (
                    SELECT mission_id FROM missions WHERE coordinator_session = ?
                ))
                OR mc.pillar_id IN (
                    SELECT pillar_id FROM pillars WHERE coordinator_session = ?
                )
            )
            ORDER BY mc.created_at ASC
            """,
            (coordinator_session, coordinator_session),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def list_coordinators_with_open_questions(
    *, db_path: Path | str | None = None,
) -> dict[str, list[dict]]:
    """Every coordinator_session (mission- or pillar-level) with at least
    one open question, mapped to its open entries -- ONE query for
    session_monitor.py's whole per-tick idle-nag sweep, not one query per
    live session (most sessions coordinate nothing in Mission Control at
    all; querying each of them individually every 10s would be pure
    waste)."""
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT mc.*, COALESCE(p.coordinator_session, m.coordinator_session) AS coordinator_session
            FROM mission_conversation mc
            LEFT JOIN pillars p ON mc.pillar_id = p.pillar_id
            LEFT JOIN missions m ON mc.pillar_id IS NULL AND mc.mission_id = m.mission_id
            WHERE mc.closed_at IS NULL AND mc.retired_at IS NULL
              -- STILL OWED, not merely still open. A conversation stays open
              -- after it is answered, because a reply is not presumed to be
              -- the end of it. The reminder asks whether YOU still owe a
              -- reply, which is a different question from whether the
              -- exchange is finished -- conflating the two is what would
              -- nag a coordinator forever about a conversation they have
              -- already spoken in.
              AND mc.answer IS NULL
              -- NEVER NAG SOMEONE ABOUT THEIR OWN QUESTION. Every open entry
              -- used to be one a guest asked and a coordinator owed, so
              -- mapping an entry to its coordinator was the same as mapping it
              -- to whoever owed the answer. A screen that asks the operator
              -- something breaks that: the asker IS the coordinator. It cannot
              -- bite while such a question is unanswered (there is no row
              -- until then), but reopening your own answered ask puts one back
              -- in scope, and you would be chased for a decision only the
              -- operator can make.
              AND mc.asked_by_participant_id IS NOT COALESCE(
                    'pillar:' || mc.pillar_id, 'mission:' || mc.mission_id)
            ORDER BY mc.created_at ASC
            """,
        ).fetchall()
    finally:
        conn.close()
    result: dict[str, list[dict]] = {}
    for r in rows:
        entry = dict(r)
        session = entry.pop("coordinator_session")
        if not session:
            continue
        result.setdefault(session, []).append(entry)
    return result
