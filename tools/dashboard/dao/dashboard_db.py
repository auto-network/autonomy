"""Dashboard DB — SQLite-backed persistent state owned by the dashboard process.

Single source of truth for tmux session identity. Replaces in-memory dicts,
scattered meta files, and file-scanning recovery.

Database: data/dashboard.db
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from tools.data_paths import resolve_store
from typing import Any

logger = logging.getLogger(__name__)

_DB_PATH = resolve_store("dashboard")
_conn: sqlite3.Connection | None = None
# Thread that created ``_conn`` (init_db). A sqlite3 connection must not be
# driven by two threads concurrently — check_same_thread=False only disables
# the safety check, it doesn't make the handle thread-safe, and concurrent
# use surfaces as sporadic "InterfaceError: bad parameter or other API
# misuse". Threads other than the owner get their own handle via
# ``_thread_local`` in get_conn().
_conn_owner: int | None = None
_active_path: Path | None = None
_thread_local = threading.local()

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS tmux_sessions (
    tmux_name           TEXT PRIMARY KEY,
    session_uuid        TEXT,
    graph_source_id     TEXT,
    harness             TEXT NOT NULL DEFAULT 'claude',
    -- harness_state: VOLATILE. Describes the CURRENT harness process only.
    -- Nothing that must outlive a relaunch may be stored here.
    --
    -- Written by three paths with three different semantics:
    --   _persist_tail_window  -> json_patch(...)      MERGE, per log window
    --   _screen_poll_loop     -> harness_state=?      FULL REPLACE, every 2s
    --                            from launch until composer_ready, carrying
    --                            only the four keys read_screen_state
    --                            returns; every other key is erased
    --   revive_session        -> harness_state='{}'   WIPED, on every
    --                            resume/relaunch, by design: the contents
    --                            describe the PREVIOUS process's screen, and
    --                            a stale composer_ready=true would let the
    --                            input gate fire before the new harness
    --                            accepts typing
    --
    -- Contents today: composer_ready / confirming_trust_prompt /
    -- in_planning_mode / blocking_modal (from the pane), plus the rate-limit
    -- block (from token_count records in the log). Both are re-derived after
    -- a wipe -- the pane keys within one poll interval, the rate-limit block
    -- only when the next token_count record arrives, so the 5H/7D figures on
    -- a session card have no source between a resume and the next model call.
    --
    -- Durable per-session facts belong in their own column, as harness_token
    -- does. Storing one here looks like it works: the two
    -- json_patch writers preserve it, so it survives until the next resume.
    harness_state       TEXT NOT NULL DEFAULT '{}',
    type                TEXT NOT NULL,
    project             TEXT NOT NULL,
    jsonl_path          TEXT,
    bead_id             TEXT,
    created_at          REAL NOT NULL,
    file_offset         INTEGER DEFAULT 0,
    last_activity       REAL,
    -- Durable timestamp of the latest direct operator send accepted by the
    -- dashboard composer. Unlike last_activity, assistant/tool traffic never
    -- advances this value.
    last_input_at       REAL,
    last_message        TEXT DEFAULT '',
    entry_count         INTEGER DEFAULT 0,
    context_tokens      INTEGER DEFAULT 0,
    label               TEXT DEFAULT '',
    topics              TEXT DEFAULT '[]',
    role                TEXT DEFAULT '',
    harness_token       TEXT,
    -- Unified single-column startup FSM (graph://92ed929a-3ec). NULL = "not in
    -- launching" — default for old rows and the terminal running state.
    -- Written ONLY by the lifecycle worker's writer and
    -- arm_startup_state (enforced by test_no_racing_writers.py).
    startup_state       TEXT,
    -- THE single lifecycle truth (FSM consolidation): closed set
    -- LAUNCHING | ACTIVE | STOPPING | ENDED | FAILED, CHECK-enforced so an
    -- out-of-domain value is unrepresentable, not merely unreviewed.
    -- Written only by the transition authority. is_live/activity_state
    -- above are write-through projections of it during the migration
    -- window. NULL = pre-backfill row only.
    state               TEXT CHECK (state IN
                            ('LAUNCHING','ACTIVE','STOPPING','ENDED','FAILED')),
    -- Presence telemetry: tracker-owned, non-authoritative, meaningful
    -- only while state=ACTIVE, NULL otherwise. Domain is the tracker
    -- vocabulary (_apply_activity_entries), CHECK-enforced. NOT lifecycle.
    attention           TEXT CHECK (attention IN
                            ('tool_running','thinking','idle')),
    -- Stamped when state enters ENDED/FAILED; the worktree GC tombstone
    -- anchor. Cleared on re-entry into LAUNCHING/ACTIVE.
    ended_at            REAL
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

-- NOTE (auto-hmow2): the former ``turn_correction_attempts`` table cached the
-- session-monitor's difflib resolution so history warm-ups could skip it.
-- Turn-correction delivery is now a synchronous authenticated API call
-- (POST /api/session/turn-corrections/suggest) that resolves the target once,
-- inline, so there is no monitor warm-up and nothing to cache. The table is no
-- longer created for fresh databases. Old installations keep their inert table;
-- we deliberately do not add a destructive migration just to drop it.

-- W4 (auto-gah4g): manifest-driven catch-up sweep. One row per session
-- JSONL the sweep has ever seen. A steady-state sweep is scandir + stat
-- against this table only — no GraphDB open at all unless (size, mtime)
-- disagrees with what's stored here. 'sealed' rows (dead + unchanged)
-- are skipped by routine sweeps entirely; --force or the weekly deep
-- integrity pass are the only things that re-touch them.
CREATE TABLE IF NOT EXISTS ingest_manifest (
    file_path      TEXT PRIMARY KEY,
    size           INTEGER NOT NULL DEFAULT 0,
    mtime          REAL NOT NULL DEFAULT 0,
    inode          INTEGER,
    org            TEXT,
    source_id      TEXT,
    ingest_offset  INTEGER NOT NULL DEFAULT 0,
    state          TEXT NOT NULL DEFAULT 'active',
    last_seen_at   REAL NOT NULL DEFAULT 0
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

    agent_runs = resolve_store("agent_runs")  # was repo-relative
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


def _backfill_last_input_at(conn: sqlite3.Connection) -> None:
    """Best-effort legacy seed from the transcript tracker.

    Older rows have no dedicated input timestamp.  The harness tracker may
    still hold its most recently observed user-message timestamp; copy that
    once when the durable column is introduced.  New writes use the narrower
    dashboard-send boundary and no longer depend on volatile harness state.
    """
    rows = conn.execute(
        "SELECT tmux_name, harness_state FROM tmux_sessions"
    ).fetchall()
    updated = 0
    for row in rows:
        try:
            state = json.loads(row[1] or "{}")
            raw = state.get("last_user_message_at") if isinstance(state, dict) else None
            if not isinstance(raw, str) or not raw:
                continue
            timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        conn.execute(
            "UPDATE tmux_sessions SET last_input_at=? WHERE tmux_name=?",
            (timestamp, row[0]),
        )
        updated += 1
    conn.commit()
    if updated:
        logger.info("dashboard_db: backfilled last_input_at for %d rows", updated)


def init_db(db_path: Path | None = None) -> None:
    """Initialise dashboard.db and create schema. Idempotent."""
    global _conn, _conn_owner, _active_path
    path = db_path or _DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(path), check_same_thread=False)
    _conn_owner = threading.get_ident()
    _active_path = path
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
    # Migrate: durable direct-input timestamp. This is deliberately separate
    # from volatile harness_state and from last_activity (which includes
    # assistant output and tool traffic).
    try:
        _conn.execute("SELECT last_input_at FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN last_input_at REAL")
        _conn.commit()
        _backfill_last_input_at(_conn)
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
    # Migrate: add startup_state column (unified single-column startup FSM).
    # Replaces setup_phase + harness_phase. NULL is the meaningful default:
    # existing rows stay NULL and the new feature does not govern them.
    try:
        _conn.execute("SELECT startup_state FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN startup_state TEXT")
        _conn.commit()
    # Migrate: lifecycle_detail column (session lifecycle FSM redesign 2026-06-18).
    # Nullable JSON holding the structured failure/teardown record the other
    # columns can't carry: {failed_phase, reason, retryable, attempt,
    # last_progress_at}. NULL on the happy path. The lifecycle worker is the
    # only writer; the API reads it O(1) to render "Failed at <phase>: <reason>"
    # + retry. Coarse lifecycle state stays DERIVED from is_live/startup_state/
    # activity_state — no extra column needed.
    try:
        _conn.execute("SELECT lifecycle_detail FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN lifecycle_detail TEXT")
        _conn.commit()
    # Migrate: the single lifecycle truth (FSM consolidation,
    # graph://92ed929a-3ec). ``state`` is a closed set —
    # LAUNCHING | ACTIVE | STOPPING | ENDED | FAILED — written ONLY by the
    # transition authority (SessionLifecycleStateWriter.transition;
    # enforced by test_no_racing_writers.py). is_live / activity_state
    # become write-through projections stamped in the same UPDATE during
    # the migration window and are dropped afterwards.
    # ``attention`` is presence telemetry (working|idle), tracker-owned,
    # non-authoritative, meaningful only while state=ACTIVE.
    # ``ended_at`` stamps terminal entry; it is the worktree GC's
    # tombstone anchor.
    try:
        _conn.execute("SELECT state FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute(
            "ALTER TABLE tmux_sessions ADD COLUMN state TEXT CHECK (state IN"
            " ('LAUNCHING','ACTIVE','STOPPING','ENDED','FAILED'))"
        )
        _conn.execute(
            "ALTER TABLE tmux_sessions ADD COLUMN attention TEXT CHECK"
            " (attention IN ('tool_running','thinking','idle'))"
        )
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN ended_at REAL")
        # One-shot backfill — the same decision table as
        # derive_lifecycle_state's legacy fallback, in SQL. FAILED must win
        # over ENDED (failed rows also carry is_live=0). The legacy columns
        # are read via introspection: a table old enough to predate one of
        # them backfills with that column's absent-value semantics
        # (activity/startup absent → NULL, is_live absent → 0 = dead).
        _cols = {
            r[1] for r in _conn.execute("PRAGMA table_info(tmux_sessions)")
        }
        _act = "activity_state" if "activity_state" in _cols else "NULL"
        _live = "is_live" if "is_live" in _cols else "0"
        _sst = "startup_state" if "startup_state" in _cols else "NULL"
        _conn.execute(
            "UPDATE tmux_sessions SET state = CASE"
            f"  WHEN {_act}='failed' OR {_sst}='setup_failed'"
            "    THEN 'FAILED'"
            f"  WHEN {_act} IN ('stopping','cleaning') THEN 'STOPPING'"
            f"  WHEN {_act}='dead' OR {_live}=0 OR {_live} IS NULL"
            "    THEN 'ENDED'"
            f"  WHEN {_sst} IS NOT NULL THEN 'LAUNCHING'"
            "  ELSE 'ACTIVE'"
            " END"
        )
        _conn.execute(
            "UPDATE tmux_sessions SET"
            "  attention = CASE WHEN state='ACTIVE' THEN"
            f"    (CASE WHEN {_act} IN ('tool_running','thinking','idle')"
            f"      THEN {_act} ELSE 'idle' END)"
            "  END,"
            "  ended_at = CASE WHEN state IN ('ENDED','FAILED')"
            "    THEN COALESCE(last_activity, created_at) END"
        )
        _conn.commit()
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tmux_sessions_state"
        " ON tmux_sessions(state)"
    )
    _conn.commit()
    # Drop the superseded lifecycle columns (FSM consolidation, Phase D).
    # Runs AFTER the state backfill above consumed them. Requires SQLite
    # ≥3.35 (ALTER DROP COLUMN); per-column and idempotent.
    for _legacy_col in ("is_live", "activity_state", "setup_phase", "harness_phase"):
        try:
            _conn.execute("SELECT %s FROM tmux_sessions LIMIT 0" % _legacy_col)
        except sqlite3.OperationalError:
            continue
        try:
            _conn.execute("ALTER TABLE tmux_sessions DROP COLUMN %s" % _legacy_col)
            _conn.commit()
        except sqlite3.OperationalError:
            logger.warning("dashboard_db: could not drop legacy column %s", _legacy_col)
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
    # Migrate: disk footprint columns (session resource metrics, Phase 1).
    # Written by resource_monitor on each disk sample and once more on death,
    # so ended sessions keep a footprint without any further polling.
    try:
        _conn.execute("SELECT disk_bytes FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN disk_bytes INTEGER")
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN disk_detail TEXT")
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN disk_sampled_at REAL")
        _conn.commit()
    # Migrate: rollout generation identity (bead auto-suvcp, Rule 6).
    # ``jsonl_generation`` = "<st_dev>:<st_ino>:<link_seq>" — stamped wherever
    # jsonl_path is written; the tail-persistence CAS requires path AND
    # generation to still match so a stale drain's write is dropped rather
    # than acking offsets against a replaced file. ``link_seq`` is the
    # persisted per-session link sequence nonce that makes a re-link of the
    # same inode distinguishable from the original link.
    try:
        _conn.execute("SELECT jsonl_generation FROM tmux_sessions LIMIT 0")
    except sqlite3.OperationalError:
        _conn.execute("ALTER TABLE tmux_sessions ADD COLUMN jsonl_generation TEXT")
        _conn.execute(
            "ALTER TABLE tmux_sessions ADD COLUMN link_seq INTEGER NOT NULL DEFAULT 0"
        )
        _conn.commit()
    logger.info("dashboard_db: initialised at %s", path)


def reset_conn() -> None:
    """Invalidate the module-level connection so the next get_conn() rebuilds it.

    2026-07-02 incident: the module-level ``_conn`` had no invalidation
    path, so once it went bad (closed handle, corrupted file, disk error)
    every subsequent liveness + reconcile tick failed identically until the
    process was restarted. Called by ``get_conn()`` when a health probe
    fails; exposed separately so tests (and other error paths) can force it.
    """
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except sqlite3.Error:
            pass
    _conn = None


def get_conn() -> sqlite3.Connection:
    """Return the module-level connection, self-healing if it has gone bad.

    Probes with a trivial query before returning; on failure, invalidates
    and rebuilds once. The probe cost is negligible (sub-millisecond on an
    already-open SQLite handle) against the cost of silently wedging every
    caller for the rest of the process lifetime.
    """
    global _conn
    if _conn is None:
        init_db()
        assert _conn is not None
    if threading.get_ident() != _conn_owner:
        # Never hand the shared handle to a different thread — concurrent
        # execute() on one sqlite3 connection raises sporadic
        # InterfaceError. Give each thread its own handle to the active DB
        # (WAL mode makes multi-connection access safe); keyed by path so a
        # re-init against a different file invalidates stale handles.
        tconn = getattr(_thread_local, "conn", None)
        if tconn is not None and getattr(_thread_local, "path", None) == _active_path:
            try:
                tconn.execute("SELECT 1")
                return tconn
            except sqlite3.Error:
                try:
                    tconn.close()
                except sqlite3.Error:
                    pass
        tconn = sqlite3.connect(str(_active_path), check_same_thread=False)
        tconn.row_factory = sqlite3.Row
        tconn.execute("PRAGMA busy_timeout=5000")
        _thread_local.conn = tconn
        _thread_local.path = _active_path
        return tconn
    try:
        _conn.execute("SELECT 1")
    except sqlite3.Error:
        logger.warning("dashboard_db: connection unhealthy, rebuilding", exc_info=True)
        reset_conn()
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
    model: str | None = None,
    harness_state: str = "{}",
    bead_id: str | None = None,
    jsonl_path: str | None = None,
    session_uuid: str | None = None,
    resolution_dir: str | None = None,
    harness_token: str | None = None,
    state: str = "ACTIVE",
) -> None:
    """INSERT a new session row. Raises sqlite3.IntegrityError on duplicate name.

    ``state`` is the birth lifecycle state: ACTIVE for registration of an
    already-running process (seed, dispatch IPC, post-spawn register);
    LAUNCHING for launch entrypoints (register_pending), whose arm then
    moves through the authority as LAUNCHING→LAUNCHING.
    """
    import json as _json

    assert state in ("LAUNCHING", "ACTIVE")
    conn = get_conn()
    # Build session_uuids and curr_jsonl_file from initial values
    session_uuids = _json.dumps([session_uuid]) if session_uuid else "[]"
    curr_jsonl_file = jsonl_path  # initially same as jsonl_path
    conn.execute(
        "INSERT INTO tmux_sessions"
        " (tmux_name, type, project, harness, model, harness_state,"
        "  bead_id, jsonl_path, session_uuid,"
        "  resolution_dir, session_uuids, curr_jsonl_file, created_at,"
        "  harness_token, state, attention)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tmux_name, session_type, project, harness, model, harness_state,
         bead_id, jsonl_path, session_uuid,
         resolution_dir, session_uuids, curr_jsonl_file, time.time(),
         harness_token, state, "idle" if state == "ACTIVE" else None),
    )
    conn.commit()


def update_session_provider_identity(
    tmux_name: str,
    *,
    harness: str | None = None,
    model: str | None = None,
) -> bool:
    """Refresh provider identity on an existing monitored session row.

    The dispatcher re-registers live JSONLs idempotently. Updating these two
    columns must not re-run full registration (which would reset in-memory
    tail/parser state on every stats poll), so the monitor IPC endpoint uses
    this narrow writer before refreshing the existing watch.
    """
    parts: list[str] = []
    vals: list[object] = []
    if harness is not None:
        parts.append("harness=?")
        vals.append(harness)
    if model is not None:
        parts.append("model=?")
        vals.append(model)
    if not parts:
        return False
    vals.append(tmux_name)
    conn = get_conn()
    cur = conn.execute(
        f"UPDATE tmux_sessions SET {', '.join(parts)} WHERE tmux_name=?",
        vals,
    )
    conn.commit()
    return cur.rowcount > 0


def update_jsonl_link(
    tmux_name: str,
    session_uuid: str,
    jsonl_path: str,
    project: str | None = None,
    *,
    generation: str | None = None,
    file_offset: int | None = None,
) -> None:
    """LINK step: set session_uuid, jsonl_path, and append to session_uuids.

    Also updates curr_jsonl_file and derives resolution_dir from the file
    path. ``generation`` is the rollout generation identity (auto-suvcp
    Rule 6) — stamped in the same UPDATE as jsonl_path so the two can never
    disagree. ``file_offset`` lets promotion initialize the read cursor at
    the file's already-published level (linking never rewinds publication).
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

    parts = [
        "session_uuid=?", "jsonl_path=?",
        "resolution_dir=COALESCE(resolution_dir, ?)", "session_uuids=?",
        "curr_jsonl_file=?",
    ]
    vals: list[Any] = [
        session_uuid, jsonl_path, resolution_dir, _json.dumps(uuids), jsonl_path,
    ]
    if project:
        parts.append("project=?")
        vals.append(project)
    if generation is not None:
        parts.append("jsonl_generation=?")
        vals.append(generation)
    if file_offset is not None:
        parts.append("file_offset=?")
        vals.append(file_offset)
    vals.append(tmux_name)
    conn.execute(
        f"UPDATE tmux_sessions SET {', '.join(parts)} WHERE tmux_name=?", vals,
    )
    conn.commit()


