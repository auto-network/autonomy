"""Auth DB — SQLite-backed token and message storage for CrossTalk.

Database: data/auth.db
Owned by the dashboard process, never mounted into agent containers.
Stores SHA-256 hashes of session and service tokens. Raw session tokens live
in container environments; raw service tokens live only with their external
caller and in the approval result that enrolled it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

from tools.data_paths import resolve_store

#: Volume-contract rooted (auto-lr6gu): AUTH_DB, else the volume
#: default. Previously repo-relative here AND in graph/cli.py — a
#: split resolver that rooting could move only halfway.
_DB_PATH = resolve_store("auth")
_conn: sqlite3.Connection | None = None

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS session_tokens (
    token_hash      TEXT PRIMARY KEY,
    tmux_name       TEXT NOT NULL,
    created_at      REAL NOT NULL,
    revoked_at      REAL,
    org             TEXT
);

CREATE TABLE IF NOT EXISTS crosstalk_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_session  TEXT NOT NULL,
    sender_label    TEXT NOT NULL DEFAULT '',
    target_session  TEXT NOT NULL,
    source_id       TEXT,
    turn            INTEGER,
    message         TEXT NOT NULL,
    timestamp       REAL NOT NULL,
    delivered       INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_crosstalk_target
    ON crosstalk_messages(target_session, id);
"""


def init_db(db_path: Path | None = None) -> None:
    """Initialise auth.db and create schema. Idempotent."""
    global _conn
    path = db_path or _DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(path), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=5000")
    _conn.executescript(_SCHEMA)
    # Add the org column to a session_tokens table that predates it. The column
    # is only ADDED here; existing rows are deliberately NOT backfilled — a token
    # minted before the column existed reads back org=NULL and, if its session
    # has a workspace, is refused by the caller-org guard (a locked-out stale
    # session is the correct outcome; it re-mints with an org on relaunch).
    cols = {r["name"] for r in _conn.execute(
        "PRAGMA table_info(session_tokens)").fetchall()}
    if "org" not in cols:
        _conn.execute("ALTER TABLE session_tokens ADD COLUMN org TEXT")
    # ``kind`` distinguishes an ordinary per-session token (NULL) from a
    # machine-scoped service token (e.g. 'mcp_service'). Only ADDED here; a
    # pre-existing token reads back kind=NULL and stays an ordinary session
    # token, which is correct — a service token is only ever created by an
    # explicit install-time act or an operator-approved enrollment.
    if "kind" not in cols:
        _conn.execute("ALTER TABLE session_tokens ADD COLUMN kind TEXT")
    # Service credentials may be operator-approved for a bounded lifetime.
    # NULL means no expiry. Session-token lifetime remains owned by the session
    # lifecycle; this column is intentionally consulted only by the service
    # resolver below.
    if "expires_at" not in cols:
        _conn.execute("ALTER TABLE session_tokens ADD COLUMN expires_at REAL")
    # Scope envelope for generic external-service credentials. It is absent on
    # ordinary sessions and legacy fixed-purpose service tokens.
    if "service_scope" not in cols:
        _conn.execute("ALTER TABLE session_tokens ADD COLUMN service_scope TEXT")
    _conn.commit()
    logger.info("auth_db: initialised at %s", path)


def get_conn() -> sqlite3.Connection:
    """Return the module-level connection, initialising if needed."""
    if _conn is None:
        init_db()
    assert _conn is not None
    return _conn


# -- Token operations ----------------------------------------------------------


def insert_token(token_hash: str, tmux_name: str, org: str | None) -> None:
    """Store a hashed session token stamped with its owning organization.

    ``org`` is required at the call site (no default) so both minters decide it
    explicitly: the container launcher passes the canonical org and refuses to
    mint without one; the host CLI passes ``None`` (a local caller, unscoped).
    ``None`` is a deliberate value here, never an accidental omission.
    """
    conn = get_conn()
    conn.execute(
        "INSERT INTO session_tokens (token_hash, tmux_name, created_at, org)"
        " VALUES (?, ?, ?, ?)",
        (token_hash, tmux_name, time.time(), org),
    )
    conn.commit()


