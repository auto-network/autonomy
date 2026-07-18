"""On-demand operator-approval rendezvous — one table, any kind.

Generalizes the commit-signing rendezvous: a requester (an agent's shim, a
future broker, ...) creates a pending request of some ``kind``; the operator
reviews it in the dashboard and posts a decision; the requester consumes the
result. A request is pending while ``result`` is NULL; a decision is JSON that
always carries ``approved`` (true/false) plus kind-specific fields — e.g.
``commit_sign`` approval carries the armored ``signature``, and operator edits
ride along as extra keys. First writer wins on the decision. Nothing here
executes or signs — it only carries the request and the decision.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = Path(os.environ.get("APPROVAL_REQUESTS_DB", str(REPO_ROOT / "data" / "approval_requests.db")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_requests (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,   -- e.g. 'commit_sign'; per-kind behavior lives in handlers, not columns
    session     TEXT NOT NULL,   -- the requesting session (the routing key)
    request     TEXT NOT NULL,   -- kind-specific JSON: what the operator reviews / what gets executed
    staged      TEXT,            -- server-frozen execution context, written ONCE at first render
    result      TEXT,            -- NULL = pending; decision JSON {"approved": bool, ...kind fields}
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approval_requests_pending
    ON approval_requests(session, created_at) WHERE result IS NULL;
"""

def _conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(db_path) if db_path is not None else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)  # cheap CREATE IF NOT EXISTS; keeps callers simple
    try:  # pre-``staged`` databases: additive migration
        c.execute("ALTER TABLE approval_requests ADD COLUMN staged TEXT")
        c.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    return c


def init_db(db_path: Path | str | None = None) -> None:
    _conn(db_path).close()


def create(*, kind: str, session: str, request: dict, created_at: float,
           db_path: Path | str | None = None) -> str:
    """Insert a new pending request; return its id."""
    rid = uuid.uuid4().hex[:12]
    c = _conn(db_path)
    try:
        c.execute(
            "INSERT INTO approval_requests (id, kind, session, request, result, created_at) "
            "VALUES (?, ?, ?, ?, NULL, ?)",
            (rid, kind, session, json.dumps(request), created_at),
        )
        c.commit()
    finally:
        c.close()
    return rid


def get(request_id: str, db_path: Path | str | None = None) -> dict | None:
    """The row with ``request`` and ``result`` parsed back to dict / dict-or-None."""
    c = _conn(db_path)
    try:
        r = c.execute("SELECT * FROM approval_requests WHERE id = ?", (request_id,)).fetchone()
    finally:
        c.close()
    if not r:
        return None
    d = dict(r)
    d["request"] = json.loads(d["request"])
    d["staged"] = json.loads(d["staged"]) if d.get("staged") is not None else None
    d["result"] = json.loads(d["result"]) if d["result"] is not None else None
    return d


def set_staged(request_id: str, staged: dict, db_path: Path | str | None = None) -> bool:
    """Freeze the server-computed execution context (e.g. the exact registry
    request a link_publish will forward, destination included) the first
    time the request is rendered. Write-once — first writer wins — so what
    the operator was shown is immutably what an approval executes; nothing
    client-supplied and no later state change can move it. Returns True iff
    this call did the freeze."""
    c = _conn(db_path)
    try:
        cur = c.execute(
            "UPDATE approval_requests SET staged = ? WHERE id = ? AND staged IS NULL",
            (json.dumps(staged), request_id),
        )
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def set_result(request_id: str, result: dict, db_path: Path | str | None = None) -> bool:
    """Attach the operator's decision. Only writes a still-pending row (first
    writer wins); returns True iff it updated one."""
    c = _conn(db_path)
    try:
        cur = c.execute(
            "UPDATE approval_requests SET result = ? WHERE id = ? AND result IS NULL",
            (json.dumps(result), request_id),
        )
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def pending_for_session(session: str, db_path: Path | str | None = None) -> dict | None:
    """The oldest pending request for a session as ``{"id", "kind"}``, or None —
    backs the session viewer's ``pending_approval`` field."""
    c = _conn(db_path)
    try:
        r = c.execute(
            "SELECT id, kind FROM approval_requests WHERE session = ? AND result IS NULL "
            "ORDER BY created_at LIMIT 1",
            (session,),
        ).fetchone()
        return {"id": r["id"], "kind": r["kind"]} if r else None
    finally:
        c.close()
