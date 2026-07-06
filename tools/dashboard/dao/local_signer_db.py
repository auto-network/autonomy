"""Local signer operational store — device pairing, signing-request auth.

Lives in the same operational store as ``commit_workflow_*``
(``tools/dashboard/dao/commit_workflow_db.py``) per DN3 (graph note
``a498f525-6b0``): this is a separate capability module, not a separate
database. Device identity here is a NEW, separate authenticator — never
the agent-session token model in ``auth_db.py``.

Three tables:

- ``local_signer_devices`` — one row per paired operator device. Public
  key only; the private half never leaves the device.
- ``local_signer_pairing_requests`` — one row per in-flight pairing
  ceremony. ``device_code`` is the human-enterable fallback; ``verifier``
  is a separate high-entropy secret embedded only in the QR payload —
  only its hash is ever stored.
- ``local_signer_audit_events`` — append-only, mirrors
  ``commit_workflow_events``'s reject-UPDATE/DELETE trigger pattern, for
  device/key lifecycle events that have no ``workflow_id`` yet.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .commit_workflow_db import DB_PATH, MIN_SQLITE_VERSION

PAIRING_STATUSES = (
    "pending",
    "awaiting_operator_confirm",
    "completed",
    "denied",
    "expired",
    "consumed",
)

AUDIT_EVENT_TYPES = (
    "pairing_started",
    "pairing_completed",
    "pairing_expired",
    "pairing_replay_rejected",
    "key_provisioned",
    "key_provision_rejected_weak_kdf",
    "device_revoked",
    "signature_submitted",
    "signature_rejected_hash_mismatch",
    "signature_rejected_revoked_device",
    "signature_rejected_stale_nonce",
)

_STATUS_SQL = ", ".join(f"'{s}'" for s in PAIRING_STATUSES)
_EVENT_TYPE_SQL = ", ".join(f"'{t}'" for t in AUDIT_EVENT_TYPES)

CREATE_TABLES = f"""\
CREATE TABLE IF NOT EXISTS local_signer_devices (
    device_id           TEXT PRIMARY KEY,
    operator_id         TEXT NOT NULL,
    device_label        TEXT NOT NULL,
    public_key          TEXT NOT NULL,
    platform            TEXT NOT NULL,
    app_version         TEXT,
    paired_at           REAL NOT NULL,
    last_seen_at        REAL,
    revoked_at          REAL,
    revoked_reason      TEXT,
    pairing_ip_hash     TEXT
);

CREATE TABLE IF NOT EXISTS local_signer_pairing_requests (
    pairing_id          TEXT PRIMARY KEY,
    device_code         TEXT NOT NULL UNIQUE,
    verifier_hash       TEXT NOT NULL,
    operator_id         TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ({_STATUS_SQL})),
    created_at          REAL NOT NULL,
    expires_at          REAL NOT NULL,
    pending_public_key  TEXT,
    pending_device_meta TEXT,
    verifier_presented  INTEGER,
    completed_device_id TEXT
);

CREATE TABLE IF NOT EXISTS local_signer_audit_events (
    audit_event_id      TEXT PRIMARY KEY,
    occurred_at         REAL NOT NULL,
    event_type          TEXT NOT NULL CHECK (event_type IN ({_EVENT_TYPE_SQL})),
    device_id           TEXT,
    operator_id         TEXT,
    signing_request_id  TEXT,
    kdf_summary_json    TEXT,
    reason              TEXT
);
"""

CREATE_INDEXES = """\
CREATE INDEX IF NOT EXISTS idx_lsd_operator
    ON local_signer_devices(operator_id, revoked_at);

CREATE INDEX IF NOT EXISTS idx_lspr_device_code
    ON local_signer_pairing_requests(device_code);

CREATE INDEX IF NOT EXISTS idx_lspr_operator_status
    ON local_signer_pairing_requests(operator_id, status);

CREATE INDEX IF NOT EXISTS idx_lsae_device_time
    ON local_signer_audit_events(device_id, occurred_at);

CREATE INDEX IF NOT EXISTS idx_lsae_signing_request
    ON local_signer_audit_events(signing_request_id);