def resolve_token(token_hash: str) -> tuple[str, str | None] | None:
    """Resolve a non-revoked SESSION token hash to ``(tmux_name, org)``.

    ``org`` is the organization stamped at mint: a slug for a container token,
    ``None`` for a host/local token (or for a token minted before the org column
    existed). Returns ``None`` when the token is unknown or revoked. Callers that
    use ``org`` for authority must not treat ``None`` as local without first
    checking the session has no workspace — see the guard in server.py.

    Machine-scoped service tokens (``kind`` set) are deliberately invisible here:
    they are not session tokens and must never resolve through the session-token
    path (a service token presented off its own routes then simply fails to
    authenticate). Resolve fixed-purpose service tokens with
    :func:`resolve_service_token` and generic API-scoped tokens with
    :func:`resolve_scoped_service_token`.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT tmux_name, org FROM session_tokens"
        " WHERE token_hash=? AND revoked_at IS NULL AND kind IS NULL",
        (token_hash,),
    ).fetchone()
    return (row["tmux_name"], row["org"]) if row else None


#: Registered service-token kinds. Their authority is established by the
#: method/path-specific authenticator, never by a general session-token lookup.
MCP_SERVICE_KIND = "mcp_service"
EXTERNAL_SERVICE_KIND = "external_service"


def insert_service_token(
    token_hash: str,
    name: str,
    kind: str = MCP_SERVICE_KIND,
    *,
    expires_at: float | None = None,
    service_scope: dict | None = None,
) -> None:
    """Store a hashed machine-scoped SERVICE token.

    A service token has no organization (``org`` is NULL) and is marked with a
    ``kind`` so it never resolves as an ordinary session token. It is created by
    either an explicit install-time act or an operator-approved enrollment,
    never per session, and is machine-local: auth.db is never mounted into
    containers and never synced across the fleet.
    ``name`` is a stable identity label (e.g. ``mcp-relay-service``) used only
    for display and revocation.
    """
    conn = get_conn()
    conn.execute(
        "INSERT INTO session_tokens "
        "(token_hash, tmux_name, created_at, org, kind, expires_at, service_scope)"
        " VALUES (?, ?, ?, NULL, ?, ?, ?)",
        (
            token_hash, name, time.time(), kind, expires_at,
            json.dumps(service_scope, sort_keys=True, separators=(",", ":"))
            if service_scope is not None else None,
        ),
    )
    conn.commit()


def resolve_service_token(token_hash: str, kind: str = MCP_SERVICE_KIND) -> str | None:
    """Resolve a non-revoked service token of ``kind`` to its identity label.

    Returns the stored ``name`` (tmux_name column) or ``None`` if unknown,
    revoked, expired, or of a different kind. Fixed-purpose service tokens use
    this lookup; generic external credentials use
    :func:`resolve_scoped_service_token`. :func:`resolve_token` can see neither.
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT tmux_name FROM session_tokens"
        " WHERE token_hash=? AND revoked_at IS NULL AND kind=?"
        " AND (expires_at IS NULL OR expires_at > ?)",
        (token_hash, kind, time.time()),
    ).fetchone()
    return row["tmux_name"] if row else None


def insert_scoped_service_token(
    token_hash: str,
    name: str,
    *,
    capabilities: list[dict[str, str]],
    application_scope: str,
    resource_audience: str,
    source_approval_id: str,
    expires_at: float | None = None,
) -> None:
    """Store a generic bearer authorized for exact API method/path pairs.

    Capability records are immutable mint-time authority derived from a
    server-side registration. No wildcard or prefix semantics exist: every
    route is an explicit ``{"method": "POST", "path": "/api/..."}`` pair.
    """
    fields = {
        "application_scope": application_scope,
        "resource_audience": resource_audience,
        "source_approval_id": source_approval_id,
    }
    for field, value in fields.items():
        if not isinstance(value, str) or not value or len(value) > 256:
            raise ValueError(f"{field} must be a non-empty string up to 256 characters")
    normalized = normalize_api_capabilities(capabilities)
    insert_service_token(
        token_hash,
        name,
        kind=EXTERNAL_SERVICE_KIND,
        expires_at=expires_at,
        service_scope={
            "application_scope": application_scope,
            "resource_audience": resource_audience,
            "sourceApprovalId": source_approval_id,
            "capabilities": normalized,
        },
    )


