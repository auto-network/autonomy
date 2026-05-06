"""Dashboard DB — SQLite-backed persistent state owned by the dashboard process.

Single source of truth for tmux session identity. Replaces in-memory dicts,
scattered meta files, and file-scanning recovery.

Database: data/dashboard.db
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH = Path(os.environ.get("DASHBOARD_DB", str(Path(__file__).parents[3] / "data" / "dashboard.db")))
_conn: sqlite3.Connection | None = None

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS tmux_sessions (
    tmux_name           TEXT PRIMARY KEY,
    session_uuid        TEXT,
    graph_source_id     TEXT,
    harness             TEXT NOT NULL DEFAULT 'claude',
    harness_state       TEXT NOT NULL DEFAULT '{}',
    type                TEXT NOT NULL,
    project             TEXT NOT NULL,
    jsonl_path          TEXT,
    bead_id             TEXT,
    created_at          REAL NOT NULL,
    is_live             INTEGER DEFAULT 1,
    file_offset         INTEGER DEFAULT 0,
    last_activity       REAL,
    last_message        TEXT DEFAULT '',
    entry_count         INTEGER DEFAULT 0,
    context_tokens      INTEGER DEFAULT 0,
    label               TEXT DEFAULT '',
    topics              TEXT DEFAULT '[]',
    role                TEXT DEFAULT '',
    harness_token       TEXT
);

CREATE TABLE IF NOT EXISTS turn_corrections (
    session_uuid       TEXT NOT NULL,
    target_message_id  TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending',
    original_sha256    TEXT NOT NULL,
    corrected_text     TEXT NOT NULL,
    mode               TEXT,
    reason             TEXT,
    confidence         REAL,
    created_at         REAL NOT NULL,
    updated_at         REAL NOT NULL,
    PRIMARY KEY (session_uuid, target_message_id)
);
"""