def next_link_seq(tmux_name: str) -> int:
    """Increment and return the per-session link sequence nonce (auto-suvcp).

    Part of the generation identity ``<st_dev>:<st_ino>:<seq>`` — the nonce
    makes a re-link of a recycled inode produce a distinct generation.
    """
    conn = get_conn()
    cur = conn.execute(
        "UPDATE tmux_sessions SET link_seq = COALESCE(link_seq, 0) + 1"
        " WHERE tmux_name=? RETURNING link_seq",
        (tmux_name,),
    )
    row = cur.fetchone()
    conn.commit()
    return int(row[0]) if row else 0


def set_jsonl_generation(
    tmux_name: str,
    generation: str,
    *,
    expect_path: str,
    file_offset: int | None = None,
) -> bool:
    """Backfill or repair ``jsonl_generation`` for a persisted re-attach,
    guarded on the path still matching. ``file_offset`` (R1) rides the
    SAME UPDATE: a generation repair after an inode change must reset the
    cursor atomically with the identity write — a stale byte cursor
    against a replacement file reads mid-line and silently loses the line
    spanning it (loss is never acceptable; replacement is a failure event
    and its re-read duplicates are the accepted residual).
    """
    conn = get_conn()
    parts = ["jsonl_generation=?"]
    vals: list[Any] = [generation]
    if file_offset is not None:
        parts.append("file_offset=?")
        vals.append(file_offset)
    vals.extend([tmux_name, expect_path])
    cur = conn.execute(
        f"UPDATE tmux_sessions SET {', '.join(parts)}"
        " WHERE tmux_name=? AND jsonl_path=?",
        vals,
    )
    conn.commit()
    return cur.rowcount > 0


