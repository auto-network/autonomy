"""Follower-mirror local state: the single per-org cursor, the sweep
generation prune, and the bootstrap reset for a forced re-sweep.

A followed mirror (``orgs.type='followed'``) holds another organization's
public surface, filled only by the credential-free follow loop
(``fleet_sync_scheduler._sync_follow_scope``; design of record
graph://5f2f5a49-00d §10.4). Three concerns live here, apart from the shared
fleet-sync apply path so the follow-only behavior does not leak into it:

* **The cursor.** A follower's position is one integer per followed org: the
  serving member's org frontier ``F`` at the time of the completed pull,
  presented back on the next request under the organization's ledger genesis
  id as the only watermark key (record v9 §10.2). It is stored explicitly
  here rather than derived from applied-row watermarks so an empty-transaction
  frame naming a machine origin can never pollute it.

* **The generation prune (mark-and-sweep).** Each bootstrap sweep carries a
  generation: the set of public-source addresses it delivered. When the sweep
  COMPLETES the follower deletes every public-projection row in the mirror not
  in that set — ``git fetch --prune`` / ``rsync --delete`` semantics, on by
  default because the mirror is a read-only cache. An interrupted sweep prunes
  nothing (the caller only prunes after the summary verifies). Re-sync is a
  merge by id (the apply path is last-writer-wins) and a prune by generation,
  never a wipe: the mirror stays readable throughout.

* **The reset.** A too-old refusal makes the follower start a full sweep. The
  ``fleet_sync_bootstrap`` row may be COMPLETE from an earlier sweep, and a
  fresh ``sweep.begin`` would not re-enter SWEEPING on top of it, so the row is
  cleared first and the sweep anchors cleanly.
"""

from __future__ import annotations

import sqlite3

#: Public-source-linked satellite tables (they carry a real ``source_id``);
#: ``tags`` never crosses under the public projection, so it is not here.
_SOURCE_SATELLITES = ("thoughts", "derivations", "attachments")

#: The states a public-projection source row may carry in the mirror.
_PUBLIC_STATES = ("published", "canonical")

_FOLLOW_STATE_TABLE = """
    CREATE TABLE IF NOT EXISTS follow_state(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        genesis TEXT NOT NULL,
        cursor INTEGER NOT NULL
    )
"""

_FOLLOW_STATUS_TABLE = """
    CREATE TABLE IF NOT EXISTS follow_status(
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        outcome TEXT,
        at REAL,
        projection TEXT,
        refusal TEXT,
        retry_after REAL,
        error TEXT
    )
"""


def _ensure_state_table(conn: sqlite3.Connection) -> None:
    conn.execute(_FOLLOW_STATE_TABLE)


def _ensure_status_table(conn: sqlite3.Connection) -> None:
    conn.execute(_FOLLOW_STATUS_TABLE)


_STATUS_FIELDS = ("outcome", "at", "projection", "refusal", "retry_after", "error")


def write_follow_status(conn: sqlite3.Connection, fields: dict) -> None:
    """Record the last follow attempt's outcome so ``graph follow status`` can
    report the last pull, the last refusal, and when the follower will retry
    (operator ruling 2026-09-20, record v10)."""
    _ensure_status_table(conn)
    values = {name: fields.get(name) for name in _STATUS_FIELDS}
    conn.execute(
        "INSERT INTO follow_status(singleton, outcome, at, projection, refusal, "
        "retry_after, error) VALUES(1, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(singleton) DO UPDATE SET "
        "outcome=excluded.outcome, at=excluded.at, projection=excluded.projection, "
        "refusal=excluded.refusal, retry_after=excluded.retry_after, "
        "error=excluded.error",
        (values["outcome"], values["at"], values["projection"],
         values["refusal"], values["retry_after"], values["error"]),
    )
    conn.commit()


def read_follow_status(conn: sqlite3.Connection) -> dict | None:
    """The last recorded follow status, or None if none recorded yet."""
    if not _table_present(conn, "follow_status"):
        return None
    row = conn.execute(
        "SELECT outcome, at, projection, refusal, retry_after, error "
        "FROM follow_status WHERE singleton=1"
    ).fetchone()
    if row is None:
        return None
    return dict(zip(_STATUS_FIELDS, row))


