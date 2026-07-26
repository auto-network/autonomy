"""Restart-safe local progress for invitation joins.

This store is deliberately a locator, not a secret store.  It persists only
the public organization/invitation/persona identifiers, the deterministic
claim key, approval counts, and timestamps.  The bearer remains in the
user-carried invitation and the personal root remains in its encrypted armor.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

from tools.data_paths import resolve_store

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_joins (
    invite_ref   TEXT PRIMARY KEY,
    org          TEXT NOT NULL,
    persona_pub  TEXT NOT NULL,
    claim_key    TEXT NOT NULL UNIQUE,
    have         INTEGER NOT NULL,
    need         INTEGER NOT NULL,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_joins_persona
    ON pending_joins(persona_pub, updated_at);
"""


class PendingJoinStoreError(RuntimeError):
    """The local resume locator could not be read or updated."""


def db_path() -> Path:
    return resolve_store("pending_joins")


def _require_hash(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise ValueError(f"{name} must be 64 lowercase hex chars")
    return value


def claim_key(invite_ref: str, persona_pub: str) -> str:
    """The ledger's deterministic pending-claim locator."""
    _require_hash(invite_ref, "invite_ref")
    _require_hash(persona_pub, "persona_pub")
    return hashlib.sha256((invite_ref + persona_pub).encode("ascii")).hexdigest()


def init_db(path: Path | str | None = None) -> None:
    target = Path(path) if path is not None else db_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(target, timeout=5) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                conn.executescript(SCHEMA)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise PendingJoinStoreError(
                    f"unsupported pending-join schema v{version}; "
                    f"expected v{SCHEMA_VERSION}"
                )
    except PendingJoinStoreError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise PendingJoinStoreError(
            f"could not initialize pending-join store: {exc}"
        ) from exc


def _connect(path: Path | str | None = None) -> sqlite3.Connection:
    target = Path(path) if path is not None else db_path()
    init_db(target)
    conn = sqlite3.connect(target, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def save(
    *,
    org: str,
    invite_ref: str,
    persona_pub: str,
    have: int,
    need: int,
    path: Path | str | None = None,
    now: int | None = None,
) -> dict:
    """Insert/update one pending join without ever extending its identity."""
    if not isinstance(org, str) or not org:
        raise ValueError("org must be a non-empty string")
    key = claim_key(invite_ref, persona_pub)
    if (
        not isinstance(have, int)
        or isinstance(have, bool)
        or not isinstance(need, int)
        or isinstance(need, bool)
        or have < 0
        or need < 1
        or have > need
    ):
        raise ValueError("have/need must satisfy 0 <= have <= need and need >= 1")
    timestamp = int(time.time() * 1000) if now is None else int(now)
    try:
        with _connect(path) as conn:
            conn.execute(
                """
                INSERT INTO pending_joins(
                    invite_ref, org, persona_pub, claim_key, have, need,
                    created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(invite_ref) DO UPDATE SET
                    org=excluded.org,
                    persona_pub=excluded.persona_pub,
                    claim_key=excluded.claim_key,
                    have=excluded.have,
                    need=excluded.need,
                    updated_at=excluded.updated_at
                """,
                (
                    invite_ref, org, persona_pub, key, have, need,
                    timestamp, timestamp,
                ),
            )
    except sqlite3.Error as exc:
        raise PendingJoinStoreError(f"could not save pending join: {exc}") from exc
    return get(invite_ref, path=path)


def get(invite_ref: str, *, path: Path | str | None = None) -> dict | None:
    _require_hash(invite_ref, "invite_ref")
    try:
        with _connect(path) as conn:
            row = conn.execute(
                "SELECT org, invite_ref, persona_pub, claim_key, have, need, "
                "created_at, updated_at FROM pending_joins WHERE invite_ref = ?",
                (invite_ref,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise PendingJoinStoreError(f"could not read pending join: {exc}") from exc
    return dict(row) if row is not None else None


def list_pending(*, path: Path | str | None = None) -> list[dict]:
    try:
        with _connect(path) as conn:
            rows = conn.execute(
                "SELECT org, invite_ref, persona_pub, claim_key, have, need, "
                "created_at, updated_at FROM pending_joins "
                "ORDER BY created_at, invite_ref"
            ).fetchall()
    except sqlite3.Error as exc:
        raise PendingJoinStoreError(f"could not list pending joins: {exc}") from exc
    return [dict(row) for row in rows]


def delete(invite_ref: str, *, path: Path | str | None = None) -> bool:
    _require_hash(invite_ref, "invite_ref")
    try:
        with _connect(path) as conn:
            cursor = conn.execute(
                "DELETE FROM pending_joins WHERE invite_ref = ?",
                (invite_ref,),
            )
            return cursor.rowcount > 0
    except sqlite3.Error as exc:
        raise PendingJoinStoreError(f"could not delete pending join: {exc}") from exc