def _backfill_new_columns(conn: sqlite3.Connection) -> None:
    """Backfill resolution_dir, session_uuids, curr_jsonl_file for existing rows.

    Called once when the new columns are first added.
    """
    import json as _json

    rows = conn.execute(
        "SELECT tmux_name, type, bead_id, jsonl_path FROM tmux_sessions"
    ).fetchall()
    if not rows:
        return

    agent_runs = Path(__file__).resolve().parents[3] / "data" / "agent-runs"
    updated = 0
    for row in rows:
        tmux_name = row[0]
        stype = row[1]
        bead_id = row[2]
        jsonl_path = row[3]

        resolution_dir: str | None = None
        session_uuids: list[str] = []
        curr_jsonl_file: str | None = None

        if jsonl_path:
            jp = Path(jsonl_path)

            # Validate: clear subagent paths (graph://301b0811-0f1 bug)
            if "subagents" in str(jp):
                logger.warning("dashboard_db: backfill clearing subagent path for %s: %s", tmux_name, jsonl_path)
                conn.execute(
                    "UPDATE tmux_sessions SET jsonl_path=NULL, session_uuid=NULL WHERE tmux_name=?",
                    (tmux_name,),
                )
                continue

            # Derive resolution_dir from jsonl_path parent
            resolution_dir = str(jp.parent)
            session_uuids = [jp.stem]
            curr_jsonl_file = jsonl_path
        elif stype == "container" and bead_id:
            # Container without jsonl_path: derive from bead_id
            if agent_runs.exists():
                matches = sorted(
                    agent_runs.glob(f"{bead_id}-*"),
                    key=lambda p: p.stat().st_mtime, reverse=True,
                )
                for m in matches:
                    sess_dir = m / "sessions"
                    if sess_dir.exists():
                        # Find project subdirs
                        subdirs = [d for d in sess_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
                        if subdirs:
                            resolution_dir = str(subdirs[0])
                        break

        if resolution_dir or session_uuids or curr_jsonl_file:
            conn.execute(
                "UPDATE tmux_sessions SET resolution_dir=?, session_uuids=?, curr_jsonl_file=? WHERE tmux_name=?",
                (resolution_dir, _json.dumps(session_uuids), curr_jsonl_file, tmux_name),
            )
            updated += 1

    conn.commit()
    if updated:
        logger.info("dashboard_db: backfilled %d rows with resolution_dir/session_uuids/curr_jsonl_file", updated)


def init_db(db_path: Path | None = None) -> None:
    """Initialise dashboard.db and create schema. Idempotent."""
    global _conn
    path = db_path or _DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(path), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=5000")
    _conn.executescript(_SCHEMA)
    _conn.commit()
    # Migrate: add label column if missing (for existing databases)
    try:
        _conn.execute("SELECT label FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN label TEXT DEFAULT ''")
        _conn.commit()
    # Migrate: add topics column if missing (for existing databases)
    try:
        _conn.execute("SELECT topics FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN topics TEXT DEFAULT '[]'")
        _conn.commit()
    # Migrate: add nag columns if missing
    try:
        _conn.execute("SELECT nag_enabled FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN nag_enabled INTEGER DEFAULT 0")
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN nag_interval INTEGER DEFAULT 15")
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN nag_message TEXT DEFAULT ''")
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN nag_last_sent REAL DEFAULT 0")
        _conn.commit()
    # Migrate: add dispatch_nag column if missing
    try:
        _conn.execute("SELECT dispatch_nag FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN dispatch_nag INTEGER DEFAULT 0")
        _conn.commit()
    # Migrate: add role column if missing
    try:
        _conn.execute("SELECT role FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN role TEXT DEFAULT ''")
        _conn.commit()
    # Migrate: add harness routing columns if missing
    try:
        _conn.execute("SELECT harness FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN harness TEXT NOT NULL DEFAULT 'claude'")
        _conn.commit()
    try:
        _conn.execute("SELECT harness_state FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN harness_state TEXT NOT NULL DEFAULT '{}'")
        _conn.commit()
    # Migrate: add resolution_dir, session_uuids, curr_jsonl_file columns (Phase 0)
    try:
        _conn.execute("SELECT resolution_dir FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN resolution_dir TEXT")
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN session_uuids TEXT DEFAULT '[]'")
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN curr_jsonl_file TEXT")
        _conn.commit()
        _backfill_new_columns(_conn)
    # Migrate: add activity_state column if missing
    try:
        _conn.execute("SELECT activity_state FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN activity_state TEXT DEFAULT 'idle'")
        _conn.commit()
    # Migrate: add todos column if missing (Phase 2 — drawer Todos tab)
    try:
        _conn.execute("SELECT todos FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN todos TEXT DEFAULT '[]'")
        _conn.commit()
    # Migrate: add model column if missing (auto-ngis4 — session card harness/model)
    try:
        _conn.execute("SELECT model FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN model TEXT DEFAULT NULL")
        _conn.commit()
    # Migrate: harness_token column (auto-ghhdg — rename from claude_token_alias;
    # auto-08n3f — values switched from operator alias strings to Anthropic
    # org UUIDs joined to ``dashboard.claude.credentials.alias`` for display).
    # Three cases:
    #   1. fresh DB → CREATE TABLE already added harness_token, nothing to do.
    #   2. legacy DB with claude_token_alias → RENAME COLUMN.
    #   3. DB created between auto-10lsv and this rename without the legacy column
    #      (e.g. dropped + recreated) → ADD COLUMN.
    try:
        _conn.execute("SELECT harness_token FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        try:
            _conn.execute("SELECT claude_token_alias FROM tmux_sessions LIMIT 0")
        except sqlite3.OperationalError:
            _conn.execute(
                "ALTER TABLE tmux_sessions ADD COLUMN harness_token TEXT DEFAULT NULL"
            )
        else:
            _conn.execute(
                "ALTER TABLE tmux_sessions RENAME COLUMN claude_token_alias TO harness_token"
            )
        _conn.commit()
    logger.info("dashboard_db: initialised at %s", path)


def get_conn() -> sqlite3.Connection:
    """Return the module-level connection, initialising if needed."""
    if _conn is None:
        init_db()
    assert _conn is not None
    return _conn


# ── INSERT / UPDATE helpers ─────────────────────────────────────


def insert_session(
    tmux_name: str,
    session_type: str,
    project: str,
    *,
    harness: str = "claude",
    harness_state: str = "{}",
    bead_id: str | None = None,
    jsonl_path: str | None = None,
    session_uuid: str | None = None,
    resolution_dir: str | None = None,
    harness_token: str | None = None,
) -> None:
    """INSERT a new session row. Raises sqlite3.IntegrityError on duplicate name."""
    import json as _json

    conn = get_conn()
    # Build session_uuids and curr_jsonl_file from initial values
    session_uuids = _json.dumps([session_uuid]) if session_uuid else "[]"
    curr_jsonl_file = jsonl_path  # initially same as jsonl_path
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, harness, harness_state,"
        "  bead_id, jsonl_path, session_uuid,"
        "  resolution_dir, session_uuids, curr_jsonl_file, created_at, is_live,"
        "  harness_token)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
        (tmux_name, session_type, project, harness, harness_state,
         bead_id, jsonl_path, session_uuid,
         resolution_dir, session_uuids, curr_jsonl_file, time.time(),
         harness_token),
    )
    conn.commit()


def update_jsonl_link(tmux_name: str, session_uuid: str, jsonl_path: str, project: str | None = None) -> None:
    """LINK step: set session_uuid, jsonl_path, and append to session_uuids.

    Also updates curr_jsonl_file and derives resolution_dir from the file path.
    """
    import json as _json

    conn = get_conn()
    # Read current session_uuids and append new UUID if not already present
    row = conn.execute(
        "SELECT session_uuids FROM tmux_sessions WHERE tmux_name=?", (tmux_name,)
    ).fetchone()
    uuids: list[str] = _json.loads(row[0] or "[]") if row and row[0] else []
    if session_uuid not in uuids:
        uuids.append(session_uuid)

    resolution_dir = str(Path(jsonl_path).parent)

    if project:
        conn.execute(
            "UPDATE tmux_sessions SET session_uuid=?, jsonl_path=?, project=?,"
            " resolution_dir=COALESCE(resolution_dir, ?), session_uuids=?,"
            " curr_jsonl_file=? WHERE tmux_name=?",
            (session_uuid, jsonl_path, project, resolution_dir, _json.dumps(uuids),
             jsonl_path, tmux_name),
        )
    else:
        conn.execute(
            "UPDATE tmux_sessions SET session_uuid=?, jsonl_path=?,"
            " resolution_dir=COALESCE(resolution_dir, ?), session_uuids=?,"
            " curr_jsonl_file=? WHERE tmux_name=?",
            (session_uuid, jsonl_path, resolution_dir, _json.dumps(uuids),
             jsonl_path, tmux_name),
        )
    conn.commit()


def link_and_enrich(tmux_name: str, session_uuid: str, jsonl_path: str, project: str | None = None) -> None:
    """LINK + ENRICH in one shot: set session_uuid, jsonl_path, and graph_source_id.

    1. Updates dashboard.db with session_uuid and jsonl_path
    2. Runs `graph ingest-session` to ingest the JSONL and get the graph source ID
    3. Updates dashboard.db with graph_source_id

    This is the ONLY function that should be called when a JSONL is discovered
    (by the watcher or the Link Terminal handshake).
    """
    import subprocess

    # LINK: write session_uuid and jsonl_path
    update_jsonl_link(tmux_name, session_uuid, jsonl_path, project)
    logger.info("dashboard_db: LINK  %s → uuid=%s  path=%s", tmux_name, session_uuid[:12], jsonl_path)

    # ENRICH: ingest into graph and capture source ID
    try:
        result = subprocess.run(
            ["graph", "ingest-session", jsonl_path],
            capture_output=True, text=True, timeout=30,
        )
        graph_source_id = result.stdout.strip()
        if result.returncode == 0 and graph_source_id:
            set_graph_source_validated(tmux_name, graph_source_id)
            logger.info("dashboard_db: ENRICH  %s → graph=%s", tmux_name, graph_source_id[:11])
        else:
            logger.warning("dashboard_db: ENRICH failed for %s: %s", tmux_name, result.stderr.strip())
    except Exception:
        logger.warning("dashboard_db: ENRICH error for %s", tmux_name, exc_info=True)


def update_graph_source(tmux_name: str, graph_source_id: str) -> None:
    """ENRICH step: set graph_source_id after graph ingestion.

    Raw writer — does not validate that ``graph_source_id`` resolves in any
    org DB. Callers that received the ID from outside the dashboard process
    (e.g. ``graph ingest-session`` stdout) should use
    :func:`set_graph_source_validated` instead so a non-resolving ID is
    surfaced as a warning.
    """
    conn = get_conn()
    conn.execute(
        "UPDATE tmux_sessions SET graph_source_id=? WHERE tmux_name=?",
        (graph_source_id, tmux_name),
    )
    conn.commit()


# ── graph_source_id reconciler (writer-side fix for auto-4jpa8) ─────────


def _resolve_source_in_orgs_by_id(graph_source_id: str) -> bool:
    """Return True if ``graph_source_id`` resolves to a sources row in any
    org DB. Used by the registration verification + reconciler to detect
    drift (non-empty IDs that no org DB has).
    """
    if not graph_source_id:
        return False
    try:
        from tools.graph.cross_org import list_org_slugs, open_peer_db
    except Exception:
        return False
    for slug in list_org_slugs():
        peer = open_peer_db(slug)
        if peer is None:
            continue
        try:
            row = peer.get_source(graph_source_id)
        except Exception:
            row = None
        if row is not None:
            return True
    return False


def _resolve_source_id_by_path(jsonl_path: str) -> str | None:
    """Look up the canonical ``sources.id`` for ``jsonl_path`` across every
    org DB. Returns the first hit (alphabetical by org slug) or None.

    Mirrors the keying ingestion uses (``sources.file_path = <abs_path>``).
    """
    if not jsonl_path:
        return None
    try:
        from tools.graph.cross_org import list_org_slugs, open_peer_db
    except Exception:
        return None
    for slug in list_org_slugs():
        peer = open_peer_db(slug)
        if peer is None:
            continue
        try:
            row = peer.get_source_by_path(jsonl_path)
        except Exception:
            row = None
        if row and row.get("id"):
            return row["id"]
    return None


def set_graph_source_validated(tmux_name: str, graph_source_id: str) -> None:
    """Set ``graph_source_id`` and log a warning if it doesn't resolve.

    Used by every registration code path (`link_and_enrich`, the seed
    monitor ENRICH pass, etc.). Empty string is acceptable — the reconciler
    will fill it. A non-empty ID that no org DB recognises is the bug
    described in auto-4jpa8 §B; we log a warning so future drift is
    visible in the runtime log even before the reconciler corrects it.
    """
    if graph_source_id and not _resolve_source_in_orgs_by_id(graph_source_id):
        logger.warning(
            "dashboard_db: registration wrote non-resolving graph_source_id"
            " for %s → %s (will be repaired by reconciler)",
            tmux_name, graph_source_id,
        )
    update_graph_source(tmux_name, graph_source_id)


def reconcile_graph_source_ids(*, live_only: bool = True) -> int:
    """Reconcile ``tmux_sessions.graph_source_id`` against the org DBs.

    For every (live, by default) session row with a ``jsonl_path``:

    * If ``graph_source_id`` is empty/NULL, look up the real ID via
      ``sources.file_path = jsonl_path`` across all org DBs and write it
      back.
    * If ``graph_source_id`` is non-empty but doesn't resolve in any org
      DB (drift), look up the real ID by ``jsonl_path``. If found, replace
      the stale value.
    * If the real ID can't be found yet (JSONL not ingested), leave the
      row alone — the next tick will retry.

    Idempotent. Returns the number of rows repaired this pass.

    This is the writer-side fix for auto-4jpa8: the dashboard's read paths
    should always trust ``graph_source_id``, so we have to keep it
    consistent with the org DB the ingester actually wrote into.
    """
    conn = get_conn()
    if live_only:
        rows = conn.execute(
            "SELECT tmux_name, graph_source_id, jsonl_path FROM tmux_sessions"
            " WHERE is_live=1 AND jsonl_path IS NOT NULL AND jsonl_path != ''"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT tmux_name, graph_source_id, jsonl_path FROM tmux_sessions"
            " WHERE jsonl_path IS NOT NULL AND jsonl_path != ''"
        ).fetchall()
    repaired = 0
    for row in rows:
        tmux_name = row["tmux_name"]
        gid = row["graph_source_id"] or ""
        jpath = row["jsonl_path"]
        if gid and _resolve_source_in_orgs_by_id(gid):
            # Already correct — no cross-org by_path lookup needed.
            continue
        real_id = _resolve_source_id_by_path(jpath)
        if not real_id:
            # JSONL not yet ingested. Try again next tick.
            continue
        if real_id == gid:
            # Defensive: file_path resolved to the same id we already store.
            # _resolve_source_in_orgs_by_id said no, so this likely means
            # the existing row is fine but the org-scan path threw — leave
            # it. Skip the write.
            continue
        # Repair: drift or empty → real_id.
        update_graph_source(tmux_name, real_id)
        logger.info(
            "dashboard_db: reconcile %s graph_source_id %s → %s",
            tmux_name, gid[:11] if gid else "(empty)", real_id[:11],
        )
        repaired += 1
    return repaired


def reconcile_session_graph_source_id(session: dict | None) -> str:
    """Return a *verified-resolving* ``graph_source_id`` for ``session``.

    Defense-in-depth read-side complement to :func:`reconcile_graph_source_ids`.
    Surfaces that hand a session's ``graph_source_id`` to a caller (the
    ``/api/session/{tmux_name}`` endpoint, the CrossTalk envelope) call this
    just-in-time so a drifted or empty stored value does not leak out.

    Resolution order:

    1. If the stored ID resolves to a row in any org DB, return it.
    2. Otherwise, look up the canonical ID via ``sources.file_path =
       jsonl_path``. If found, persist the repair back to
       ``tmux_sessions`` and return the new ID.
    3. Otherwise, return ``""`` — never leak a non-resolving ID. Callers
       MUST treat the empty string as "no verified source yet" and either
       omit the ID or render a placeholder.
    """
    if not session:
        return ""
    stored = (session.get("graph_source_id") or "").strip()
    if stored and _resolve_source_in_orgs_by_id(stored):
        return stored
    jpath = (session.get("jsonl_path") or "").strip()
    if not jpath:
        return ""
    real_id = _resolve_source_id_by_path(jpath)
    if not real_id:
        return ""
    if real_id == stored:
        return stored
    tmux_name = session.get("tmux_name") or ""
    if tmux_name:
        try:
            update_graph_source(tmux_name, real_id)
            logger.info(
                "dashboard_db: read-side reconcile %s graph_source_id %s → %s",
                tmux_name, stored[:11] if stored else "(empty)", real_id[:11],
            )
        except Exception:
            logger.warning(
                "dashboard_db: read-side reconcile write failed for %s",
                tmux_name, exc_info=True,
            )
    return real_id


def get_source_max_turn_number(graph_source_id: str) -> int | None:
    """Return ``MAX(turn_number)`` over thoughts ∪ derivations for ``graph_source_id``.

    Used by CrossTalk to write the graph turn number into the envelope's
    ``turn=`` attribute (per auto-4nr14 §B). Distinct from
    ``tmux_sessions.entry_count``, which is the JSONL/viewer-tail line
    count and is ~10–20× larger because graph ingest filters tool-use /
    tool-result messages and only counts operator-visible text turns
    (plus compact summaries for Claude).

    Returns ``None`` when the source is unknown to every org DB or has no
    thoughts/derivations yet — callers should omit ``turn=`` rather than
    fall back to a different counter on a different scale.
    """
    if not graph_source_id:
        return None
    try:
        from tools.graph.cross_org import list_org_slugs, open_peer_db
    except Exception:
        return None
    for slug in list_org_slugs():
        peer = open_peer_db(slug)
        if peer is None:
            continue
        try:
            row = peer.get_source(graph_source_id)
        except Exception:
            row = None
        if row is None:
            continue
        try:
            max_row = peer.conn.execute(
                """SELECT MAX(turn_number) AS m FROM (
                       SELECT turn_number FROM thoughts WHERE source_id = ?
                       UNION ALL
                       SELECT turn_number FROM derivations WHERE source_id = ?
                   )""",
                (graph_source_id, graph_source_id),
            ).fetchone()
        except Exception:
            return None
        if max_row is None:
            return None
        m = max_row["m"] if hasattr(max_row, "keys") else max_row[0]
        return int(m) if m is not None else None
    return None


def update_tail_state(
    tmux_name: str,
    *,
    file_offset: int | None = None,
    last_activity: float | None = None,
    last_message: str | None = None,
    entry_count: int | None = None,
    context_tokens: int | None = None,
    model: str | None = None,
    harness_state: str | None = None,
) -> None:
    """TAIL step: update read position and latest content."""
    conn = get_conn()
    parts = []
    vals: list[Any] = []
    if file_offset is not None:
        parts.append("file_offset=?")
        vals.append(file_offset)
    if last_activity is not None:
        parts.append("last_activity=?")
        vals.append(last_activity)
    if last_message is not None:
        parts.append("last_message=?")
        vals.append(last_message)
    if entry_count is not None:
        parts.append("entry_count=?")
        vals.append(entry_count)
    if context_tokens is not None:
        parts.append("context_tokens=?")
        vals.append(context_tokens)
    if model is not None:
        parts.append("model=?")
        vals.append(model)
    if harness_state is not None:
        parts.append("harness_state=?")
        vals.append(harness_state)
    if not parts:
        return
    vals.append(tmux_name)
    conn.execute(f"UPDATE tmux_sessions SET {', '.join(parts)} WHERE tmux_name=?", vals)
    conn.commit()


def update_label(tmux_name: str, label: str) -> None:
    """Set or clear the user-facing label for a session."""
    conn = get_conn()
    conn.execute("UPDATE tmux_sessions SET label=? WHERE tmux_name=?", (label, tmux_name))
    conn.commit()


def update_topics(tmux_name: str, topics: list[str]) -> None:
    """Set the sub-topic status lines for a session (1-4 items, max 80 chars each)."""
    import json
    conn = get_conn()
    conn.execute("UPDATE tmux_sessions SET topics=? WHERE tmux_name=?",
                 (json.dumps(topics), tmux_name))
    conn.commit()


def update_todos(tmux_name: str, todos: list[dict]) -> None:
    """Persist the current todo-list snapshot for a session.

    ``todos`` is a list of dicts produced by ``TaskStateTracker.snapshot()`` —
    each dict has keys: task_id, subject, status, description, activeForm.
    Writes replace any prior snapshot (no append). An empty list persists as
    ``"[]"``, NOT NULL, so consumers can unconditionally json.loads.
    """
    import json
    conn = get_conn()
    conn.execute("UPDATE tmux_sessions SET todos=? WHERE tmux_name=?",
                 (json.dumps(todos), tmux_name))
    conn.commit()


def update_role(tmux_name: str, role: str) -> None:
    """Set or clear the explicit role for a session."""
    conn = get_conn()
    conn.execute("UPDATE tmux_sessions SET role=? WHERE tmux_name=?", (role, tmux_name))
    conn.commit()


def get_nag_config(tmux_name: str) -> dict | None:
    """Return nag configuration for a session."""
    conn = get_conn()
    row = conn.execute(
        "SELECT nag_enabled, nag_interval, nag_message, nag_last_sent"
        " FROM tmux_sessions WHERE tmux_name=?",
        (tmux_name,),
    ).fetchone()
    if not row:
        return None
    return {
        "enabled": bool(row["nag_enabled"]),
        "interval": row["nag_interval"] or 15,
        "message": row["nag_message"] or "",
        "last_sent": row["nag_last_sent"] or 0,
    }


def update_nag_config(
    tmux_name: str,
    *,
    enabled: bool | None = None,
    interval: int | None = None,
    message: str | None = None,
) -> None:
    """Update nag configuration for a session."""
    conn = get_conn()
    parts, vals = [], []
    if enabled is not None:
        parts.append("nag_enabled=?")
        vals.append(1 if enabled else 0)
    if interval is not None:
        parts.append("nag_interval=?")
        vals.append(interval)
    if message is not None:
        parts.append("nag_message=?")
        vals.append(message)
    if not parts:
        return
    vals.append(tmux_name)
    conn.execute(f"UPDATE tmux_sessions SET {', '.join(parts)} WHERE tmux_name=?", vals)
    conn.commit()


def update_nag_last_sent(tmux_name: str, ts: float) -> None:
    """Record when the last nag was sent."""
    conn = get_conn()
    conn.execute("UPDATE tmux_sessions SET nag_last_sent=? WHERE tmux_name=?", (ts, tmux_name))
    conn.commit()


def update_dispatch_nag(tmux_name: str, enabled: bool) -> None:
    """Enable or disable dispatch completion nag for a session."""
    conn = get_conn()
    conn.execute("UPDATE tmux_sessions SET dispatch_nag=? WHERE tmux_name=?",
                 (1 if enabled else 0, tmux_name))
    conn.commit()


def get_dispatch_nag_sessions() -> list[str]:
    """Return tmux_names of live sessions with dispatch_nag enabled."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT tmux_name FROM tmux_sessions WHERE dispatch_nag=1 AND is_live=1"
    ).fetchall()
    return [r["tmux_name"] for r in rows]


def mark_dead(tmux_name: str) -> None:
    """CLOSE step: mark session as no longer live.

    Logs at INFO so every deactivation is visible — silent mark_dead calls
    from background sweeps (e.g. /api/terminals when tmux list is transiently
    empty) previously mass-flipped sessions with no log trace. Callers are
    identified via the standard logging stack (filename + lineno) in the log
    formatter.
    """
    import inspect
    caller = inspect.stack()[1]
    caller_loc = f"{Path(caller.filename).name}:{caller.lineno}"
    conn = get_conn()
    cursor = conn.execute(
        "UPDATE tmux_sessions SET is_live=0, activity_state='dead' WHERE tmux_name=?",
        (tmux_name,),
    )
    conn.commit()
    logger.info(
        "dashboard_db: mark_dead  tmux=%s  updated_rows=%d  caller=%s",
        tmux_name, cursor.rowcount, caller_loc,
    )


def update_activity_state(tmux_name: str, state: str) -> None:
    """Update the activity_state for a session."""
    conn = get_conn()
    conn.execute(
        "UPDATE tmux_sessions SET activity_state=? WHERE tmux_name=?",
        (state, tmux_name),
    )
    conn.commit()


def delete_session(tmux_name: str) -> None:
    """Hard-delete a session row (used for cleanup of expired dead sessions)."""
    conn = get_conn()
    conn.execute("DELETE FROM tmux_sessions WHERE tmux_name=?", (tmux_name,))
    conn.commit()


# ── Queries ─────────────────────────────────────────────────────


def get_live_sessions() -> list[dict]:
    """Return all sessions with is_live=1."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM tmux_sessions WHERE is_live=1").fetchall()
    return [dict(r) for r in rows]


