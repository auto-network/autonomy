"""Browser lease records (auto-czoc0; design graph://c330323d-986).

One row per lease in the dashboard database, keyed by the SHA-256 of the lease
identifier (the identifier itself is never stored). Rows hold the owner, the
state and lock holder, timestamps, the container's name and address, and the
per-lease secret and VNC password encrypted at rest.

Epoch fencing. Each dashboard worker takes a new epoch when it activates
(:func:`take_epoch`). Every write names the writer's epoch and succeeds only
while that epoch is still the current one, so a replaced worker's writes are
refused, and adoption (:func:`adopt`) moves a row to the new epoch.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from tools.dashboard.dao import dashboard_db

ACTIVE_STATES = ("requested", "starting", "ready", "busy", "locked", "unhealthy", "releasing")
FINAL_STATES = ("gone", "failed")

#: The design's state machine ("Lease states"), plus the failed path out of
#: requested (the container could not be created).
TRANSITIONS: dict[str, frozenset] = {
    "requested": frozenset({"starting", "failed"}),
    "starting": frozenset({"ready", "failed", "releasing"}),
    "ready": frozenset({"busy", "locked", "unhealthy", "releasing"}),
    "busy": frozenset({"ready", "locked", "releasing"}),
    "locked": frozenset({"ready", "releasing"}),
    "unhealthy": frozenset({"releasing"}),
    "releasing": frozenset({"gone"}),
    "failed": frozenset({"gone"}),
    "gone": frozenset(),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS browser_broker_epoch (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    epoch INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS browser_leases (
    lease_hash      TEXT PRIMARY KEY,
    session         TEXT NOT NULL,
    org             TEXT NOT NULL,
    workspace       TEXT NOT NULL,
    profile_kind    TEXT NOT NULL,
    profile_name    TEXT,
    adapter         TEXT NOT NULL,
    container_name  TEXT NOT NULL,
    address         TEXT,
    state           TEXT NOT NULL,
    lock_holder     TEXT,
    epoch           INTEGER NOT NULL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    last_activity   REAL NOT NULL,
    expires_at      REAL NOT NULL,
    health_failures INTEGER NOT NULL DEFAULT 0,
    last_health_at  REAL,
    diagnostic      TEXT,
    secret_enc      BLOB NOT NULL,
    vnc_enc         BLOB NOT NULL,
    audit           TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS browser_leases_state ON browser_leases(state);
"""

_ready_path: Optional[str] = None


def _conn() -> sqlite3.Connection:
    global _ready_path
    conn = dashboard_db.get_conn()
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    if _ready_path != path:
        conn.executescript(_SCHEMA)
        conn.commit()
        _ready_path = path
    return conn


class StaleEpoch(RuntimeError):
    """The writer's epoch is no longer current; a newer worker owns leases."""


class AdmissionRefused(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ── identifiers and secrets ────────────────────────────────────────────


def new_lease_id() -> str:
    return "brl_" + secrets.token_hex(16)


def lease_hash(lease_id: str) -> str:
    return hashlib.sha256(lease_id.encode()).hexdigest()


#: VNC authentication uses only 8 characters; draw them from all printable
#: ASCII except space (94 symbols, about 52 bits).
_VNC_ALPHABET = "".join(chr(c) for c in range(33, 127))


def new_vnc_password() -> str:
    return "".join(secrets.choice(_VNC_ALPHABET) for _ in range(8))


def new_lease_secret() -> str:
    return secrets.token_hex(32)


def _aead():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    from tools.dashboard.unlock_routes import _session_secret

    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b"autonomy browser lease secrets v1").derive(_session_secret())
    return AESGCM(key)


def seal(value: str, lease_hash_: str) -> bytes:
    nonce = os.urandom(12)
    return nonce + _aead().encrypt(nonce, value.encode(), lease_hash_.encode())


def unseal(blob: bytes, lease_hash_: str) -> str:
    return _aead().decrypt(blob[:12], blob[12:], lease_hash_.encode()).decode()


# ── epochs ─────────────────────────────────────────────────────────────


def take_epoch() -> int:
    """A new epoch for an activating worker; every older epoch is fenced off."""
    conn = _conn()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT epoch FROM browser_broker_epoch WHERE id = 1").fetchone()
        epoch = (row[0] if row else 0) + 1
        conn.execute("INSERT INTO browser_broker_epoch (id, epoch) VALUES (1, ?) "
                     "ON CONFLICT(id) DO UPDATE SET epoch = excluded.epoch", (epoch,))
    return epoch


def current_epoch() -> int:
    row = _conn().execute("SELECT epoch FROM browser_broker_epoch WHERE id = 1").fetchone()
    return row[0] if row else 0


_EPOCH_GUARD = "(SELECT epoch FROM browser_broker_epoch WHERE id = 1) = :epoch"


# ── rows ───────────────────────────────────────────────────────────────


@dataclass
class Lease:
    lease_hash: str
    session: str
    org: str
    workspace: str
    profile_kind: str
    profile_name: Optional[str]
    adapter: str
    container_name: str
    address: Optional[str]
    state: str
    lock_holder: Optional[str]
    epoch: int
    created_at: float
    updated_at: float
    last_activity: float
    expires_at: float
    health_failures: int
    last_health_at: Optional[float]
    diagnostic: Optional[str]
    secret_enc: bytes
    vnc_enc: bytes
    audit: str

    @property
    def secret(self) -> str:
        return unseal(self.secret_enc, self.lease_hash)

    @property
    def vnc_password(self) -> str:
        return unseal(self.vnc_enc, self.lease_hash)