def link_and_enrich(
    tmux_name: str,
    session_uuid: str,
    jsonl_path: str,
    project: str | None = None,
    *,
    generation: str | None = None,
    file_offset: int | None = None,
) -> None:
    """LINK + ENRICH in one shot: set session_uuid, jsonl_path, and graph_source_id.

    1. Updates dashboard.db with session_uuid and jsonl_path (plus, when
       provided, the generation identity and cursor in the SAME UPDATE —
       auto-suvcp B6 atomicity)
    2. Runs `graph ingest-session` to ingest the JSONL and get the graph source ID
    3. Updates dashboard.db with graph_source_id

    This is the ONLY function that should be called when a JSONL is discovered
    (by the watcher or the Link Terminal handshake).

    R3 (auto-suvcp round 2): the generation identity and cursor are
    derived HERE by default — callers cannot opt out of atomicity. A
    caller-supplied ``generation``/``file_offset`` (the host-rollover
    path) still wins; otherwise the generation is stat-derived and, when
    the link moves to a DIFFERENT path than the row currently holds, the
    cursor resets to 0 in the same UPDATE (the old offset describes the
    old file's bytes).
    """
    import subprocess

    if generation is None:
        try:
            st = Path(jsonl_path).stat()
            seq = next_link_seq(tmux_name)
            generation = f"{st.st_dev}:{st.st_ino}:{seq}"
        except OSError:
            generation = None
    if file_offset is None:
        conn = get_conn()
        row = conn.execute(
            "SELECT jsonl_path FROM tmux_sessions WHERE tmux_name=?",
            (tmux_name,),
        ).fetchone()
        if row is None or row[0] != jsonl_path:
            file_offset = 0

    # LINK: write session_uuid and jsonl_path (+ generation/cursor atomically)
    update_jsonl_link(
        tmux_name, session_uuid, jsonl_path, project,
        generation=generation, file_offset=file_offset,
    )
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
        from tools.graph.cross_org import all_store_slugs, open_peer_db
    except Exception:
        return False
    for slug in all_store_slugs():
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
        from tools.graph.cross_org import all_store_slugs, open_peer_db
    except Exception:
        return None
    for slug in all_store_slugs():
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


