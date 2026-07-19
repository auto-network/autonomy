"""Durable, revocable sessions for the human dashboard unlock gate.

The HMAC cookie proves that the dashboard minted a token; this store decides
whether that token is still live.  Session history is personal, local security
state.  It does not belong in an organization graph, authority ledger, or the
auto.network registry.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "dashboard_identity_sessions.db"
TOUCH_INTERVAL_S = 60
_SCHEMA_USER_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS identity_sessions (
    sid             TEXT PRIMARY KEY,
    method          TEXT NOT NULL,
    credential_id   TEXT,
    created_at      INTEGER NOT NULL,
    last_activity   REAL NOT NULL,
    user_agent      TEXT,
    source_ip       TEXT,
    status          TEXT NOT NULL,
    end_reason      TEXT,
    ended_at        REAL,
    expires_at      INTEGER NOT NULL,
    grantee         TEXT,
    scope_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_identity_sessions_credential
    ON identity_sessions(credential_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_identity_sessions_status
    ON identity_sessions(status, expires_at);
"""


class SessionStoreError(RuntimeError):
    """The durable session decision could not be read or written."""


_pool_lock = threading.Lock()
_connections: dict[str, tuple[sqlite3.Connection, threading.RLock]] = {}


def db_path() -> Path:
    return Path(os.environ.get(
        "DASHBOARD_IDENTITY_SESSION_DB", str(DEFAULT_DB_PATH)
    ))


def _open_pooled(path: Path) -> tuple[sqlite3.Connection, threading.RLock]:
    key = str(path.resolve())
    pooled = _connections.get(key)
    if pooled is not None:
        return pooled
    with _pool_lock:
        pooled = _connections.get(key)
        if pooled is not None:
            return pooled
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA journal_mode=WAL")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    conn.executescript(SCHEMA)
                    conn.execute(f"PRAGMA user_version = {_SCHEMA_USER_VERSION}")
                    conn.commit()
                elif version != _SCHEMA_USER_VERSION:
                    raise SessionStoreError(
                        "unsupported dashboard session-store schema version "
                        f"{version}; expected {_SCHEMA_USER_VERSION}"
                    )
            except Exception:
                conn.close()
                raise
        except SessionStoreError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise SessionStoreError(f"could not initialize session store: {exc}") from exc
        pooled = (conn, threading.RLock())
        _connections[key] = pooled
        return pooled


@contextmanager
def _connect():
    """Serialize access to the persistent connection for the active DB path."""
    conn, lock = _open_pooled(db_path())
    with lock:
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _scope_json(scope: Any) -> str | None:
    if scope is None:
        return None
    try:
        return json.dumps(scope, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("session scope must be JSON-serializable") from exc


def _row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    raw_scope = result.pop("scope_json", None)
    result["scope"] = json.loads(raw_scope) if raw_scope is not None else None
    return result


def _validate_text(value: str | None, field: str, *, required: bool = False,
                   limit: int = 1024) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value) or len(value) > limit:
        qualifier = "non-empty " if required else ""
        raise ValueError(f"{field} must be a {qualifier}string up to {limit} characters")
    return value


def create_session(*, sid: str, method: str, created_at: int,
                   expires_at: int, last_activity: float,
                   credential_id: str | None = None,
                   user_agent: str | None = None,
                   source_ip: str | None = None,
                   grantee: str | None = None,
                   scope: Any = None) -> dict:
    """Persist an active session before its cookie is issued."""
    sid = _validate_text(sid, "sid", required=True, limit=128)  # type: ignore[assignment]
    method = _validate_text(method, "method", required=True, limit=64)  # type: ignore[assignment]
    credential_id = _validate_text(credential_id, "credential_id", limit=2048)
    user_agent = _validate_text(user_agent, "user_agent", limit=1024)
    source_ip = _validate_text(source_ip, "source_ip", limit=128)
    grantee = _validate_text(grantee, "grantee", limit=256)
    if not isinstance(created_at, int) or not isinstance(expires_at, int) \
            or isinstance(created_at, bool) or isinstance(expires_at, bool) \
            or expires_at <= created_at:
        raise ValueError("session timestamps must be integer seconds with expiry after creation")
    if not isinstance(last_activity, (int, float)) or isinstance(last_activity, bool):
        raise ValueError("last_activity must be a timestamp")
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO identity_sessions (
                       sid, method, credential_id, created_at, last_activity,
                       user_agent, source_ip, status, end_reason, ended_at,
                       expires_at, grantee, scope_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', NULL, NULL, ?, ?, ?)""",
                (sid, method, credential_id, created_at, float(last_activity),
                 user_agent, source_ip, expires_at, grantee, _scope_json(scope)),
            )
    except sqlite3.Error as exc:
        raise SessionStoreError(f"could not create dashboard session: {exc}") from exc
    row = get_session(sid, now=float(created_at))
    if row is None:  # pragma: no cover - defensive against a broken SQLite driver
        raise SessionStoreError("created dashboard session could not be read back")
    return row


def _expire_active(conn: sqlite3.Connection, now: float,
                   *, credential_id: str | None = None) -> None:
    where = "status = 'active' AND expires_at <= ?"
    params: list[Any] = [now]
    if credential_id is not None:
        where += " AND credential_id = ?"
        params.append(credential_id)
    conn.execute(
        f"""UPDATE identity_sessions
               SET status = 'expired', end_reason = 'expired', ended_at = ?
             WHERE {where}""",
        [now, *params],
    )


