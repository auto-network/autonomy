"""Durable Web Push device, preference, and VAPID-key substrate.

This database is transport state only.  It is not an Activity inbox and it
never stores approval request or decision bodies.  Browser installations,
Fleet machines, and passkey credentials are deliberately separate records.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import stat
import time
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)

from tools.data_paths import resolve_store


DB_PATH = resolve_store("web_push")
KEY_DIR = resolve_store("web_push_keys")
LEGACY_KEY_PATH = resolve_store("web_push_vapid")

SCHEMA_VERSION = 2
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
KEY_ID_RE = re.compile(r"^[a-f0-9]{32}$")
MODES = frozenset({"off", "generic", "descriptive"})
DETAIL_LEVELS = frozenset({"generic", "descriptive"})
KEY_STATES = frozenset({"active", "retiring", "retired"})
NONTERMINAL_OUTBOX_STATES = (
    "fallback_wait",
    "budget_wait",
    "pending",
    "leased",
    "retry_wait",
)
_TOKEN_DOMAIN = b"autonomy:web-push-device-token:v1\0"


class WebPushStoreError(RuntimeError):
    """A bounded storage or compare-and-set refusal."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code.replace("_", " "))


@dataclass(frozen=True, slots=True)
class EnrollmentResult:
    device_id: str
    status: str
    device_update_token: str
    token_version: int


@dataclass(frozen=True, slots=True)
class VapidKeyRecord:
    key_id: str
    public_key: str
    status: str
    created_at: float
    retire_after: float | None
    retired_at: float | None
    private_key_path: str


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _token_hash(token: str) -> str:
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        raise WebPushStoreError("invalid_device_token")
    try:
        encoded = token.encode("ascii", "strict")
    except UnicodeEncodeError as exc:
        raise WebPushStoreError("invalid_device_token") from exc
    if not re.fullmatch(rb"[A-Za-z0-9_-]+", encoded):
        raise WebPushStoreError("invalid_device_token")
    return hashlib.sha256(_TOKEN_DOMAIN + encoded).hexdigest()


def _new_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, _token_hash(token)


def _device_id(value: Any) -> str:
    if not isinstance(value, str) or not DEVICE_ID_RE.fullmatch(value):
        raise WebPushStoreError("invalid_device_id")
    return value


def _bounded_label(value: Any, label: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value.encode("utf-8")) <= maximum
        or any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)
    ):
        raise WebPushStoreError(f"invalid_{label}")
    return value