def _resolve_source_and_org_by_path(jsonl_path: str) -> tuple[str, str] | None:
    """Like :func:`_resolve_source_id_by_path` but also returns the org slug
    the source was found in, so a write-through targets the right store."""
    if not jsonl_path:
        return None
    try:
        from tools.graph.cross_org import all_store_slugs, open_peer_db
    except Exception:
        return None
    for slug in all_store_slugs():
        peer = open_peer_db(slug)
        if peer is None:
            continue
        try:
            row = peer.get_source_by_path(jsonl_path)
        except Exception:
            row = None
        if row and row.get("id"):
            return row["id"], slug
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

    W6 addition: when a repair links a row that already carries a
    dashboard-set ``label``, push that label onto the newly-linked source's
    title. This closes a gap opened by W5 (title derivation is now
    creation-only): if an operator runs ``set-label`` before
    ``graph_source_id`` gets linked, ``api_session_label``'s write-through
    no-ops (no id to write to yet), and post-W5 nothing else would ever
    apply that label — the old code healed this on the session's next
    re-ingest via ``_derive_session_title``, which no longer runs here.
    """
    conn = get_conn()
    if live_only:
        rows = conn.execute(
            "SELECT tmux_name, graph_source_id, jsonl_path, label FROM tmux_sessions"
            " WHERE state NOT IN ('ENDED','FAILED') AND jsonl_path IS NOT NULL AND jsonl_path != ''"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT tmux_name, graph_source_id, jsonl_path, label FROM tmux_sessions"
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
        resolved = _resolve_source_and_org_by_path(jpath)
        if not resolved:
            # JSONL not yet ingested. Try again next tick.
            continue
        real_id, source_org = resolved
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

        label = (row["label"] or "").strip() if "label" in row.keys() else ""
        if label:
            try:
                from tools.graph import ops as graph_ops
                # Write the title into the org the source actually lives in
                # — the caller org (personal) is not where a non-personal
                # org's source is stored, so an unscoped write would land
                # in the wrong DB and the label would never reach the title.
                graph_ops.update_source_title(real_id, label, org=source_org)
            except Exception:
                logger.warning(
                    "dashboard_db: reconcile label write-through failed for %s (%s)",
                    tmux_name, real_id[:11], exc_info=True,
                )
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
        from tools.graph.cross_org import all_store_slugs, open_peer_db
    except Exception:
        return None
    for slug in all_store_slugs():
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


def patch_harness_state(tmux_name: str, patch: dict) -> None:
    """Merge *patch* into the row's ``harness_state`` without a read.

    Same ``json_patch`` merge the drain path uses, so a concurrent writer's
    keys are never clobbered by a read-modify-write.
    """
    conn = get_conn()
    conn.execute(
        "UPDATE tmux_sessions SET harness_state="
        "json_patch(COALESCE(NULLIF(harness_state,''),'{}'), ?) "
        "WHERE tmux_name=?",
        (json.dumps(patch), tmux_name),
    )
    conn.commit()


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
        # Monotonic (auto-7263s): callers pass a transcript file's mtime, and
        # resuming an idle session replays its OLD transcript — whose mtime
        # predates the launch by days. A plain assignment let that clobber the
        # launch's fresh stamp, and the launch-orphan reaper then measured the
        # session's idle age against a 600s budget and false-failed the resume,
        # tearing down its file watches. Activity never moves backwards.
        parts.append("last_activity=MAX(COALESCE(last_activity,0),?)")
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


def persist_tail_state(
    tmux_name: str,
    *,
    expect_path: str,
    expect_generation: str,
    expect_offset: int | None = None,
    file_offset: int,
    last_activity: float | None = None,
    last_message: str | None = None,
    entry_count_add: int = 0,
    context_tokens: int | None = None,
    model: str | None = None,
    harness_state_patch: str | None = None,
) -> bool:
    """Drain-owner persistence with generation CAS (auto-suvcp Rule 6).

    The write lands only if ``jsonl_path`` AND ``jsonl_generation`` still
    match what the drain pass read — a drain racing a rollover or re-link
    silently drops its ack instead of corrupting the successor's cursor
    (OffsetCoherent).

    ``expect_offset`` (when given) extends the CAS to the row's cursor: the
    ack lands only if ``file_offset`` is still where this window's read
    began. revive_session resets the cursor to 0 for a resume's full
    backfill WITHOUT claiming the drain gate; path and generation both
    survive a revive (same file, same inode), so without this term an
    in-flight pre-revive window's ack wrote its high ``new_offset`` back
    over the reset and the backfill silently never happened.

    ``harness_state_patch`` is merged INTO the row inside the UPDATE via
    ``json_patch`` (B7): the patch carries only the keys this drain pass
    itself changed, so a concurrent screen-poller write (e.g.
    ``composer_ready``) is never clobbered by a read-modify-write that
    straddled an await (ComposerSticky).

    ``entry_count_add`` is an increment, not an absolute, for the same
    reason. Lifecycle columns are deliberately absent from this statement —
    ``state``/``startup_state`` belong exclusively to STATE_AUTHORITY.

    Returns True iff the row accepted the write.
    """
    conn = get_conn()
    parts = ["file_offset=?", "entry_count=COALESCE(entry_count,0)+?"]
    vals: list[Any] = [file_offset, entry_count_add]
    if last_activity is not None:
        # Monotonic (auto-7263s): callers pass a transcript file's mtime, and
        # resuming an idle session replays its OLD transcript — whose mtime
        # predates the launch by days. A plain assignment let that clobber the
        # launch's fresh stamp, and the launch-orphan reaper then measured the
        # session's idle age against a 600s budget and false-failed the resume,
        # tearing down its file watches. Activity never moves backwards.
        parts.append("last_activity=MAX(COALESCE(last_activity,0),?)")
        vals.append(last_activity)
    if last_message is not None:
        parts.append("last_message=?")
        vals.append(last_message)
    if context_tokens is not None:
        parts.append("context_tokens=?")
        vals.append(context_tokens)
    if model is not None:
        parts.append("model=?")
        vals.append(model)
    if harness_state_patch is not None:
        parts.append(
            "harness_state=json_patch(COALESCE(NULLIF(harness_state,''),'{}'), ?)"
        )
        vals.append(harness_state_patch)
    vals.extend([tmux_name, expect_path, expect_generation])
    where = (
        " WHERE tmux_name=? AND jsonl_path=?"
        "   AND COALESCE(jsonl_generation,'')=?"
    )
    if expect_offset is not None:
        where += " AND COALESCE(file_offset,0)=?"
        vals.append(expect_offset)
    cur = conn.execute(
        f"UPDATE tmux_sessions SET {', '.join(parts)}{where}",
        vals,
    )
    conn.commit()
    return cur.rowcount > 0


def increment_entry_count(
    tmux_name: str,
    add: int,
    *,
    last_message: str | None = None,
    last_activity: float | None = None,
) -> None:
    """Handover publication accounting (auto-suvcp Rule 4/6).

    Publishing a never-linked predecessor advances the session's cumulative
    ``entry_count`` (it counts lines published across ALL of the session's
    files) without touching ``file_offset`` — the offset cursor belongs to
    the currently linked file only.
    """
    conn = get_conn()
    parts = ["entry_count=COALESCE(entry_count,0)+?"]
    vals: list[Any] = [add]
    if last_message is not None:
        parts.append("last_message=?")
        vals.append(last_message)
    if last_activity is not None:
        # Monotonic (auto-7263s): callers pass a transcript file's mtime, and
        # resuming an idle session replays its OLD transcript — whose mtime
        # predates the launch by days. A plain assignment let that clobber the
        # launch's fresh stamp, and the launch-orphan reaper then measured the
        # session's idle age against a 600s budget and false-failed the resume,
        # tearing down its file watches. Activity never moves backwards.
        parts.append("last_activity=MAX(COALESCE(last_activity,0),?)")
        vals.append(last_activity)
    vals.append(tmux_name)
    conn.execute(
        f"UPDATE tmux_sessions SET {', '.join(parts)} WHERE tmux_name=?", vals,
    )
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


def update_last_input_at(tmux_name: str, timestamp: float | None = None) -> float:
    """Record one successful direct operator send, monotonically.

    Returns the timestamp offered to the row so the sending browser can update
    its already-local session store without another request.
    """
    value = float(timestamp if timestamp is not None else time.time())
    conn = get_conn()
    conn.execute(
        "UPDATE tmux_sessions SET last_input_at="
        "MAX(COALESCE(last_input_at, 0), ?) WHERE tmux_name=?",
        (value, tmux_name),
    )
    conn.commit()
    return value


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
        "SELECT tmux_name FROM tmux_sessions WHERE dispatch_nag=1 AND state NOT IN ('ENDED','FAILED')"
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
    # Death is a lifecycle transition like any other: route through the
    # authority (legality matrix, atomic write incl. write-through
    # projections, broadcast hook). Import deferred — the worker module
    # imports this one lazily inside methods, so this cannot cycle.
    from tools.dashboard.session_lifecycle_worker import STATE_AUTHORITY
    changed = STATE_AUTHORITY.transition(
        tmux_name, "ENDED", cause=f"death-detected@{caller_loc}",
    )
    logger.info(
        "dashboard_db: mark_dead  tmux=%s  changed=%s  caller=%s",
        tmux_name, changed, caller_loc,
    )


def update_disk_usage(
    tmux_name: str, disk_bytes: int, disk_detail: str, sampled_at: float,
) -> None:
    """Persist a session's disk footprint (resource_monitor writes these)."""
    conn = get_conn()
    conn.execute(
        "UPDATE tmux_sessions SET disk_bytes=?, disk_detail=?, disk_sampled_at=?"
        " WHERE tmux_name=?",
        (disk_bytes, disk_detail, sampled_at, tmux_name),
    )
    conn.commit()


