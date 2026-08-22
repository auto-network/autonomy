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

from tools.data_paths import resolve_store

REPO_ROOT = Path(__file__).resolve().parents[3]
DB_PATH = resolve_store("approval_requests")

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
CREATE INDEX IF NOT EXISTS idx_approval_requests_decided_kind
    ON approval_requests(kind, id) WHERE result IS NOT NULL;
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
           staged: dict | None = None,
           db_path: Path | str | None = None) -> str:
    """Insert a new pending request; return its id."""
    rid = uuid.uuid4().hex[:12]
    c = _conn(db_path)
    try:
        c.execute(
            "INSERT INTO approval_requests "
            "(id, kind, session, request, staged, result, created_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?)",
            (rid, kind, session, json.dumps(request),
             json.dumps(staged) if staged is not None else None, created_at),
        )
        c.commit()
    finally:
        c.close()
    return rid


def create_idempotent(
    *,
    request_id: str,
    kind: str,
    session: str,
    request: dict,
    created_at: float,
    staged: dict | None = None,
    db_path: Path | str | None = None,
) -> bool:
    """Create a request under a caller-derived stable id.

    Returns ``True`` when this call inserted the row and ``False`` when the
    exact row already existed.  Reusing an id for different bytes is refused.
    This is the crash/retry seam for producers whose source object already has
    a content id (Fleet admission is the first consumer).
    """
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise ValueError("approval request_id must be a non-empty string up to 128 characters")
    request_wire = json.dumps(request, sort_keys=True, separators=(",", ":"))
    staged_wire = (
        json.dumps(staged, sort_keys=True, separators=(",", ":"))
        if staged is not None
        else None
    )
    c = _conn(db_path)
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT kind,session,request,staged,created_at "
            "FROM approval_requests WHERE id=?",
            (request_id,),
        ).fetchone()
        if row is not None:
            existing_request = json.dumps(
                json.loads(row["request"]), sort_keys=True, separators=(",", ":")
            )
            existing_staged = (
                json.dumps(
                    json.loads(row["staged"]),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if row["staged"] is not None
                else None
            )
            if (
                row["kind"] != kind
                or row["session"] != session
                or existing_request != request_wire
                or existing_staged != staged_wire
                or float(row["created_at"]) != float(created_at)
            ):
                raise ValueError(
                    "approval request id is already bound to different bytes"
                )
            return False
        c.execute(
            "INSERT INTO approval_requests "
            "(id,kind,session,request,staged,result,created_at) "
            "VALUES(?,?,?,?,?,NULL,?)",
            (
                request_id,
                kind,
                session,
                request_wire,
                staged_wire,
                created_at,
            ),
        )
        c.commit()
        return True
    finally:
        c.close()


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


def pending_all(
    *, limit: int = 500, db_path: Path | str | None = None,
) -> list[dict]:
    """Bounded pending truth for transport/source-cursor reconciliation."""
    c = _conn(db_path)
    try:
        rows = c.execute(
            "SELECT id,kind,session,created_at FROM approval_requests "
            "WHERE result IS NULL ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 5000)),),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        c.close()


def decided_ids_for_kind(
    kind: str, db_path: Path | str | None = None
) -> set[str]:
    """Stable ids with a completed decision for one registered kind."""
    c = _conn(db_path)
    try:
        rows = c.execute(
            "SELECT id FROM approval_requests WHERE kind=? AND result IS NOT NULL",
            (kind,),
        ).fetchall()
        return {str(row["id"]) for row in rows}
    finally:
        c.close()


def recent_for_kind(
    kind: str,
    *,
    limit: int = 50,
    db_path: Path | str | None = None,
) -> list[dict]:
    """Newest approval records for one registered kind.

    Application projections use this to correlate their own producer records
    without creating a second approval queue.  Decision controls and lifecycle
    remain exclusively in the generic approval rendezvous.
    """
    if not isinstance(kind, str) or not kind:
        raise ValueError("approval kind must be a non-empty string")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 500:
        raise ValueError("approval limit must be an integer from 1 to 500")
    c = _conn(db_path)
    try:
        rows = c.execute(
            "SELECT * FROM approval_requests WHERE kind=? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (kind, limit),
        ).fetchall()
    finally:
        c.close()
    out = []
    for row in rows:
        item = dict(row)
        item["request"] = json.loads(item["request"])
        item["staged"] = (
            json.loads(item["staged"])
            if item.get("staged") is not None
            else None
        )
        item["result"] = (
            json.loads(item["result"])
            if item.get("result") is not None
            else None
        )
        out.append(item)
    return out