def get_all_sessions() -> list[dict]:
    """Return all sessions (live and dead)."""
    conn = get_conn()
    rows = conn.execute("SELECT * FROM tmux_sessions ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_session_status_rows(*, live_only: bool, since_cutoff: float | None = None) -> list[dict]:
    """Return rows for ``graph sessions --status`` directly from SQL."""
    conn = get_conn()
    if since_cutoff is None:
        if live_only:
            rows = conn.execute(
                "SELECT * FROM tmux_sessions"
                " WHERE is_live=1 ORDER BY COALESCE(last_activity, created_at) DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tmux_sessions ORDER BY COALESCE(last_activity, created_at) DESC"
            ).fetchall()
    else:
        if live_only:
            rows = conn.execute(
                "SELECT * FROM tmux_sessions"
                " WHERE is_live=1 AND COALESCE(last_activity, created_at) > ?"
                " ORDER BY COALESCE(last_activity, created_at) DESC",
                (since_cutoff,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tmux_sessions"
                " WHERE COALESCE(last_activity, created_at) > ?"
                " ORDER BY COALESCE(last_activity, created_at) DESC",
                (since_cutoff,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_session(tmux_name: str) -> dict | None:
    """Return a single session by tmux_name, or None."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM tmux_sessions WHERE tmux_name=?", (tmux_name,)).fetchone()
    return dict(row) if row else None


def is_session_live(tmux_name: str) -> bool:
    """Return True iff a row exists for ``tmux_name`` and is_live=1.

    Used by the targeted dashboard-approval nag (auto-rh2r5) to decide
    whether the authoring session is still alive to receive the message.
    Missing rows and dead rows both return False.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT is_live FROM tmux_sessions WHERE tmux_name=?", (tmux_name,)
    ).fetchone()
    return bool(row and row["is_live"])


def get_tailable_sessions() -> list[dict]:
    """Return live sessions that have a jsonl_path set (ready for tailing)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tmux_sessions WHERE is_live=1 AND jsonl_path IS NOT NULL"
    ).fetchall()
    return [dict(r) for r in rows]


def get_sessions_needing_resolution() -> list[dict]:
    """Return live sessions with no jsonl_path yet (need directory resolution or watcher link)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tmux_sessions WHERE is_live=1 AND jsonl_path IS NULL"
    ).fetchall()
    return [dict(r) for r in rows]


def session_exists(tmux_name: str) -> bool:
    """Check if a session with this name exists in the DB."""
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM tmux_sessions WHERE tmux_name=?", (tmux_name,)).fetchone()
    return row is not None


def count_live() -> int:
    """Count live sessions."""
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) FROM tmux_sessions WHERE is_live=1").fetchone()
    return row[0] if row else 0


def find_dead_session(session_uuid: str | None = None, file_path: str | None = None) -> dict | None:
    """Find a dead (is_live=0) session by session_uuid or jsonl_path.

    Checks session_uuid first, then falls back to jsonl_path.
    Returns the full row as a dict, or None.
    """
    conn = get_conn()
    if session_uuid:
        row = conn.execute(
            "SELECT * FROM tmux_sessions WHERE session_uuid=? AND is_live=0"
            " ORDER BY created_at DESC LIMIT 1",
            (session_uuid,),
        ).fetchone()
        if row:
            return dict(row)
    if file_path:
        row = conn.execute(
            "SELECT * FROM tmux_sessions WHERE jsonl_path=? AND is_live=0"
            " ORDER BY created_at DESC LIMIT 1",
            (file_path,),
        ).fetchone()
        if row:
            return dict(row)
    return None


def find_live_session(session_uuid: str | None = None, file_path: str | None = None) -> dict | None:
    """Find a live (is_live=1) session by session_uuid or jsonl_path.

    Checks session_uuid first, then falls back to jsonl_path.
    Returns the full row as a dict, or None.
    """
    conn = get_conn()
    if session_uuid:
        row = conn.execute(
            "SELECT * FROM tmux_sessions WHERE session_uuid=? AND is_live=1"
            " ORDER BY created_at DESC LIMIT 1",
            (session_uuid,),
        ).fetchone()
        if row:
            return dict(row)
    if file_path:
        row = conn.execute(
            "SELECT * FROM tmux_sessions WHERE jsonl_path=? AND is_live=1"
            " ORDER BY created_at DESC LIMIT 1",
            (file_path,),
        ).fetchone()
        if row:
            return dict(row)
    return None


def revive_session(tmux_name: str, *, file_offset: int = 0) -> None:
    """Re-activate a dead session: set is_live=1, reset file_offset, and
    clear the 'dead' activity_state flag so the row isn't contradictory
    (alive but flagged dead). Leaves non-dead activity states alone."""
    conn = get_conn()
    conn.execute(
        "UPDATE tmux_sessions SET"
        "  is_live=1,"
        "  file_offset=?,"
        "  activity_state=CASE WHEN activity_state='dead' THEN 'idle' ELSE activity_state END"
        " WHERE tmux_name=?",
        (file_offset, tmux_name),
    )
    conn.commit()


def upsert_session(
    tmux_name: str,
    session_type: str,
    project: str,
    *,
    harness: str = "claude",
    harness_state: str = "{}",
    bead_id: str | None = None,
    jsonl_path: str | None = None,
    session_uuid: str | None = None,
    resolution_dir: str | None = None,
    session_uuids: str = "[]",
    curr_jsonl_file: str | None = None,
    created_at: float | None = None,
    file_offset: int = 0,
    last_message: str = "",
    is_live: bool = True,
    label: str = "",
    harness_token: str | None = None,
) -> None:
    """INSERT ... ON CONFLICT — used for seeding on first run.

    Preserves existing label, topics, role, and nag settings on re-registration.
    Only file resolution fields and liveness are updated on conflict.
    """
    conn = get_conn()
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, harness, harness_state,"
        "  bead_id, jsonl_path, session_uuid,"
        "  resolution_dir, session_uuids, curr_jsonl_file,"
        "  created_at, is_live, file_offset, last_message, label,"
        "  harness_token)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(tmux_name) DO UPDATE SET"
        "  harness = excluded.harness,"
        "  harness_state = excluded.harness_state,"
        "  jsonl_path = excluded.jsonl_path,"
        "  session_uuid = excluded.session_uuid,"
        "  resolution_dir = COALESCE(excluded.resolution_dir, resolution_dir),"
        "  session_uuids = CASE WHEN excluded.session_uuids != '[]'"
        "    THEN excluded.session_uuids ELSE session_uuids END,"
        "  curr_jsonl_file = COALESCE(excluded.curr_jsonl_file, curr_jsonl_file),"
        "  file_offset = excluded.file_offset,"
        "  last_message = CASE WHEN excluded.last_message != ''"
        "    THEN excluded.last_message ELSE last_message END,"
        "  is_live = excluded.is_live,"
        "  harness_token = COALESCE(excluded.harness_token, harness_token),"
        # When a previously-dead row is revived via seed, clear the stale
        # 'dead' activity_state so it doesn't contradict is_live=1. Non-dead
        # states (idle / thinking / tool_running) are preserved.
        "  activity_state = CASE"
        "    WHEN excluded.is_live=1 AND activity_state='dead' THEN 'idle'"
        "    ELSE activity_state END",
        (
            tmux_name, session_type, project, harness, harness_state,
            bead_id, jsonl_path, session_uuid,
            resolution_dir, session_uuids, curr_jsonl_file,
            created_at or time.time(), 1 if is_live else 0, file_offset, last_message,
            label,
            harness_token,
        ),
    )
    conn.commit()