"""

CREATE_TRIGGERS = """\
CREATE TRIGGER IF NOT EXISTS trg_lsae_no_update
BEFORE UPDATE ON local_signer_audit_events
BEGIN
    SELECT RAISE(ABORT, 'local_signer_audit_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_lsae_no_delete
BEFORE DELETE ON local_signer_audit_events
BEGIN
    SELECT RAISE(ABORT, 'local_signer_audit_events is append-only');
END;
"""


@dataclass(frozen=True)
class LocalSignerDevice:
    device_id: str
    operator_id: str
    device_label: str
    public_key: str
    platform: str
    app_version: str | None
    paired_at: float
    last_seen_at: float | None
    revoked_at: float | None
    revoked_reason: str | None
    pairing_ip_hash: str | None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


@dataclass(frozen=True)
class PairingRequest:
    pairing_id: str
    device_code: str
    verifier_hash: str
    operator_id: str
    status: str
    created_at: float
    expires_at: float
    pending_public_key: str | None
    pending_device_meta: str | None
    verifier_presented: int | None
    completed_device_id: str | None


def _db_path(db_path: Path | str | None = None) -> Path:
    return Path(db_path) if db_path is not None else DB_PATH


def _check_sqlite_version() -> None:
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        got = ".".join(str(part) for part in sqlite3.sqlite_version_info)
        need = ".".join(str(part) for part in MIN_SQLITE_VERSION)
        raise RuntimeError(
            f"local signer store requires SQLite >= {need}; got {got}"
        )


def _get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    _check_sqlite_version()
    path = _db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA recursive_triggers=ON")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    conn = _get_conn(db_path)
    try:
        init_schema_on_connection(conn)
    finally:
        conn.close()


def init_schema_on_connection(conn: sqlite3.Connection) -> None:
    """Ensure schema exists on an already configured connection."""
    conn.executescript(CREATE_TABLES)
    conn.executescript(CREATE_INDEXES)
    conn.executescript(CREATE_TRIGGERS)
    conn.commit()


# ── Devices ──────────────────────────────────────────────────────────


def insert_device(
    *,
    device_id: str,
    operator_id: str,
    device_label: str,
    public_key: str,
    platform: str,
    paired_at: float,
    app_version: str | None = None,
    pairing_ip_hash: str | None = None,
    db_path: Path | str | None = None,
) -> None:
    conn = _get_conn(db_path)
    try:
        conn.execute(
            """INSERT INTO local_signer_devices (
                   device_id, operator_id, device_label, public_key, platform,
                   app_version, paired_at, pairing_ip_hash
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                device_id, operator_id, device_label, public_key, platform,
                app_version, paired_at, pairing_ip_hash,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_device(device_id: str, db_path: Path | str | None = None) -> LocalSignerDevice | None:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM local_signer_devices WHERE device_id = ?", (device_id,),
        ).fetchone()
        return LocalSignerDevice(**dict(row)) if row else None
    finally:
        conn.close()


def revoke_device(
    device_id: str, *, revoked_at: float, reason: str,
    db_path: Path | str | None = None,
) -> bool:
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            """UPDATE local_signer_devices SET revoked_at = ?, revoked_reason = ?
               WHERE device_id = ? AND revoked_at IS NULL""",
            (revoked_at, reason, device_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── Pairing requests ─────────────────────────────────────────────────


def insert_pairing_request(
    *,
    pairing_id: str,
    device_code: str,
    verifier_hash: str,
    operator_id: str,
    created_at: float,
    expires_at: float,
    db_path: Path | str | None = None,
) -> None:
    conn = _get_conn(db_path)
    try:
        conn.execute(
            """INSERT INTO local_signer_pairing_requests (
                   pairing_id, device_code, verifier_hash, operator_id,
                   status, created_at, expires_at
               ) VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
            (pairing_id, device_code, verifier_hash, operator_id, created_at, expires_at),
        )
        conn.commit()
    finally:
        conn.close()


def get_pairing_request(
    pairing_id: str, db_path: Path | str | None = None,
) -> PairingRequest | None:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM local_signer_pairing_requests WHERE pairing_id = ?",
            (pairing_id,),
        ).fetchone()
        return PairingRequest(**dict(row)) if row else None
    finally:
        conn.close()


def get_pairing_request_by_code(
    device_code: str, db_path: Path | str | None = None,
) -> PairingRequest | None:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM local_signer_pairing_requests WHERE device_code = ?",
            (device_code,),
        ).fetchone()
        return PairingRequest(**dict(row)) if row else None
    finally:
        conn.close()


def complete_pairing_to_awaiting_confirm(
    *,
    device_code: str,
    pending_public_key: str,
    pending_device_meta: str,
    verifier_presented: bool,
    now: float,
    db_path: Path | str | None = None,
) -> str | None:
    """Atomically flip a ``pending``, unexpired row to ``awaiting_operator_confirm``.

    Returns the ``pairing_id`` on success, ``None`` if the row is missing,
    not ``pending``, or expired (caller distinguishes these by re-reading
    the row for the exact rejection reason). Uses ``BEGIN IMMEDIATE`` so a
    concurrent race on the same code resolves to exactly one winner.
    """
    conn = _get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM local_signer_pairing_requests WHERE device_code = ?",
            (device_code,),
        ).fetchone()
        if row is None or row["status"] != "pending" or row["expires_at"] <= now:
            conn.rollback()
            return None
        conn.execute(
            """UPDATE local_signer_pairing_requests
               SET status = 'awaiting_operator_confirm',
                   pending_public_key = ?,
                   pending_device_meta = ?,
                   verifier_presented = ?
               WHERE pairing_id = ? AND status = 'pending'""",
            (
                pending_public_key, pending_device_meta,
                1 if verifier_presented else 0, row["pairing_id"],
            ),
        )
        conn.commit()
        return row["pairing_id"]
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def decide_pairing(
    *,
    pairing_id: str,
    approve: bool,
    device_id: str | None,
    now: float,
    db_path: Path | str | None = None,
) -> bool:
    """Terminal decision on an ``awaiting_operator_confirm`` row.

    ``device_id`` is only used (and required) when ``approve=True``; the
    caller is responsible for having already inserted the device row via
    :func:`insert_device` in the same operator-authenticated request.
    Returns ``False`` (no-op) if the row is not currently
    ``awaiting_operator_confirm`` — deciding a terminal pairing twice must
    not succeed silently.
    """
    conn = _get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM local_signer_pairing_requests WHERE pairing_id = ?",
            (pairing_id,),
        ).fetchone()
        if row is None or row["status"] != "awaiting_operator_confirm":
            conn.rollback()
            return False
        if approve:
            conn.execute(
                """UPDATE local_signer_pairing_requests
                   SET status = 'completed', completed_device_id = ?
                   WHERE pairing_id = ?""",
                (device_id, pairing_id),
            )
        else:
            conn.execute(
                "UPDATE local_signer_pairing_requests SET status = 'denied' WHERE pairing_id = ?",
                (pairing_id,),
            )
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def expire_stale_pairing_requests(
    *, now: float, db_path: Path | str | None = None,
) -> int:
    """Flip any ``awaiting_operator_confirm`` or ``pending`` row past its
    TTL to ``expired``. Returns the number of rows changed."""
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            """UPDATE local_signer_pairing_requests
               SET status = 'expired'
               WHERE status IN ('pending', 'awaiting_operator_confirm')
                 AND expires_at <= ?""",
            (now,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ── Audit events ─────────────────────────────────────────────────────


def append_audit_event(
    *,
    audit_event_id: str,
    occurred_at: float,
    event_type: str,
    device_id: str | None = None,
    operator_id: str | None = None,
    signing_request_id: str | None = None,
    kdf_summary_json: str | None = None,
    reason: str | None = None,
    db_path: Path | str | None = None,
) -> None:
    if event_type not in AUDIT_EVENT_TYPES:
        raise ValueError(f"invalid local signer audit event_type: {event_type!r}")
    conn = _get_conn(db_path)
    try:
        conn.execute(
            """INSERT INTO local_signer_audit_events (
                   audit_event_id, occurred_at, event_type, device_id,
                   operator_id, signing_request_id, kdf_summary_json, reason
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                audit_event_id, occurred_at, event_type, device_id,
                operator_id, signing_request_id, kdf_summary_json, reason,
            ),
        )
        conn.commit()
    finally:
        conn.close()