def update_activity_state(tmux_name: str, state: str) -> None:
    """Record presence telemetry (working|idle) for an ACTIVE session.

    This is the ATTENTION writer — not lifecycle state. The guard is load-
    bearing: the tracker fires on tailed JSONL entries, which can trail a
    lifecycle transition (a batch landing after a stop/failure), and an
    unguarded write here used to punch 'idle'/'working' into dead rows.
    """
    assert state in ("tool_running", "thinking", "idle"), state
    conn = get_conn()
    conn.execute(
        "UPDATE tmux_sessions SET attention=?"
        " WHERE tmux_name=? AND state='ACTIVE'",
        (state, tmux_name),
    )
    conn.commit()


def delete_session(tmux_name: str) -> None:
    """Hard-delete a session row (used for cleanup of expired dead sessions)."""
    conn = get_conn()
    conn.execute("DELETE FROM tmux_sessions WHERE tmux_name=?", (tmux_name,))
    conn.commit()


# ── Queries ─────────────────────────────────────────────────────


_TERMINAL_STATES_SQL = "('ENDED','FAILED')"


def get_live_sessions() -> list[dict]:
    """Return all non-terminal sessions (LAUNCHING/ACTIVE/STOPPING).

    The NULL-state arm is the compat belt for rows written by raw INSERTs
    that bypass the sanctioned birth helpers (test fixtures, external
    writers); the migration backfill means production rows carry state.
    """
    conn = get_conn()
    rows = conn.execute(
        f"SELECT * FROM tmux_sessions WHERE state NOT IN {_TERMINAL_STATES_SQL}"
        " OR state IS NULL"
    ).fetchall()
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
                " WHERE state NOT IN ('ENDED','FAILED') ORDER BY COALESCE(last_activity, created_at) DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tmux_sessions ORDER BY COALESCE(last_activity, created_at) DESC"
            ).fetchall()
    else:
        if live_only:
            rows = conn.execute(
                "SELECT * FROM tmux_sessions"
                " WHERE state NOT IN ('ENDED','FAILED') AND COALESCE(last_activity, created_at) > ?"
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


def get_tmux_name_for_source(graph_source_id: str, session_uuid: str | None = None) -> str | None:
    """Reverse lookup: return the tmux_name linked to a graph source.

    Tries the validated ``graph_source_id`` link first; falls back to
    ``session_uuid`` (the JSONL stem stored in source metadata) so freshly
    ingested sources resolve before the linker runs. Returns None when
    nothing matches.
    """
    if not graph_source_id and not session_uuid:
        return None
    conn = get_conn()
    if graph_source_id:
        row = conn.execute(
            "SELECT tmux_name FROM tmux_sessions WHERE graph_source_id=? LIMIT 1",
            (graph_source_id,),
        ).fetchone()
        if row and row["tmux_name"]:
            return row["tmux_name"]
    if session_uuid:
        row = conn.execute(
            "SELECT tmux_name FROM tmux_sessions WHERE session_uuid=? LIMIT 1",
            (session_uuid,),
        ).fetchone()
        if row and row["tmux_name"]:
            return row["tmux_name"]
    return None


def is_session_live(tmux_name: str) -> bool:
    """Return True iff a row exists for ``tmux_name`` in a non-terminal state.

    Used by the targeted dashboard-approval nag (auto-rh2r5) to decide
    whether the authoring session is still alive to receive the message.
    Missing rows and terminal rows both return False.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT state NOT IN ('ENDED','FAILED') AS is_live"
        " FROM tmux_sessions WHERE tmux_name=?", (tmux_name,)
    ).fetchone()
    return bool(row and row["is_live"])


def get_tailable_sessions() -> list[dict]:
    """Return live sessions that have a jsonl_path set (ready for tailing)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tmux_sessions WHERE state NOT IN ('ENDED','FAILED') AND jsonl_path IS NOT NULL"
    ).fetchall()
    return [dict(r) for r in rows]