# ── Turn Corrections ─────────────────────────────────────────────
#
# Sparse persisted overlay rows for the session-viewer turn-correction
# feature (auto-edec1.2). One row per accepted/dismissed/pending suggestion,
# keyed by (session_uuid, target_message_id) so the JSONL transcript stays
# immutable. Identity validation uses original_sha256 to reject stale or
# mismatched targets even if the same target_message_id is reused.

_VALID_CORRECTION_STATUSES = ("pending", "accepted", "dismissed")


def upsert_turn_correction(
    session_uuid: str,
    target_message_id: str,
    *,
    original_sha256: str,
    corrected_text: str,
    mode: str | None = None,
    reason: str | None = None,
    confidence: float | None = None,
) -> dict:
    """Insert or refresh a pending turn-correction row.

    Pending rows are upsertable: a new suggestion for the same
    (session_uuid, target_message_id) replaces the prior pending one. Once a
    row reaches a terminal status (accepted/dismissed) it is preserved and
    NOT overwritten by a fresh pending suggestion — callers must dismiss the
    terminal row first if they want to re-suggest. Returns the row as stored.
    """
    if not session_uuid or not target_message_id:
        raise ValueError("session_uuid and target_message_id are required")
    if not original_sha256 or not isinstance(original_sha256, str):
        raise ValueError("original_sha256 is required")
    if corrected_text is None:
        raise ValueError("corrected_text is required")

    conn = get_conn()
    now = time.time()
    existing = conn.execute(
        "SELECT * FROM turn_corrections WHERE session_uuid=? AND target_message_id=?",
        (session_uuid, target_message_id),
    ).fetchone()
    if existing is not None and existing["status"] in ("accepted", "dismissed"):
        # Terminal — leave alone.
        return dict(existing)

    conn.execute(
        "INSERT INTO turn_corrections"
        " (session_uuid, target_message_id, status, original_sha256, corrected_text,"
        "  mode, reason, confidence, created_at, updated_at)"
        " VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(session_uuid, target_message_id) DO UPDATE SET"
        "  original_sha256=excluded.original_sha256,"
        "  corrected_text=excluded.corrected_text,"
        "  mode=excluded.mode,"
        "  reason=excluded.reason,"
        "  confidence=excluded.confidence,"
        "  updated_at=excluded.updated_at"
        " WHERE turn_corrections.status='pending'",
        (session_uuid, target_message_id, original_sha256, corrected_text,
         mode, reason, confidence, now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM turn_corrections WHERE session_uuid=? AND target_message_id=?",
        (session_uuid, target_message_id),
    ).fetchone()
    return dict(row) if row else {}


def get_turn_correction(session_uuid: str, target_message_id: str) -> dict | None:
    """Return a single correction row, or None."""
    if not session_uuid or not target_message_id:
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM turn_corrections WHERE session_uuid=? AND target_message_id=?",
        (session_uuid, target_message_id),
    ).fetchone()
    return dict(row) if row else None