def get_session(sid: str, *, now: float) -> dict | None:
    """Return a session row, lazily recording expiry in its history."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM identity_sessions WHERE sid = ?", (sid,)
            ).fetchone()
            result = _row_dict(row)
            if result is not None and result["status"] == "active" \
                    and result["expires_at"] <= now:
                conn.execute(
                    """UPDATE identity_sessions
                          SET status = 'expired', end_reason = 'expired', ended_at = ?
                        WHERE sid = ? AND status = 'active'""",
                    (now, sid),
                )
                result.update({
                    "status": "expired", "end_reason": "expired", "ended_at": now,
                })
    except sqlite3.Error as exc:
        raise SessionStoreError(f"could not read dashboard session: {exc}") from exc
    return result


def check_active(*, sid: str, method: str, created_at: int,
                 expires_at: int, now: float) -> bool:
    """Validate a signed token against its durable row and touch activity.

    All signed provenance is compared with the row so the cookie and store
    cannot silently disagree.  A missing row is deliberately invalid: this is
    what retires the old stateless-cookie behavior.
    """
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM identity_sessions WHERE sid = ?", (sid,)
            ).fetchone()
            if row is None:
                return False
            if row["status"] == "active" and row["expires_at"] <= now:
                conn.execute(
                    """UPDATE identity_sessions
                          SET status = 'expired', end_reason = 'expired', ended_at = ?
                        WHERE sid = ? AND status = 'active'""",
                    (now, sid),
                )
                return False
            if row["status"] != "active" \
                    or row["method"] != method \
                    or row["created_at"] != created_at \
                    or row["expires_at"] != expires_at:
                return False
            if float(row["last_activity"]) <= now - TOUCH_INTERVAL_S:
                conn.execute(
                    "UPDATE identity_sessions SET last_activity = ? WHERE sid = ?",
                    (now, sid),
                )
            return True
    except sqlite3.Error as exc:
        raise SessionStoreError(f"could not verify dashboard session: {exc}") from exc


def end_session(sid: str, *, reason: str = "locked", now: float) -> bool:
    """End exactly one active session while retaining its history row."""
    reason = _validate_text(reason, "reason", required=True, limit=128)  # type: ignore[assignment]
    try:
        with _connect() as conn:
            _expire_active(conn, now)
            cur = conn.execute(
                """UPDATE identity_sessions
                      SET status = 'ended', end_reason = ?, ended_at = ?
                    WHERE sid = ? AND status = 'active'""",
                (reason, now, sid),
            )
            return cur.rowcount == 1
    except sqlite3.Error as exc:
        raise SessionStoreError(f"could not end dashboard session: {exc}") from exc


def revoke_session(sid: str, *, reason: str = "revoked", now: float) -> bool:
    """Revoke one active session while retaining its history row."""
    reason = _validate_text(reason, "reason", required=True, limit=128)  # type: ignore[assignment]
    try:
        with _connect() as conn:
            _expire_active(conn, now)
            cur = conn.execute(
                """UPDATE identity_sessions
                      SET status = 'revoked', end_reason = ?, ended_at = ?
                    WHERE sid = ? AND status = 'active'""",
                (reason, now, sid),
            )
            return cur.rowcount == 1
    except sqlite3.Error as exc:
        raise SessionStoreError(f"could not revoke dashboard session: {exc}") from exc


def revoke_credential_sessions(credential_id: str, *, now: float,
                               reason: str = "passkey_revoked") -> int:
    """Revoke every active session established through one passkey."""
    credential_id = _validate_text(
        credential_id, "credential_id", required=True, limit=2048
    )  # type: ignore[assignment]
    reason = _validate_text(reason, "reason", required=True, limit=128)  # type: ignore[assignment]
    try:
        with _connect() as conn:
            _expire_active(conn, now, credential_id=credential_id)
            cur = conn.execute(
                """UPDATE identity_sessions
                      SET status = 'revoked', end_reason = ?, ended_at = ?
                    WHERE credential_id = ? AND status = 'active'""",
                (reason, now, credential_id),
            )
            return cur.rowcount
    except sqlite3.Error as exc:
        raise SessionStoreError(f"could not revoke passkey sessions: {exc}") from exc


def sessions_for_credential(credential_id: str, *, now: float,
                            recent_ended: int = 10) -> dict[str, list[dict]]:
    """All active and the latest ended sessions for a passkey-centric UI."""
    credential_id = _validate_text(
        credential_id, "credential_id", required=True, limit=2048
    )  # type: ignore[assignment]
    if not isinstance(recent_ended, int) or isinstance(recent_ended, bool) \
            or recent_ended < 0 or recent_ended > 100:
        raise ValueError("recent_ended must be an integer from 0 to 100")
    try:
        with _connect() as conn:
            _expire_active(conn, now, credential_id=credential_id)
            active = conn.execute(
                """SELECT * FROM identity_sessions
                    WHERE credential_id = ? AND status = 'active'
                    ORDER BY last_activity DESC, created_at DESC""",
                (credential_id,),
            ).fetchall()
            ended = conn.execute(
                """SELECT * FROM identity_sessions
                    WHERE credential_id = ? AND status != 'active'
                    ORDER BY ended_at DESC, created_at DESC LIMIT ?""",
                (credential_id, recent_ended),
            ).fetchall()
    except sqlite3.Error as exc:
        raise SessionStoreError(f"could not list passkey sessions: {exc}") from exc
    return {
        "active": [_row_dict(row) for row in active],
        "recent_ended": [_row_dict(row) for row in ended],
    }


def reset_for_tests() -> None:
    """Close pooled connections before a test removes its temporary DB."""
    with _pool_lock:
        for conn, _lock in _connections.values():
            conn.close()
        _connections.clear()