def read_follow_cursor(conn: sqlite3.Connection) -> tuple[str, int] | None:
    """The mirror's ``(genesis, cursor)`` for its followed org, or ``None``
    when no completed pull has recorded one yet."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='follow_state'"
    ).fetchone()
    if row is None:
        return None
    got = conn.execute(
        "SELECT genesis, cursor FROM follow_state WHERE singleton=1"
    ).fetchone()
    if got is None:
        return None
    return str(got[0]), int(got[1])


def write_follow_cursor(
    conn: sqlite3.Connection, genesis: str, cursor: int
) -> None:
    """Record the mirror's cursor for its followed org after a completed pull;
    the next request presents ``{genesis: cursor}`` as its one watermark."""
    _ensure_state_table(conn)
    conn.execute(
        "INSERT INTO follow_state(singleton, genesis, cursor) VALUES(1, ?, ?) "
        "ON CONFLICT(singleton) DO UPDATE SET genesis=excluded.genesis, "
        "cursor=excluded.cursor",
        (str(genesis), int(cursor)),
    )
    conn.commit()


def reset_bootstrap(conn: sqlite3.Connection) -> None:
    """Clear the ``fleet_sync_bootstrap`` row so a forced re-sweep (after a
    too-old refusal) anchors a fresh sweep instead of colliding with a
    COMPLETE bootstrap that would refuse the re-entry into SWEEPING."""
    present = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='fleet_sync_bootstrap'"
    ).fetchone()
    if present is None:
        return
    conn.execute("DELETE FROM fleet_sync_bootstrap")
    conn.commit()


def prune_to_generation(
    conn: sqlite3.Connection, carried_source_ids: set[str]
) -> int:
    """Delete every public-projection source the completed sweep did not carry,
    and the satellites orphaned by that delete. ``carried_source_ids`` is the
    current sweep generation — every ``sources.id`` a public row was applied
    for. Returns the number of source rows pruned.

    One transaction, so the mirror is never seen half-pruned. A carried set
    that (spuriously) contains every live public id prunes nothing, which is
    the correct outcome for an unchanged surface.
    """
    conn.execute("DROP TABLE IF EXISTS _follow_carried")
    conn.execute("CREATE TEMP TABLE _follow_carried(id TEXT PRIMARY KEY)")
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO _follow_carried(id) VALUES(?)",
            [(sid,) for sid in carried_source_ids],
        )
        placeholders = ",".join("?" for _ in _PUBLIC_STATES)
        cur = conn.execute(
            f"DELETE FROM sources WHERE publication_state IN ({placeholders}) "
            f"AND id NOT IN (SELECT id FROM _follow_carried)",
            _PUBLIC_STATES,
        )
        pruned = cur.rowcount if cur.rowcount is not None else 0
        # Satellites orphaned by the source prune. Done explicitly rather than
        # by ON DELETE CASCADE so the outcome does not depend on the
        # connection's foreign_keys pragma, and so attachments (which declare
        # no cascade) are pruned too.
        for table in _SOURCE_SATELLITES:
            if not _table_present(conn, table):
                continue
            conn.execute(
                f"DELETE FROM {table} WHERE source_id IS NOT NULL "
                f"AND source_id NOT IN (SELECT id FROM sources)"
            )
        # Settings the projection no longer admits. A mirror filled before
        # the follower-visible set allowlist existed (2026-09-25) holds the
        # org's ledger events, member profiles and fleet reachability; a
        # completed sweep is the moment the mirror is brought back to exactly
        # the public surface, so those rows leave here too.
        if _table_present(conn, "settings"):
            from .projection import FOLLOW_VISIBLE_SET_IDS

            allowed = sorted(FOLLOW_VISIBLE_SET_IDS)
            marks = ",".join("?" for _ in allowed)
            conn.execute(
                f"DELETE FROM settings WHERE set_id NOT IN ({marks})", allowed
            )
        conn.commit()
        return pruned
    finally:
        conn.execute("DROP TABLE IF EXISTS _follow_carried")


def _table_present(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None