def _expiration(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WebPushStoreError("invalid_expiration_time")
    converted = float(value)
    if not 0 < converted < 253402300800:  # before year 9999, finite by comparison
        raise WebPushStoreError("invalid_expiration_time")
    return converted


_SUBSCRIPTION_SCHEMA = """
CREATE TABLE web_push_subscriptions (
    id                       TEXT PRIMARY KEY,
    operator_subject         TEXT NOT NULL,
    device_id                TEXT NOT NULL,
    serving_machine_id       TEXT,
    endpoint                 TEXT NOT NULL,
    endpoint_hash            TEXT NOT NULL,
    endpoint_origin          TEXT NOT NULL,
    vapid_subject            TEXT NOT NULL,
    p256dh                   TEXT NOT NULL,
    auth_secret              TEXT NOT NULL,
    vapid_key_id             TEXT NOT NULL,
    expiration_time          REAL,
    max_detail               TEXT NOT NULL CHECK(max_detail IN ('generic','descriptive')),
    status                   TEXT NOT NULL CHECK(status IN ('active','retired')),
    device_update_token_hash TEXT NOT NULL,
    token_version            INTEGER NOT NULL CHECK(token_version >= 0),
    device_label             TEXT,
    browser_family           TEXT,
    platform_family          TEXT,
    created_at               REAL NOT NULL,
    updated_at               REAL NOT NULL,
    last_confirmed_at        REAL NOT NULL,
    retired_at               REAL,
    retire_reason            TEXT
);
CREATE UNIQUE INDEX uq_web_push_active_owner_device
    ON web_push_subscriptions(operator_subject, device_id)
    WHERE status='active';
CREATE UNIQUE INDEX uq_web_push_active_endpoint
    ON web_push_subscriptions(endpoint_hash)
    WHERE status='active';
CREATE INDEX idx_web_push_subscription_owner_status
    ON web_push_subscriptions(operator_subject, status, updated_at DESC);
CREATE INDEX idx_web_push_subscription_device
    ON web_push_subscriptions(device_id, status);
"""

_SUBSTRATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_push_preferences (
    operator_subject TEXT NOT NULL,
    application      TEXT NOT NULL,
    mode             TEXT NOT NULL CHECK(mode IN ('off','generic','descriptive')),
    updated_at       REAL NOT NULL,
    PRIMARY KEY(operator_subject, application)
);
CREATE TABLE IF NOT EXISTS web_push_vapid_keys (
    key_id           TEXT PRIMARY KEY,
    public_key       TEXT NOT NULL,
    status           TEXT NOT NULL CHECK(status IN ('active','retiring','retired')),
    created_at       REAL NOT NULL,
    retire_after     REAL,
    retired_at       REAL,
    private_key_path TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_web_push_one_active_vapid
    ON web_push_vapid_keys(status) WHERE status='active';
"""


def _execute_ddl(connection: sqlite3.Connection, script: str) -> None:
    """Execute simple DDL without ``executescript``'s implicit commit.

    Schema creation and the legacy table rename must be one transaction.  The
    scripts in this module contain only ordinary statements, so stepping them
    preserves rollback if any later migration check fails.
    """

    for statement in script.split(";"):
        statement = statement.strip()
        if statement:
            connection.execute(statement)


class WebPushStore:
    """SQLite owner for transport subscriptions and preferences."""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path is not None else DB_PATH

    def _connect_raw(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.db_path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def connect(self) -> sqlite3.Connection:
        self.initialize()
        return self._connect_raw()

    def initialize(self) -> None:
        connection = self._connect_raw()
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise WebPushStoreError("unsupported_schema_version")
            connection.execute("BEGIN IMMEDIATE")
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='web_push_subscriptions'"
            ).fetchone()
            if table is None:
                _execute_ddl(connection, _SUBSCRIPTION_SCHEMA)
            else:
                columns = {
                    row["name"]
                    for row in connection.execute(
                        "PRAGMA table_info(web_push_subscriptions)"
                    ).fetchall()
                }
                if "operator_subject" not in columns and "owner_id" in columns:
                    self._migrate_legacy_subscriptions(connection)
                else:
                    required = {
                        "id", "operator_subject", "device_id", "serving_machine_id",
                        "endpoint", "endpoint_hash", "endpoint_origin", "vapid_subject",
                        "p256dh", "auth_secret", "vapid_key_id", "expiration_time",
                        "max_detail", "status", "device_update_token_hash",
                        "token_version", "device_label", "browser_family",
                        "platform_family", "created_at", "updated_at",
                        "last_confirmed_at", "retired_at", "retire_reason",
                    }
                    if not required.issubset(columns):
                        raise WebPushStoreError("unsupported_subscription_schema")
                    _execute_ddl(connection,
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_web_push_active_owner_device "
                        "ON web_push_subscriptions(operator_subject,device_id) "
                        "WHERE status='active';"
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_web_push_active_endpoint "
                        "ON web_push_subscriptions(endpoint_hash) WHERE status='active';"
                        "CREATE INDEX IF NOT EXISTS idx_web_push_subscription_owner_status "
                        "ON web_push_subscriptions(operator_subject,status,updated_at DESC);"
                        "CREATE INDEX IF NOT EXISTS idx_web_push_subscription_device "
                        "ON web_push_subscriptions(device_id,status);"
                    )
            _execute_ddl(connection, _SUBSTRATE_SCHEMA)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _migrate_legacy_subscriptions(connection: sqlite3.Connection) -> None:
        connection.execute(
            "ALTER TABLE web_push_subscriptions RENAME TO web_push_subscriptions_legacy"
        )
        _execute_ddl(connection, _SUBSCRIPTION_SCHEMA)
        rows = connection.execute(
            "SELECT * FROM web_push_subscriptions_legacy ORDER BY created_at,installation_id"
        ).fetchall()
        for row in rows:
            endpoint = row["endpoint"]
            parsed = urlsplit(endpoint)
            endpoint_origin = f"{parsed.scheme}://{parsed.hostname}"
            if parsed.port not in (None, 443):
                endpoint_origin += f":{parsed.port}"
            _discarded_token, unreachable_hash = _new_token()
            created = float(row["created_at"])
            updated = float(row["last_seen_at"])
            connection.execute(
                "INSERT INTO web_push_subscriptions("
                "id,operator_subject,device_id,serving_machine_id,endpoint,endpoint_hash,"
                "endpoint_origin,vapid_subject,p256dh,auth_secret,vapid_key_id,expiration_time,max_detail,"
                "status,device_update_token_hash,token_version,device_label,browser_family,"
                "platform_family,created_at,updated_at,last_confirmed_at,retired_at,"
                "retire_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex, row["owner_id"], row["installation_id"], None,
                    endpoint, row["endpoint_hash"], endpoint_origin, row["origin"], row["p256dh"],
                    row["auth_secret"], row["vapid_key_id"], None, "generic",
                    row["status"], unreachable_hash, 0, None, None, None, created,
                    updated, updated, row["retired_at"], row["retire_reason"],
                ),
            )
        connection.execute("DROP TABLE web_push_subscriptions_legacy")

    @staticmethod
    def _key_is_usable(connection: sqlite3.Connection, key_id: str) -> bool:
        row = connection.execute(
            "SELECT status FROM web_push_vapid_keys WHERE key_id=?", (key_id,)
        ).fetchone()
        return row is not None and row["status"] in {"active", "retiring"}

    def enroll(
        self,
        *,
        operator_subject: str,
        device_id: str,
        endpoint: str,
        endpoint_hash: str,
        endpoint_origin: str,
        vapid_subject: str,
        p256dh: str,
        auth_secret: str,
        vapid_key_id: str,
        expiration_time: float | int | None,
        max_detail: str = "generic",
        device_label: str | None = None,
        browser_family: str | None = None,
        platform_family: str | None = None,
        serving_machine_id: str | None = None,
        now: float | None = None,
    ) -> EnrollmentResult:
        device_id = _device_id(device_id)
        if not isinstance(operator_subject, str) or not re.fullmatch(r"[a-f0-9]{64}", operator_subject):
            raise WebPushStoreError("invalid_operator_subject")
        if max_detail not in DETAIL_LEVELS:
            raise WebPushStoreError("invalid_max_detail")
        device_label = _bounded_label(device_label, "device_label", maximum=160)
        browser_family = _bounded_label(browser_family, "browser_family", maximum=40)
        platform_family = _bounded_label(platform_family, "platform_family", maximum=40)
        expiration_time = _expiration(expiration_time)
        if serving_machine_id is not None:
            serving_machine_id = _bounded_label(
                serving_machine_id, "serving_machine_id", maximum=128
            )
        if not isinstance(vapid_key_id, str) or not KEY_ID_RE.fullmatch(vapid_key_id):
            raise WebPushStoreError("invalid_vapid_key")
        timestamp = float(time.time() if now is None else now)
        token, token_hash = _new_token()
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if not self._key_is_usable(connection, vapid_key_id):
                raise WebPushStoreError("vapid_key_unavailable")
            collision = connection.execute(
                "SELECT operator_subject FROM web_push_subscriptions "
                "WHERE device_id=? AND status='active'", (device_id,)
            ).fetchone()
            if collision is not None and collision["operator_subject"] != operator_subject:
                raise WebPushStoreError("device_conflict")
            endpoint_row = connection.execute(
                "SELECT operator_subject,device_id FROM web_push_subscriptions "
                "WHERE endpoint_hash=? AND status='active'", (endpoint_hash,)
            ).fetchone()
            if endpoint_row is not None and (
                endpoint_row["operator_subject"] != operator_subject
                or endpoint_row["device_id"] != device_id
            ):
                raise WebPushStoreError("endpoint_conflict")
            existing = connection.execute(
                "SELECT * FROM web_push_subscriptions WHERE operator_subject=? "
                "AND device_id=? AND status='active'",
                (operator_subject, device_id),
            ).fetchone()
            if existing is not None:
                if existing["vapid_key_id"] != vapid_key_id:
                    # VAPID identity is immutable for one PushSubscription.
                    # An authenticated browser that has explicitly subscribed
                    # under the new active key replaces the old row rather
                    # than rewriting its cryptographic binding in place.
                    self._retire_row(
                        connection, existing, "vapid_reenrolled", timestamp,
                    )
                    existing = None
                else:
                    version = int(existing["token_version"]) + 1
                    connection.execute(
                        "UPDATE web_push_subscriptions SET endpoint=?,endpoint_hash=?,"
                        "endpoint_origin=?,vapid_subject=?,p256dh=?,auth_secret=?,expiration_time=?,"
                        "max_detail=?,device_update_token_hash=?,token_version=?,device_label=?,"
                        "browser_family=?,platform_family=?,updated_at=?,last_confirmed_at=?,"
                        "retired_at=NULL,retire_reason=NULL WHERE id=?",
                        (
                            endpoint, endpoint_hash, endpoint_origin, vapid_subject, p256dh,
                            auth_secret, expiration_time, max_detail, token_hash, version,
                            device_label, browser_family, platform_family, timestamp,
                            timestamp, existing["id"],
                        ),
                    )
            if existing is None:
                version = 1
                connection.execute(
                    "INSERT INTO web_push_subscriptions("
                    "id,operator_subject,device_id,serving_machine_id,endpoint,endpoint_hash,"
                    "endpoint_origin,vapid_subject,p256dh,auth_secret,vapid_key_id,expiration_time,max_detail,"
                    "status,device_update_token_hash,token_version,device_label,browser_family,"
                    "platform_family,created_at,updated_at,last_confirmed_at,retired_at,"
                    "retire_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                    (
                        uuid.uuid4().hex, operator_subject, device_id, serving_machine_id,
                        endpoint, endpoint_hash, endpoint_origin, vapid_subject, p256dh, auth_secret,
                        vapid_key_id, expiration_time, max_detail, "active", token_hash,
                        version, device_label, browser_family, platform_family, timestamp,
                        timestamp, timestamp,
                    ),
                )
            connection.commit()
            return EnrollmentResult(device_id, "active", token, version)
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise WebPushStoreError("subscription_conflict") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def refresh(
        self,
        *,
        device_id: str,
        device_update_token: str,
        token_version: int,
        old_endpoint_hash: str,
        subscription: dict[str, Any] | None,
        now: float | None = None,
    ) -> EnrollmentResult | None:
        device_id = _device_id(device_id)
        if isinstance(token_version, bool) or not isinstance(token_version, int) or token_version < 0:
            raise WebPushStoreError("invalid_token_version")
        if not isinstance(old_endpoint_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", old_endpoint_hash):
            raise WebPushStoreError("invalid_endpoint_hash")
        supplied_hash = _token_hash(device_update_token)
        timestamp = float(time.time() if now is None else now)
        next_token, next_hash = _new_token()
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            candidates = connection.execute(
                "SELECT * FROM web_push_subscriptions WHERE device_id=? AND status='active'",
                (device_id,),
            ).fetchall()
            row = next(
                (
                    candidate for candidate in candidates
                    if hmac.compare_digest(candidate["device_update_token_hash"], supplied_hash)
                    and int(candidate["token_version"]) == token_version
                    and hmac.compare_digest(candidate["endpoint_hash"], old_endpoint_hash)
                ),
                None,
            )
            if row is None:
                raise WebPushStoreError("device_not_found")
            if subscription is None:
                self._retire_row(connection, row, "browser_subscription_lost", timestamp)
                connection.commit()
                return None
            required = {
                "endpoint", "endpoint_hash", "endpoint_origin", "p256dh", "auth_secret",
                "vapid_key_id", "expiration_time",
            }
            if set(subscription) != required:
                raise WebPushStoreError("invalid_subscription")
            if subscription["vapid_key_id"] != row["vapid_key_id"]:
                raise WebPushStoreError("vapid_key_mismatch")
            if not self._key_is_usable(connection, row["vapid_key_id"]):
                raise WebPushStoreError("vapid_key_unavailable")
            endpoint_row = connection.execute(
                "SELECT id FROM web_push_subscriptions WHERE endpoint_hash=? "
                "AND status='active' AND id<>?",
                (subscription["endpoint_hash"], row["id"]),
            ).fetchone()
            if endpoint_row is not None:
                raise WebPushStoreError("endpoint_conflict")
            next_version = token_version + 1
            changed = connection.execute(
                "UPDATE web_push_subscriptions SET endpoint=?,endpoint_hash=?,"
                "endpoint_origin=?,p256dh=?,auth_secret=?,expiration_time=?,"
                "device_update_token_hash=?,token_version=?,updated_at=?,last_confirmed_at=? "
                "WHERE id=? AND token_version=? AND device_update_token_hash=? "
                "AND endpoint_hash=? AND status='active'",
                (
                    subscription["endpoint"], subscription["endpoint_hash"],
                    subscription["endpoint_origin"], subscription["p256dh"],
                    subscription["auth_secret"], _expiration(subscription["expiration_time"]),
                    next_hash, next_version, timestamp, timestamp, row["id"], token_version,
                    supplied_hash, old_endpoint_hash,
                ),
            ).rowcount
            if changed != 1:
                raise WebPushStoreError("device_not_found")
            connection.commit()
            return EnrollmentResult(device_id, "active", next_token, next_version)
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise WebPushStoreError("subscription_conflict") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _retire_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        reason: str,
        timestamp: float,
    ) -> None:
        connection.execute(
            "UPDATE web_push_subscriptions SET status='retired',retired_at=?,"
            "retire_reason=?,updated_at=? WHERE id=? AND status='active'",
            (timestamp, reason, timestamp, row["id"]),
        )
        # The outbox schema belongs to the next transport bead and may not yet
        # exist in a store initialized solely for device enrollment.
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_push_outbox'"
        ).fetchone():
            placeholders = ",".join("?" for _ in NONTERMINAL_OUTBOX_STATES)
            connection.execute(
                f"UPDATE web_push_outbox SET state='canceled',last_reason=? "
                f"WHERE installation_id=? AND state IN ({placeholders})",
                (reason, row["device_id"], *NONTERMINAL_OUTBOX_STATES),
            )

    def retire(self, operator_subject: str, device_id: str, *, reason: str) -> bool:
        device_id = _device_id(device_id)
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM web_push_subscriptions WHERE operator_subject=? "
                "AND device_id=? AND status='active'",
                (operator_subject, device_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            self._retire_row(connection, row, reason, time.time())
            connection.commit()
            return True
        finally:
            connection.close()

    def state(self, operator_subject: str, device_id: str | None = None) -> dict[str, Any]:
        connection = self.connect()
        try:
            active_count = connection.execute(
                "SELECT count(*) FROM web_push_subscriptions "
                "WHERE operator_subject=? AND status='active'", (operator_subject,),
            ).fetchone()[0]
            row = None
            if device_id is not None and DEVICE_ID_RE.fullmatch(device_id):
                row = connection.execute(
                    "SELECT status,last_confirmed_at AS last_seen_at,retire_reason "
                    "FROM web_push_subscriptions WHERE operator_subject=? AND device_id=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (operator_subject, device_id),
                ).fetchone()
            return {
                "active_installations": int(active_count),
                "this_installation": dict(row) if row is not None else None,
            }
        finally:
            connection.close()

    def list_devices(self, operator_subject: str) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT s.device_id,s.device_label,s.browser_family,s.platform_family,"
                "s.max_detail,s.status,s.last_confirmed_at,s.retired_at,s.retire_reason,"
                "s.vapid_key_id,k.created_at AS vapid_created_at "
                "FROM web_push_subscriptions s LEFT JOIN web_push_vapid_keys k "
                "ON k.key_id=s.vapid_key_id WHERE s.operator_subject=? "
                "AND s.id=(SELECT newest.id FROM web_push_subscriptions newest "
                "WHERE newest.operator_subject=s.operator_subject "
                "AND newest.device_id=s.device_id ORDER BY newest.created_at DESC,newest.rowid DESC LIMIT 1) "
                "ORDER BY s.updated_at DESC",
                (operator_subject,),
            ).fetchall()
            return [
                {
                    "device_id": row["device_id"],
                    "device_label": row["device_label"],
                    "browser_family": row["browser_family"],
                    "platform_family": row["platform_family"],
                    "max_detail": row["max_detail"],
                    "status": row["status"],
                    "vapid_key_id": row["vapid_key_id"],
                    "vapid_key_created_at": row["vapid_created_at"],
                    "last_confirmed_at": row["last_confirmed_at"],
                    "retired_at": row["retired_at"],
                    "health": (
                        "active" if row["status"] == "active"
                        else "revoked" if row["retire_reason"] == "push_service_gone"
                        else "retired"
                    ),
                }
                for row in rows
            ]
        finally:
            connection.close()

    def preferences(
        self, operator_subject: str, applications: Iterable[str]
    ) -> dict[str, str]:
        names = tuple(applications)
        connection = self.connect()
        try:
            stored = {
                row["application"]: row["mode"]
                for row in connection.execute(
                    "SELECT application,mode FROM web_push_preferences "
                    "WHERE operator_subject=?", (operator_subject,),
                ).fetchall()
            }
        finally:
            connection.close()
        return {name: stored.get(name, "off") for name in names}

    def set_preference(
        self, operator_subject: str, application: str, mode: str
    ) -> str:
        if mode not in MODES:
            raise WebPushStoreError("invalid_preference_mode")
        connection = self.connect()
        try:
            connection.execute(
                "INSERT INTO web_push_preferences(operator_subject,application,mode,updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(operator_subject,application) DO UPDATE SET "
                "mode=excluded.mode,updated_at=excluded.updated_at",
                (operator_subject, application, mode, time.time()),
            )
            connection.commit()
            return mode
        finally:
            connection.close()


class VapidKeyCustody:
    """Stable P-256 key files plus active/retiring/retired metadata."""

    def __init__(
        self,
        store: WebPushStore,
        *,
        key_dir: Path | str | None = None,
        legacy_key_path: Path | str | None = None,
    ):
        self.store = store
        self.key_dir = Path(key_dir) if key_dir is not None else KEY_DIR
        self.legacy_key_path = (
            Path(legacy_key_path) if legacy_key_path is not None else LEGACY_KEY_PATH
        )

    def _secure_directory(self) -> None:
        self.key_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = os.lstat(self.key_dir)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise WebPushStoreError("vapid_key_directory_unsafe")
        if info.st_uid != os.geteuid():
            raise WebPushStoreError("vapid_key_directory_owner_mismatch")
        os.chmod(self.key_dir, 0o700)
        if stat.S_IMODE(os.stat(self.key_dir).st_mode) != 0o700:
            raise WebPushStoreError("vapid_key_directory_mode_mismatch")

    def _path(self, basename: str) -> Path:
        if Path(basename).name != basename or not re.fullmatch(r"[a-f0-9]{32}\.pem", basename):
            raise WebPushStoreError("vapid_key_path_invalid")
        path = self.key_dir / basename
        if path.parent.resolve() != self.key_dir.resolve():
            raise WebPushStoreError("vapid_key_path_invalid")
        return path

    def _write_key(self, private_key: ec.EllipticCurvePrivateKey, path: Path) -> None:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            try:
                stream.write(private_key.private_bytes(
                    Encoding.PEM, PrivateFormat.PKCS8, NoEncryption(),
                ))
                stream.flush()
                os.fsync(stream.fileno())
            except Exception:
                try:
                    path.unlink()
                except OSError:
                    pass
                raise
        os.chmod(path, 0o600)
        directory = os.open(self.key_dir, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _load_path(self, path: Path):
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise WebPushStoreError("vapid_key_file_unsafe")
        if info.st_uid != os.geteuid():
            raise WebPushStoreError("vapid_key_file_owner_mismatch")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise WebPushStoreError("vapid_key_file_mode_mismatch")
        try:
            private_key = load_pem_private_key(path.read_bytes(), password=None)
        except Exception as exc:
            raise WebPushStoreError("vapid_key_file_invalid") from exc
        if (
            not isinstance(private_key, ec.EllipticCurvePrivateKey)
            or not isinstance(private_key.curve, ec.SECP256R1)
        ):
            raise WebPushStoreError("vapid_key_file_invalid")
        return private_key

    @staticmethod
    def _public_key(private_key: ec.EllipticCurvePrivateKey) -> str:
        return _b64url(private_key.public_key().public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint,
        ))

    @staticmethod
    def _record(row: sqlite3.Row) -> VapidKeyRecord:
        return VapidKeyRecord(**dict(row))

    def ensure_active(self) -> VapidKeyRecord:
        self._secure_directory()
        self.store.initialize()
        connection = self.store._connect_raw()
        created_path: Path | None = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM web_push_vapid_keys WHERE status='active'"
            ).fetchall()
            if len(rows) > 1:
                raise WebPushStoreError("ambiguous_active_vapid_key")
            if rows:
                record = self._record(rows[0])
                self._verify_record(record)
                connection.commit()
                return record

            legacy = self.legacy_key_path
            if legacy.exists():
                legacy_info = os.lstat(legacy)
                if stat.S_ISLNK(legacy_info.st_mode) or not stat.S_ISREG(legacy_info.st_mode):
                    raise WebPushStoreError("legacy_vapid_key_unsafe")
                if legacy_info.st_uid != os.geteuid():
                    raise WebPushStoreError("legacy_vapid_key_owner_mismatch")
                os.chmod(legacy, 0o600)
                try:
                    private_key = load_pem_private_key(
                        legacy.read_bytes(), password=None,
                    )
                except Exception as exc:
                    raise WebPushStoreError("legacy_vapid_key_invalid") from exc
                if (
                    not isinstance(private_key, ec.EllipticCurvePrivateKey)
                    or not isinstance(private_key.curve, ec.SECP256R1)
                ):
                    raise WebPushStoreError("legacy_vapid_key_invalid")
            else:
                private_key = ec.generate_private_key(ec.SECP256R1())
            key_id = uuid.uuid4().hex
            basename = f"{key_id}.pem"
            path = self._path(basename)
            self._write_key(private_key, path)
            created_path = path
            public = self._public_key(private_key)
            created_at = time.time()
            connection.execute(
                "INSERT INTO web_push_vapid_keys(key_id,public_key,status,created_at,"
                "retire_after,retired_at,private_key_path) VALUES(?,?,'active',?,NULL,NULL,?)",
                (key_id, public, created_at, basename),
            )
            # The pre-lifecycle sender labeled every subscription ``primary``.
            # Rebind metadata to this exact copied key; no subscription or key
            # material rotates during migration.
            connection.execute(
                "UPDATE web_push_subscriptions SET vapid_key_id=? "
                "WHERE vapid_key_id='primary'", (key_id,),
            )
            connection.commit()
            created_path = None
            return VapidKeyRecord(
                key_id, public, "active", created_at, None, None, basename,
            )
        except Exception:
            connection.rollback()
            if created_path is not None:
                try:
                    created_path.unlink()
                except OSError:
                    pass
            raise
        finally:
            connection.close()

    def _verify_record(self, record: VapidKeyRecord):
        if record.status not in KEY_STATES or not KEY_ID_RE.fullmatch(record.key_id):
            raise WebPushStoreError("vapid_key_metadata_invalid")
        path = self._path(record.private_key_path)
        vapid = self._load_path(path)
        if not hmac.compare_digest(self._public_key(vapid), record.public_key):
            raise WebPushStoreError("vapid_key_material_mismatch")
        return vapid

    def load(self, key_id: str | None = None):
        self.ensure_active()
        connection = self.store.connect()
        try:
            if key_id is None:
                row = connection.execute(
                    "SELECT * FROM web_push_vapid_keys WHERE status='active'"
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM web_push_vapid_keys WHERE key_id=? "
                    "AND status IN ('active','retiring')", (key_id,),
                ).fetchone()
            if row is None:
                raise WebPushStoreError("vapid_key_unavailable")
            record = self._record(row)
            return record, self._verify_record(record)
        finally:
            connection.close()

    def rotate(self, *, retire_after: float) -> VapidKeyRecord:
        if isinstance(retire_after, bool) or not isinstance(retire_after, (int, float)):
            raise WebPushStoreError("invalid_retire_after")
        deadline = float(retire_after)
        if deadline <= time.time():
            raise WebPushStoreError("invalid_retire_after")
        current = self.ensure_active()
        private_key = ec.generate_private_key(ec.SECP256R1())
        key_id = uuid.uuid4().hex
        basename = f"{key_id}.pem"
        path = self._path(basename)
        self._write_key(private_key, path)
        public = self._public_key(private_key)
        created_at = time.time()
        connection = self.store.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT key_id FROM web_push_vapid_keys WHERE status='active'"
            ).fetchone()
            if active is None or active["key_id"] != current.key_id:
                raise WebPushStoreError("vapid_rotation_conflict")
            connection.execute(
                "UPDATE web_push_vapid_keys SET status='retiring',retire_after=? "
                "WHERE key_id=? AND status='active'", (deadline, current.key_id),
            )
            connection.execute(
                "INSERT INTO web_push_vapid_keys(key_id,public_key,status,created_at,"
                "retire_after,retired_at,private_key_path) VALUES(?,?,'active',?,NULL,NULL,?)",
                (key_id, public, created_at, basename),
            )
            connection.commit()
            return VapidKeyRecord(
                key_id, public, "active", created_at, None, None, basename,
            )
        except Exception:
            connection.rollback()
            try:
                path.unlink()
            except OSError:
                pass
            raise
        finally:
            connection.close()

    def retire(self, key_id: str) -> None:
        connection = self.store.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM web_push_vapid_keys WHERE key_id=?", (key_id,)
            ).fetchone()
            if row is None or row["status"] != "retiring":
                raise WebPushStoreError("vapid_key_not_retiring")
            active_subscriptions = connection.execute(
                "SELECT count(*) FROM web_push_subscriptions "
                "WHERE vapid_key_id=? AND status='active'", (key_id,),
            ).fetchone()[0]
            if active_subscriptions:
                raise WebPushStoreError("vapid_key_still_in_use")
            connection.execute(
                "UPDATE web_push_vapid_keys SET status='retired',retired_at=? "
                "WHERE key_id=?", (time.time(), key_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


__all__ = [
    "DB_PATH",
    "DEVICE_ID_RE",
    "EnrollmentResult",
    "KEY_DIR",
    "LEGACY_KEY_PATH",
    "VapidKeyCustody",
    "VapidKeyRecord",
    "WebPushStore",
    "WebPushStoreError",
]
