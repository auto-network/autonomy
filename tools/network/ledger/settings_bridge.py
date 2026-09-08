"""Ledger events ARE Settings rows (design of record graph://53b5bb04-bc0).

The ruling that design serves: Settings is the table-synchronization method;
a bespoke table is allowed only with a concrete mechanical reason. Its verdict
for the event log is *convert*, not mirror: ``ledger_events`` becomes an
org-homed append-only Settings set keyed by the event id, which is the content
hash of the event's own signed bytes.

One home, therefore:

* **Write** — appending an event writes exactly one Settings row. That write is
  what the capture triggers replicate, so an event is on the wire by virtue of
  being stored, not by a second bookkeeping step.
* **Read** — a store loads its events from those rows and rebuilds its in-memory
  graph from them. Parent links are inside each signed event; heads are computed
  from the graph.
* **Receive** — a co-member's events arrive as ordinary replicated rows, into the
  same place this store reads from. There is no absorb step, because there is
  nowhere else to absorb them to.

The interim shape (bead auto-dqemk) kept ``ledger_events`` as the store and
mirrored each event into the set as a transport copy. Two copies of the same
bytes cannot be written atomically -- SQLite locks per file, so the mirror had
to be a second connection after the first transaction committed -- which is why
that arrangement needed a repair pass and a correspondence check. Converting
removes the second copy and every mechanism that existed to reconcile it.

:func:`migrate_events_to_settings` carries a legacy store's rows across on open.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

SET_ID = "autonomy.org.ledger-event"
REVISION = 1


def _slug_of(path) -> str:
    from pathlib import Path as _Path

    return _Path(str(path)).stem


#: The settings table as tools/graph/db.py defines it. Created only when
#: absent, which is the case for a bare ledger file (tests, tooling); a real
#: organization database already has it and this is a no-op there.
_SETTINGS_DDL = (
    "CREATE TABLE IF NOT EXISTS settings ("
    " id TEXT PRIMARY KEY,"
    " set_id TEXT NOT NULL,"
    " schema_revision INTEGER NOT NULL,"
    " key TEXT NOT NULL,"
    " payload TEXT NOT NULL,"
    " publication_state TEXT NOT NULL DEFAULT 'raw'"
    "   CHECK (publication_state IN ('raw','curated','published','canonical')),"
    " supersedes TEXT,"
    " excludes TEXT,"
    " deprecated INTEGER NOT NULL DEFAULT 0 CHECK (deprecated IN (0,1)),"
    " successor_id TEXT,"
    " created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),"
    " updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
    ")"
)


def ensure_settings_table(conn) -> None:
    """Make sure the connection's database can hold event rows."""
    conn.execute(_SETTINGS_DDL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_settings_set ON settings(set_id, key)")


def read_event_wires(conn) -> dict:
    """``{event_id: wire}`` for every event stored in *conn*'s database.

    Read on the store's OWN connection, against the file it was opened on.
    A ledger loads its own store's rows and never a peer's, so there is no
    org resolution here -- the path the caller gave is the answer.
    """
    import sqlite3

    try:
        rows = conn.execute(
            'SELECT "key", payload FROM settings WHERE set_id=? '
            "AND supersedes IS NULL AND excludes IS NULL AND deprecated=0",
            (SET_ID,),
        ).fetchall()
    except sqlite3.Error:
        return {}
    out: dict = {}
    for key, payload in rows:
        try:
            body = json.loads(payload) if isinstance(payload, str) else payload
            wire = body.get("wire") if isinstance(body, dict) else None
        except (ValueError, TypeError):
            continue
        if isinstance(wire, str) and wire:
            out[str(key)] = wire
    return out


def write_event(conn, event_id: str, wire: str) -> bool:
    """Store one event as its Settings row, on the caller's connection.

    Returns False when the row is already there. The caller supplies the
    transaction, so the event is stored atomically -- there is no second
    connection and no window in which the event exists in one place and not
    another. On a real organization database this INSERT is what the capture
    triggers replicate, so an event is on the wire by virtue of being stored.
    """
    import uuid

    from tools.graph.schemas.org_ledger_event import OrgLedgerEventV1

    payload = {"wire": wire}
    OrgLedgerEventV1.validate(payload)
    OrgLedgerEventV1.validate_member_key(event_id)
    existing = conn.execute(
        'SELECT 1 FROM settings WHERE set_id=? AND "key"=? LIMIT 1',
        (SET_ID, event_id),
    ).fetchone()
    if existing is not None:
        return False
    conn.execute(
        "INSERT INTO settings(id, set_id, schema_revision, key, payload,"
        " publication_state) VALUES(?,?,?,?,?,'published')",
        (str(uuid.uuid4()), SET_ID, REVISION, event_id, json.dumps(payload)),
    )
    return True


def migrate_events_to_settings(conn, label: str = "") -> int:
    """Carry a legacy ``ledger_events`` table into the Settings set.

    Returns the number of events carried. Idempotent, and a no-op for a store
    that never held the table or whose rows are already across. The table is
    left in place: dropping it is a separate, later step once every store on
    every machine has been through this.
    """
    import sqlite3

    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='ledger_events' LIMIT 1"
        ).fetchone() is not None
        if not has_table:
            return 0
        legacy = {
            str(row[0]): bytes(row[1]).decode("utf-8")
            for row in conn.execute("SELECT event_id, wire FROM ledger_events")
        }
    except (sqlite3.Error, UnicodeDecodeError):
        return 0
    if not legacy:
        return 0
    carried = 0
    with conn:
        for event_id, wire in legacy.items():
            if write_event(conn, event_id, wire):
                carried += 1
    if carried:
        logger.info(
            "ledger migration: carried %d legacy event(s) of %s into %s",
            carried, label or "store", SET_ID,
        )
    return carried