def get_sessions_needing_resolution() -> list[dict]:
    """Return live sessions with no jsonl_path yet (need directory resolution or watcher link)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM tmux_sessions WHERE state NOT IN ('ENDED','FAILED') AND jsonl_path IS NULL"
    ).fetchall()
    return [dict(r) for r in rows]


def session_exists(tmux_name: str) -> bool:
    """Check if a session with this name exists in the DB."""
    conn = get_conn()
    row = conn.execute("SELECT 1 FROM tmux_sessions WHERE tmux_name=?", (tmux_name,)).fetchone()
    return row is not None


def count_live() -> int:
    """Count non-terminal sessions."""
    conn = get_conn()
    row = conn.execute(
        f"SELECT COUNT(*) FROM tmux_sessions WHERE state NOT IN {_TERMINAL_STATES_SQL}"
        " OR state IS NULL"
    ).fetchone()
    return row[0] if row else 0


def find_dead_session(session_uuid: str | None = None, file_path: str | None = None) -> dict | None:
    """Find a terminal (ENDED/FAILED) session by session_uuid or jsonl_path.

    Checks session_uuid first, then falls back to jsonl_path.
    Returns the full row as a dict, or None.
    """
    conn = get_conn()
    if session_uuid:
        row = conn.execute(
            "SELECT * FROM tmux_sessions WHERE session_uuid=? AND state IN ('ENDED','FAILED')"
            " ORDER BY created_at DESC LIMIT 1",
            (session_uuid,),
        ).fetchone()
        if row:
            return dict(row)
    if file_path:
        row = conn.execute(
            "SELECT * FROM tmux_sessions WHERE jsonl_path=? AND state IN ('ENDED','FAILED')"
            " ORDER BY created_at DESC LIMIT 1",
            (file_path,),
        ).fetchone()
        if row:
            return dict(row)
    return None


def find_live_session(session_uuid: str | None = None, file_path: str | None = None) -> dict | None:
    """Find a non-terminal session by session_uuid or jsonl_path.

    Checks session_uuid first, then falls back to jsonl_path.
    Returns the full row as a dict, or None.
    """
    conn = get_conn()
    if session_uuid:
        row = conn.execute(
            "SELECT * FROM tmux_sessions WHERE session_uuid=? AND state NOT IN ('ENDED','FAILED')"
            " ORDER BY created_at DESC LIMIT 1",
            (session_uuid,),
        ).fetchone()
        if row:
            return dict(row)
    if file_path:
        row = conn.execute(
            "SELECT * FROM tmux_sessions WHERE jsonl_path=? AND state NOT IN ('ENDED','FAILED')"
            " ORDER BY created_at DESC LIMIT 1",
            (file_path,),
        ).fetchone()
        if row:
            return dict(row)
    return None


def revive_session(tmux_name: str, *, file_offset: int = 0) -> None:
    """Row-prep for a relaunch: reset the tail offset for full backfill.

    harness_state resets too: it describes the PREVIOUS process's screen. A
    stale composer_ready=true from the old boot would satisfy the
    composer-ready injection gate before the relaunched harness accepts
    input; the pane-poller re-derives fresh state within a poll interval."""
    conn = get_conn()
    # Row-prep only: the lifecycle STATE change (→ LAUNCHING, is_live,
    # ended_at clear) happens at the arm_startup_state call that
    # immediately follows, as one atomic authority write.
    conn.execute(
        "UPDATE tmux_sessions SET"
        "  file_offset=?,"
        "  harness_state='{}'"
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
        "  created_at, file_offset, last_message, label,"
        "  harness_token, state, attention, ended_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
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
        "  harness_token = COALESCE(excluded.harness_token, harness_token),"
        # When a previously-dead row is revived via seed/IPC
        # re-registration, the one state follows liveness. Non-dead
        # states are preserved. This is a sanctioned birth/seed writer;
        # everything after registration goes through the authority.
        "  state = CASE"
        "    WHEN excluded.state='ACTIVE' AND (state IS NULL OR state IN ('ENDED','FAILED'))"
        "      THEN 'ACTIVE'"
        "    WHEN excluded.state='ENDED' AND (state IS NULL OR state NOT IN ('ENDED','FAILED'))"
        "      THEN 'ENDED'"
        "    ELSE state END,"
        "  ended_at = CASE"
        "    WHEN excluded.state='ACTIVE' THEN NULL"
        "    ELSE COALESCE(ended_at, excluded.ended_at) END",
        (
            tmux_name, session_type, project, harness, harness_state,
            bead_id, jsonl_path, session_uuid,
            resolution_dir, session_uuids, curr_jsonl_file,
            created_at or time.time(), file_offset, last_message,
            label,
            harness_token,
            "ACTIVE" if is_live else "ENDED",
            "idle" if is_live else None,
            None if is_live else time.time(),
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


# ── W4: ingest manifest (auto-gah4g) ────────────────────────────────


def get_manifest_entry(file_path: str) -> dict | None:
    """Look up one file's manifest row, or None if never seen."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM ingest_manifest WHERE file_path = ?", (file_path,)
    ).fetchone()
    return dict(row) if row else None