def list_turn_corrections(session_uuid: str) -> list[dict]:
    """Return all corrections for a session, oldest first."""
    if not session_uuid:
        return []
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM turn_corrections WHERE session_uuid=? ORDER BY created_at ASC",
        (session_uuid,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_turn_correction_status(
    session_uuid: str,
    target_message_id: str,
    status: str,
    *,
    expected_sha256: str,
) -> tuple[str, dict | None]:
    """Transition a pending correction to a terminal state.

    Returns ``(outcome, row)``:

    * ``("ok", row)`` — transition applied; ``row`` is the updated record.
    * ``("not_found", None)`` — no row matches the (session, message) pair.
    * ``("sha_mismatch", row)`` — the supplied ``expected_sha256`` does not
      match the stored ``original_sha256``. Row left unchanged.
    * ``("already_terminal", row)`` — row is already accepted/dismissed.
      Row left unchanged.

    Both terminal outcomes return the existing row so callers can render an
    accurate "stale or mismatched" message without a second lookup.
    """
    if status not in ("accepted", "dismissed"):
        raise ValueError(f"invalid terminal status: {status}")
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM turn_corrections WHERE session_uuid=? AND target_message_id=?",
        (session_uuid, target_message_id),
    ).fetchone()
    if row is None:
        return ("not_found", None)
    existing = dict(row)
    if existing["original_sha256"] != expected_sha256:
        return ("sha_mismatch", existing)
    if existing["status"] != "pending":
        return ("already_terminal", existing)
    now = time.time()
    cursor = conn.execute(
        "UPDATE turn_corrections SET status=?, updated_at=?"
        " WHERE session_uuid=? AND target_message_id=? AND status='pending'",
        (status, now, session_uuid, target_message_id),
    )
    conn.commit()
    if (cursor.rowcount or 0) == 0:
        # Another caller won the race after our initial SELECT but before our
        # UPDATE. Re-read and report the now-terminal row instead of claiming
        # success for a transition that never applied.
        raced = conn.execute(
            "SELECT * FROM turn_corrections WHERE session_uuid=? AND target_message_id=?",
            (session_uuid, target_message_id),
        ).fetchone()
        return ("already_terminal", dict(raced) if raced else existing)
    refreshed = conn.execute(
        "SELECT * FROM turn_corrections WHERE session_uuid=? AND target_message_id=?",
        (session_uuid, target_message_id),
    ).fetchone()
    return ("ok", dict(refreshed) if refreshed else None)


def delete_turn_corrections_for_session(session_uuid: str) -> int:
    """Drop every correction row for a session. Returns rows deleted.

    Used by tests and dashboard-mock fixture rotation. Production code paths
    do not delete correction rows — terminal rows are intentionally durable.
    """
    if not session_uuid:
        return 0
    conn = get_conn()
    cursor = conn.execute(
        "DELETE FROM turn_corrections WHERE session_uuid=?", (session_uuid,)
    )
    conn.commit()
    return cursor.rowcount or 0