_COLUMNS = [f for f in Lease.__dataclass_fields__]


def _row(values) -> Lease:
    return Lease(**dict(zip(_COLUMNS, values)))


def get(lease_hash_: str) -> Optional[Lease]:
    row = _conn().execute(
        f"SELECT {', '.join(_COLUMNS)} FROM browser_leases WHERE lease_hash = ?",
        (lease_hash_,)).fetchone()
    return _row(row) if row else None


def list_leases(states: Iterable[str] = ACTIVE_STATES) -> list[Lease]:
    states = tuple(states)
    marks = ", ".join("?" * len(states))
    rows = _conn().execute(
        f"SELECT {', '.join(_COLUMNS)} FROM browser_leases WHERE state IN ({marks}) "
        "ORDER BY created_at", states).fetchall()
    return [_row(r) for r in rows]


def admit(*, epoch: int, max_leases: int, lease_hash_: str, session: str, org: str,
          workspace: str, profile_kind: str, profile_name: Optional[str], adapter: str,
          container_name: str, expires_at: float, secret: str, vnc_password: str) -> None:
    """Insert a ``requested`` row if fewer than *max_leases* are active.

    One IMMEDIATE transaction, so concurrent requests (from any worker) are
    counted one at a time and the node never exceeds its lease count.
    """
    now = time.time()
    secret_enc = seal(secret, lease_hash_)
    vnc_enc = seal(vnc_password, lease_hash_)
    conn = _conn()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if current_epoch() != epoch:
            raise StaleEpoch()
        marks = ", ".join("?" * len(ACTIVE_STATES))
        active = conn.execute(
            f"SELECT COUNT(*) FROM browser_leases WHERE state IN ({marks})",
            ACTIVE_STATES).fetchone()[0]
        if active >= max_leases:
            raise AdmissionRefused("lease-count")
        conn.execute(
            "INSERT INTO browser_leases (lease_hash, session, org, workspace, profile_kind, "
            "profile_name, adapter, container_name, state, epoch, created_at, updated_at, "
            "last_activity, expires_at, secret_enc, vnc_enc, audit) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, 'requested', ?, ?, ?, ?, ?, ?, ?, ?)",
            (lease_hash_, session, org, workspace, profile_kind, profile_name, adapter,
             container_name, epoch, now, now, now, expires_at, secret_enc, vnc_enc,
             json.dumps([{"op": "request", "at": now, "result": "ok"}])))


def transition(lease_hash_: str, *, epoch: int, to: str, expect: Iterable[str] = (),
               audit_op: Optional[str] = None, result: str = "ok", **fields) -> bool:
    """Compare-and-set a row into state *to*.

    Succeeds only while *epoch* is current and the row's state is one of
    *expect* (default: every state allowed to move to *to*). Extra *fields*
    are written in the same statement. Returns whether the row changed.
    """
    sources = tuple(expect) or tuple(s for s, targets in TRANSITIONS.items() if to in targets)
    if not sources:
        return False
    for source in sources:
        if to not in TRANSITIONS[source] and to != source:
            raise ValueError(f"illegal lease transition {source} -> {to}")
    now = time.time()
    sets = ["state = :to", "updated_at = :now", "epoch = :epoch"]
    params = {"to": to, "now": now, "epoch": epoch, "lease_hash": lease_hash_}
    for name, value in fields.items():
        if name not in _COLUMNS or name in {"lease_hash", "state", "epoch", "audit"}:
            raise ValueError(f"not a writable lease field: {name}")
        sets.append(f"{name} = :{name}")
        params[name] = value
    if audit_op:
        sets.append("audit = json_insert(audit, '$[#]', json_object('op', :op, 'at', :now, "
                    "'result', :result))")
        params.update(op=audit_op, result=result)
    marks = ", ".join(f":s{i}" for i in range(len(sources)))
    params.update({f"s{i}": s for i, s in enumerate(sources)})
    conn = _conn()
    with conn:
        cursor = conn.execute(
            f"UPDATE browser_leases SET {', '.join(sets)} WHERE lease_hash = :lease_hash "
            f"AND state IN ({marks}) AND {_EPOCH_GUARD}", params)
    return cursor.rowcount == 1


def update(lease_hash_: str, *, epoch: int, **fields) -> bool:
    """Epoch-fenced write of non-state fields (address, health, activity)."""
    lease = get(lease_hash_)
    if lease is None:
        return False
    return transition(lease_hash_, epoch=epoch, to=lease.state, expect=(lease.state,), **fields)


def adopt(lease_hash_: str, *, epoch: int) -> bool:
    """Move an active row to *epoch* (the adopting worker's)."""
    marks = ", ".join(f":s{i}" for i in range(len(ACTIVE_STATES)))
    params = {"epoch": epoch, "lease_hash": lease_hash_, "now": time.time(),
              **{f"s{i}": s for i, s in enumerate(ACTIVE_STATES)}}
    conn = _conn()
    with conn:
        cursor = conn.execute(
            "UPDATE browser_leases SET epoch = :epoch, updated_at = :now "
            f"WHERE lease_hash = :lease_hash AND state IN ({marks}) AND {_EPOCH_GUARD}", params)
    return cursor.rowcount == 1


def delete_requested(lease_hash_: str, *, epoch: int) -> bool:
    """Remove a row whose container was never created (e.g. profile busy)."""
    conn = _conn()
    with conn:
        cursor = conn.execute(
            f"DELETE FROM browser_leases WHERE lease_hash = :h AND state = 'requested' "
            f"AND {_EPOCH_GUARD}", {"h": lease_hash_, "epoch": epoch})
    return cursor.rowcount == 1