def normalize_api_capabilities(
    capabilities: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Validate and canonicalize exact API method/path capabilities."""
    if (
        not isinstance(capabilities, list)
        or not capabilities
        or len(capabilities) > 64
    ):
        raise ValueError("scoped service token requires 1 to 64 capabilities")
    normalized = []
    for capability in capabilities:
        if not isinstance(capability, dict) or set(capability) != {"method", "path"}:
            raise ValueError("API capability must be an object")
        method = capability.get("method")
        path = capability.get("path")
        if (
            not isinstance(method, str)
            or method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
            or not isinstance(path, str)
            or not path.startswith("/api/")
            or "?" in path
            or "#" in path
        ):
            raise ValueError("API capability requires an uppercase method and exact /api/ path")
        record = {"method": method, "path": path}
        if record not in normalized:
            normalized.append(record)
    return normalized


def resolve_scoped_service_token(
    token_hash: str, *, method: str, path: str,
) -> dict | None:
    """Resolve a generic service bearer only when its exact API scope allows it."""
    conn = get_conn()
    row = conn.execute(
        "SELECT tmux_name,service_scope FROM session_tokens"
        " WHERE token_hash=? AND revoked_at IS NULL AND kind=?"
        " AND (expires_at IS NULL OR expires_at > ?)",
        (token_hash, EXTERNAL_SERVICE_KIND, time.time()),
    ).fetchone()
    if row is None or row["service_scope"] is None:
        return None
    try:
        scope = json.loads(row["service_scope"])
        capabilities = scope["capabilities"]
    except (TypeError, ValueError, KeyError):
        return None
    if (
        not isinstance(scope, dict)
        or not isinstance(capabilities, list)
        or any(not isinstance(item, dict) for item in capabilities)
    ):
        return None
    exact = {"method": method.upper(), "path": path}
    if exact not in capabilities:
        return None
    return {"name": row["tmux_name"], **scope}


def revoke_token(tmux_name: str) -> None:
    """Revoke all tokens for a given tmux session."""
    conn = get_conn()
    conn.execute(
        "UPDATE session_tokens SET revoked_at=? WHERE tmux_name=? AND revoked_at IS NULL",
        (time.time(), tmux_name),
    )
    conn.commit()


# -- Message operations --------------------------------------------------------


def insert_message(
    sender_session: str,
    sender_label: str,
    target_session: str,
    source_id: str | None,
    turn: int | None,
    message: str,
    timestamp: float,
    delivered: int = 1,
) -> int:
    """Insert a crosstalk message and return the row id.

    `delivered` is 1 for a message pasted into a live tmux target (the historical
    case, still the default) and 0 for one queued for a non-session participant
    (e.g. a ChatGPT chat) that will collect it later. A queued row flips to 1 when
    its owner collects it.
    """
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO crosstalk_messages"
        " (sender_session, sender_label, target_session, source_id, turn, message,"
        " timestamp, delivered)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (sender_session, sender_label, target_session, source_id, turn, message,
         timestamp, delivered),
    )
    conn.commit()
    return cur.lastrowid


def collect_inbox(target_session: str, limit: int = 100) -> list[dict]:
    """Return the undelivered (queued) messages for a non-session participant in
    arrival order and mark them delivered, in one transaction. Idempotent: a
    repeat call returns nothing new because the rows are now delivered. The rows
    are kept (one copy, still visible in the log) — collection sets the flag, it
    does not delete."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM crosstalk_messages"
        " WHERE target_session = ? AND delivered = 0 ORDER BY id ASC LIMIT ?",
        (target_session, limit),
    ).fetchall()
    if rows:
        ids = [r["id"] for r in rows]
        conn.execute(
            "UPDATE crosstalk_messages SET delivered = 1 WHERE id IN (%s)"
            % ",".join("?" * len(ids)),
            ids,
        )
        conn.commit()
    return [dict(r) for r in rows]


def get_messages(
    limit: int = 50,
    since: float | None = None,
    session: str | None = None,
) -> list[dict]:
    """Query recent crosstalk messages.

    Args:
        limit: Max messages to return.
        since: Unix epoch — only messages after this time.
        session: Filter to messages sent by or to this session.
    """
    conn = get_conn()
    query = "SELECT * FROM crosstalk_messages WHERE 1=1"
    params: list = []
    if since is not None:
        query += " AND timestamp >= ?"
        params.append(since)
    if session:
        query += " AND (sender_session = ? OR target_session = ?)"
        params.extend([session, session])
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]