def upsert_manifest_entry(
    file_path: str, *, size: int, mtime: float, inode: int | None,
    org: str | None, source_id: str | None, ingest_offset: int,
    state: str = "active", last_seen_at: float | None = None,
) -> None:
    """Insert or fully overwrite one file's manifest row.

    Called after a sweep pass touches ``file_path`` (new file, changed
    file, or a state transition like sealing) — always writes the
    complete row rather than a partial patch, since the sweep always has
    every field in hand at the point it calls this.
    """
    conn = get_conn()
    conn.execute(
        """INSERT INTO ingest_manifest
               (file_path, size, mtime, inode, org, source_id, ingest_offset, state, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(file_path) DO UPDATE SET
               size=excluded.size, mtime=excluded.mtime, inode=excluded.inode,
               org=excluded.org, source_id=excluded.source_id,
               ingest_offset=excluded.ingest_offset, state=excluded.state,
               last_seen_at=excluded.last_seen_at""",
        (file_path, size, mtime, inode, org, source_id, ingest_offset, state,
         last_seen_at if last_seen_at is not None else time.time()),
    )
    conn.commit()


def seal_manifest_entry(file_path: str) -> None:
    """Mark a file 'sealed' — dead + unchanged, skipped by routine sweeps
    until --force or the deep integrity pass. Leaves every other column
    (size/mtime/inode/org/source_id/ingest_offset) untouched."""
    conn = get_conn()
    conn.execute(
        "UPDATE ingest_manifest SET state = 'sealed', last_seen_at = ? WHERE file_path = ?",
        (time.time(), file_path),
    )
    conn.commit()


def get_sealed_manifest_paths() -> set[str]:
    """Every currently-sealed file_path — the sweep's skip-set."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT file_path FROM ingest_manifest WHERE state = 'sealed'"
    ).fetchall()
    return {r["file_path"] for r in rows}


def get_active_manifest_paths() -> set[str]:
    """Every file_path not already sealed — candidates for the sweep's
    'no longer found on disk' seal-detection pass."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT file_path FROM ingest_manifest WHERE state != 'sealed'"
    ).fetchall()
    return {r["file_path"] for r in rows}
