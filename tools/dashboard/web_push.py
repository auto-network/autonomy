"""Dashboard Web Push transport: subscriptions, attention latch, and sender.

The rows in this module are delivery machinery only. Approval truth remains in
``approval_requests`` and Activity remains the semantic inbox. The first
registered producer is an approval entering ``pending``; adding another
producer requires an explicit renderer, route, delivery class, and budget.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import logging
import random
import re
import socket
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric import ec
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from tools.dashboard import api_auth
from tools.dashboard.dao import web_push as web_push_dao
from tools.data_paths import resolve_store


logger = logging.getLogger(__name__)

DB_PATH = resolve_store("web_push")
VAPID_PATH = resolve_store("web_push_vapid")
VAPID_DIR = resolve_store("web_push_keys")

_MAX_BODY_BYTES = 12 * 1024
_MAX_ENDPOINT_CHARS = 4096
_GRACE_SECONDS = 20.0
_EVENT_TTL_SECONDS = 6 * 60 * 60
_LEASE_SECONDS = 60.0
_MAX_ATTEMPTS = 10
_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_INSTALLATION_ID = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_EVENT_ID = re.compile(r"^[A-Za-z0-9:_-]{1,160}$")

# Browser-selected services only. This makes a browser-supplied endpoint
# incapable of selecting a Dashboard/private host. DNS is additionally checked
# for global addresses before publication; redirects and ambient proxies are
# disabled in _send_push.
_PUSH_SERVICE_HOSTS = (
    "push.apple.com",
    "fcm.googleapis.com",
    "push.services.mozilla.com",
    "notify.windows.com",
)

_BUDGET_LIMITS = {
    "operator_approval": 12,  # one attention event, not one device/attempt
    "operator_diagnostic": 3,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_push_attention_events (
    event_id        TEXT NOT NULL,
    event_version   INTEGER NOT NULL,
    owner_id        TEXT NOT NULL,
    application     TEXT NOT NULL,
    attention_class TEXT NOT NULL,
    route           TEXT NOT NULL,
    coalesce_key    TEXT NOT NULL,
    delivery_class  TEXT NOT NULL,
    budget_class    TEXT NOT NULL,
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    acknowledged_at REAL,
    canceled_at     REAL,
    cancel_reason   TEXT,
    budget_reserved_at REAL,
    PRIMARY KEY(event_id, event_version, owner_id)
);

CREATE TABLE IF NOT EXISTS web_push_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL,
    event_version   INTEGER NOT NULL,
    owner_id        TEXT NOT NULL,
    installation_id TEXT NOT NULL,
    state           TEXT NOT NULL,
    available_at    REAL NOT NULL,
    expires_at      REAL NOT NULL,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    lease_owner     TEXT,
    lease_until     REAL,
    accepted_at     REAL,
    last_status     INTEGER,
    last_reason     TEXT,
    UNIQUE(event_id, event_version, installation_id)
);
CREATE INDEX IF NOT EXISTS idx_web_push_outbox_due
    ON web_push_outbox(state, available_at, lease_until);

CREATE TABLE IF NOT EXISTS web_push_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    outbox_id       INTEGER NOT NULL,
    attempted_at    REAL NOT NULL,
    outcome         TEXT NOT NULL,
    status          INTEGER,
    reason          TEXT
);
"""

_vapid_lock = threading.Lock()
_vapid: dict[str, object] = {}
_worker_task: asyncio.Task | None = None
_worker_wake: asyncio.Event | None = None
_worker_stop: asyncio.Event | None = None
_worker_loop_ref: asyncio.AbstractEventLoop | None = None


def _conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else DB_PATH
    web_push_dao.WebPushStore(path).initialize()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA)
    return connection


def init_db(db_path: Path | str | None = None) -> None:
    _conn(db_path).close()


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_b64url(value: object, *, name: str, exact_len: int) -> bytes:
    if not isinstance(value, str) or not value or not _B64URL.fullmatch(value):
        raise ValueError(f"{name} must be unpadded base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise ValueError(f"{name} is not valid base64url") from exc
    if len(decoded) != exact_len:
        raise ValueError(f"{name} must decode to {exact_len} bytes")
    return decoded


def _load_vapid(key_id: str | None = None):
    """Load a custody-verified active/retiring sender key for pywebpush."""

    with _vapid_lock:
        store = web_push_dao.WebPushStore(DB_PATH)
        custody = web_push_dao.VapidKeyCustody(
            store, key_dir=VAPID_DIR, legacy_key_path=VAPID_PATH,
        )
        record, _private_key = custody.load(key_id)
        cached = _vapid.get(record.key_id)
        if cached is not None:
            return cached
        try:
            from py_vapid import Vapid
        except ImportError as exc:
            raise RuntimeError(
                "Web Push runtime is unavailable; install deploy/requirements.txt"
            ) from exc
        vapid = Vapid.from_file(str(VAPID_DIR / record.private_key_path))
        _vapid[record.key_id] = vapid
        return vapid


def _application_server_key() -> str:
    store = web_push_dao.WebPushStore(DB_PATH)
    return web_push_dao.VapidKeyCustody(
        store, key_dir=VAPID_DIR, legacy_key_path=VAPID_PATH,
    ).ensure_active().public_key


def _stable_owner_id() -> str:
    """Derive routing ownership from the stored personal root, never a SID."""
    from tools.dashboard.identity_routes import resolve_stable_personal_root_public_key

    root_pub = resolve_stable_personal_root_public_key()
    return hashlib.sha256(
        b"autonomy:web-push-owner:v1\0" + bytes.fromhex(root_pub)
    ).hexdigest()


def _operator_only(request: Request) -> JSONResponse | None:
    """Require the human browser principal; caller identity is never in JSON."""

    principal = api_auth.principal_from_request(request)
    if principal.kind is api_auth.ApiPrincipalKind.OPERATOR_COOKIE:
        return None
    # An intentionally disabled/unenrolled gate has no cookie principal. The
    # browser is the operator in that deployment; org/local-session bearers are
    # still positively identified and refused.
    if principal.kind is api_auth.ApiPrincipalKind.COMPATIBILITY:
        from tools.dashboard import unlock_routes
        if not unlock_routes.gate_enforced():
            return None
        return JSONResponse({"error": "authentication required"}, status_code=401)
    return JSONResponse({"error": "operator browser authority required"}, status_code=403)


def _validated_request_origin(request: Request) -> str:
    expected = urlsplit(str(request.base_url))
    supplied_raw = request.headers.get("origin")
    supplied = urlsplit(supplied_raw) if supplied_raw else expected
    if (
        supplied.scheme != "https"
        or supplied.hostname != expected.hostname
        or supplied.port != expected.port
        or supplied.username
        or supplied.password
    ):
        raise ValueError("same-origin HTTPS request required")
    host = supplied.hostname.rstrip(".").lower()
    port = supplied.port
    return f"https://{host}" + (f":{port}" if port not in (None, 443) else "")


def _valid_push_host(host: str) -> bool:
    host = host.rstrip(".").lower()
    return any(host == suffix or host.endswith("." + suffix)
               for suffix in _PUSH_SERVICE_HOSTS)


def _validate_subscription(value: object) -> dict:
    if not isinstance(value, dict) or set(value) - {"endpoint", "expirationTime", "keys"}:
        raise ValueError("subscription must contain only endpoint, expirationTime, and keys")
    endpoint = value.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > _MAX_ENDPOINT_CHARS:
        raise ValueError("subscription endpoint is invalid")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("subscription endpoint is invalid") from exc
    host = (parsed.hostname or "").rstrip(".").lower()
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.fragment
        or port not in (None, 443)
        or not _valid_push_host(host)
    ):
        raise ValueError("subscription endpoint is not an allowed browser push service")
    keys = value.get("keys")
    if not isinstance(keys, dict) or set(keys) != {"p256dh", "auth"}:
        raise ValueError("subscription keys must contain p256dh and auth")
    p256dh = _decode_b64url(keys.get("p256dh"), name="p256dh", exact_len=65)
    if p256dh[0] != 0x04:
        raise ValueError("p256dh must be an uncompressed P-256 point")
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
    except ValueError as exc:
        raise ValueError("p256dh must be a valid P-256 point") from exc
    _decode_b64url(keys.get("auth"), name="auth", exact_len=16)
    expiration = value.get("expirationTime")
    if expiration is not None:
        if isinstance(expiration, bool) or not isinstance(expiration, (int, float)):
            raise ValueError("expirationTime must be a finite Unix millisecond value")
        try:
            expiration = float(expiration) / 1000.0
        except OverflowError as exc:
            raise ValueError("expirationTime is out of range") from exc
        if not 0 < expiration < 253402300800:
            raise ValueError("expirationTime is out of range")
    return {
        "endpoint": endpoint,
        "host": host,
        "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
        "expiration_time": expiration,
    }


def _json_request(request: Request, raw: bytes) -> dict:
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type.strip().lower() != "application/json":
        raise TypeError("application/json required")
    if len(raw) > _MAX_BODY_BYTES:
        raise OverflowError("request is too large")
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    return body


def _upsert_subscription(
    *, owner_id: str, origin: str, installation_id: str, subscription: dict,
    db_path: Path | str | None = None,
) -> None:
    if not _INSTALLATION_ID.fullmatch(installation_id):
        raise ValueError("installation_id is invalid")
    endpoint = subscription["endpoint"]
    endpoint_hash = hashlib.sha256(endpoint.encode()).hexdigest()
    parsed = urlsplit(endpoint)
    endpoint_origin = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port not in (None, 443):
        endpoint_origin += f":{parsed.port}"
    store = web_push_dao.WebPushStore(DB_PATH if db_path is None else db_path)
    custody = web_push_dao.VapidKeyCustody(
        store, key_dir=VAPID_DIR, legacy_key_path=VAPID_PATH,
    )
    try:
        key = custody.ensure_active()
        store.enroll(
            operator_subject=owner_id,
            device_id=installation_id,
            endpoint=endpoint,
            endpoint_hash=endpoint_hash,
            endpoint_origin=endpoint_origin,
            vapid_subject=origin,
            p256dh=subscription["keys"]["p256dh"],
            auth_secret=subscription["keys"]["auth"],
            vapid_key_id=key.key_id,
            expiration_time=subscription.get("expiration_time"),
        )
    except web_push_dao.WebPushStoreError as exc:
        if exc.code in {"device_conflict", "endpoint_conflict"}:
            raise PermissionError(
                "this browser installation or push endpoint belongs to another operator"
            ) from exc
        raise


def _retire_subscription(
    installation_id: str, owner_id: str, reason: str,
    *, db_path: Path | str | None = None,
) -> bool:
    store = web_push_dao.WebPushStore(DB_PATH if db_path is None else db_path)
    return store.retire(
        owner_id, installation_id, reason=reason,
    )


def _subscription_state(
    owner_id: str, installation_id: str | None,
    *, db_path: Path | str | None = None,
) -> dict:
    return web_push_dao.WebPushStore(
        DB_PATH if db_path is None else db_path
    ).state(owner_id, installation_id)


def _register_attention(
    *, event_id: str, event_version: int, application: str,
    attention_class: str, route: str, coalesce_key: str,
    delivery_class: str, budget_class: str, grace_seconds: float,
    created_at: float | None = None,
    db_path: Path | str | None = None,
) -> int:
    if not _EVENT_ID.fullmatch(event_id):
        raise ValueError("event_id is invalid")
    owner_id = _stable_owner_id()
    now = time.time()
    event_created_at = float(created_at) if created_at is not None else now
    expires_at = event_created_at + _EVENT_TTL_SECONDS
    if expires_at <= now:
        return 0
    connection = _conn(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        subscriptions = connection.execute(
            "SELECT device_id AS installation_id FROM web_push_subscriptions "
            "WHERE operator_subject=? AND status='active'", (owner_id,),
        ).fetchall()
        if not subscriptions:
            connection.rollback()
            return 0
        connection.execute(
            "INSERT OR IGNORE INTO web_push_attention_events(event_id,event_version,"
            "owner_id,application,attention_class,route,coalesce_key,delivery_class,"
            "budget_class,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id, event_version, owner_id, application, attention_class,
                route, coalesce_key, delivery_class, budget_class,
                event_created_at, expires_at,
            ),
        )
        for subscription in subscriptions:
            connection.execute(
                "INSERT OR IGNORE INTO web_push_outbox(event_id,event_version,owner_id,"
                "installation_id,state,available_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (
                    event_id, event_version, owner_id,
                    subscription["installation_id"],
                    "fallback_wait" if grace_seconds > 0 else "pending",
                    max(now, event_created_at + max(0.0, grace_seconds)), expires_at,
                ),
            )
        connection.commit()
        return len(subscriptions)
    finally:
        connection.close()


def register_approval_pending_sync(
    approval_id: str, kind: str, *, created_at: float | None = None,
) -> int:
    """Synchronous producer seam for validated non-request worker paths."""
    try:
        count = _register_attention(
            event_id=f"approval:{approval_id}", event_version=1,
            application="approvals", attention_class="approval_pending",
            route=f"/activity?focus=approval&id={approval_id}",
            coalesce_key=f"approval:{approval_id}",
            delivery_class="normal", budget_class="operator_approval",
            grace_seconds=_GRACE_SECONDS, created_at=created_at,
        )
    except RuntimeError as exc:
        if str(exc) == "no personal identity is stored":
            logger.debug(
                "web_push_attention_skipped class=approval_pending reason=no_personal_identity"
            )
            return 0
        logger.exception(
            "web_push_attention_registration_failed class=approval_pending kind=%s",
            kind,
        )
        return 0
    except Exception:
        logger.exception(
            "web_push_attention_registration_failed class=approval_pending kind=%s",
            kind,
        )
        return 0
    if count:
        wake_worker()
    return count


async def register_approval_pending(approval_id: str, kind: str) -> int:
    """Register one approval transition after approval truth commits."""

    return await asyncio.to_thread(
        register_approval_pending_sync, approval_id, kind,
    )


def _reconcile_approval_attention_sync(eligible_kind) -> None:
    """Close the cross-store crash gap without treating outbox as truth."""
    from tools.dashboard.dao import approval_requests as approval_truth

    try:
        owner_id = _stable_owner_id()
    except RuntimeError:
        return
    connection = _conn()
    try:
        has_subscription = connection.execute(
            "SELECT 1 FROM web_push_subscriptions WHERE operator_subject=? "
            "AND status='active' LIMIT 1", (owner_id,),
        ).fetchone() is not None
    finally:
        connection.close()
    if not has_subscription:
        return

    pending = approval_truth.pending_all(limit=500)
    for row in pending:
        if eligible_kind(row["kind"]):
            register_approval_pending_sync(
                row["id"], row["kind"], created_at=row["created_at"],
            )
    connection = _conn()
    try:
        active = connection.execute(
            "SELECT event_id,event_version FROM web_push_attention_events "
            "WHERE attention_class='approval_pending' AND acknowledged_at IS NULL "
            "AND canceled_at IS NULL"
        ).fetchall()
    finally:
        connection.close()
    for event in active:
        approval_id = event["event_id"].removeprefix("approval:")
        truth = approval_truth.get(approval_id)
        if truth is None or truth.get("result") is not None:
            _cancel_attention(
                event["event_id"], event["event_version"],
                "source_no_longer_pending",
            )


async def reconcile_approval_attention(eligible_kind) -> None:
    await asyncio.to_thread(_reconcile_approval_attention_sync, eligible_kind)


def cancel_approval(approval_id: str, reason: str = "approval_decided") -> None:
    _cancel_attention(f"approval:{approval_id}", 1, reason)
    wake_worker()


def _cancel_attention(
    event_id: str, event_version: int, reason: str,
    *, owner_id: str | None = None, db_path: Path | str | None = None,
) -> bool:
    now = time.time()
    connection = _conn(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        owner_clause = " AND owner_id=?" if owner_id else ""
        args = [now, reason, event_id, event_version]
        if owner_id:
            args.append(owner_id)
        changed = connection.execute(
            "UPDATE web_push_attention_events SET canceled_at=?,cancel_reason=? "
            "WHERE event_id=? AND event_version=?" + owner_clause
            + " AND canceled_at IS NULL AND acknowledged_at IS NULL",
            args,
        ).rowcount
        outbox_args = [reason, event_id, event_version]
        if owner_id:
            outbox_args.append(owner_id)
        connection.execute(
            "UPDATE web_push_outbox SET state='canceled',last_reason=? "
            "WHERE event_id=? AND event_version=?" + owner_clause
            + " AND state IN ('fallback_wait','pending','retry_wait','leased')",
            outbox_args,
        )
        connection.commit()
        return bool(changed)
    finally:
        connection.close()


def _ack_attention(
    event_id: str, event_version: int, owner_id: str,
    *, db_path: Path | str | None = None,
) -> bool:
    now = time.time()
    connection = _conn(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            "UPDATE web_push_attention_events SET acknowledged_at=? "
            "WHERE event_id=? AND event_version=? AND owner_id=? "
            "AND acknowledged_at IS NULL AND canceled_at IS NULL",
            (now, event_id, event_version, owner_id),
        ).rowcount
        connection.execute(
            "UPDATE web_push_outbox SET state='canceled',last_reason='foreground_applied' "
            "WHERE event_id=? AND event_version=? AND owner_id=? "
            "AND state IN ('fallback_wait','pending','retry_wait','leased')",
            (event_id, event_version, owner_id),
        )
        connection.commit()
        return bool(changed)
    finally:
        connection.close()


def _reserve_budget(connection: sqlite3.Connection, row: sqlite3.Row, now: float) -> bool:
    event = connection.execute(
        "SELECT budget_class,budget_reserved_at FROM web_push_attention_events "
        "WHERE event_id=? AND event_version=? AND owner_id=?",
        (row["event_id"], row["event_version"], row["owner_id"]),
    ).fetchone()
    if event is None:
        return False
    if event["budget_reserved_at"] is not None:
        return True
    limit = _BUDGET_LIMITS.get(event["budget_class"], 6)
    recent = connection.execute(
        "SELECT budget_reserved_at FROM web_push_attention_events "
        "WHERE owner_id=? AND budget_class=? AND budget_reserved_at>? "
        "ORDER BY budget_reserved_at",
        (row["owner_id"], event["budget_class"], now - 3600),
    ).fetchall()
    if len(recent) >= limit:
        next_allowed = float(recent[0]["budget_reserved_at"]) + 3600
        connection.execute(
            "UPDATE web_push_outbox SET state='retry_wait',available_at=?,"
            "last_reason='budget_wait' WHERE event_id=? AND event_version=? "
            "AND owner_id=? AND state IN ('fallback_wait','pending','retry_wait')",
            (next_allowed, row["event_id"], row["event_version"], row["owner_id"]),
        )
        return False
    connection.execute(
        "UPDATE web_push_attention_events SET budget_reserved_at=? "
        "WHERE event_id=? AND event_version=? AND owner_id=? "
        "AND budget_reserved_at IS NULL",
        (now, row["event_id"], row["event_version"], row["owner_id"]),
    )
    return True


def _claim_due(*, db_path: Path | str | None = None) -> dict | None:
    now = time.time()
    lease_owner = uuid.uuid4().hex
    connection = _conn(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE web_push_outbox SET state='pending',lease_owner=NULL,lease_until=NULL "
            "WHERE state='leased' AND lease_until<?", (now,),
        )
        row = connection.execute(
            "SELECT o.* FROM web_push_outbox o JOIN web_push_attention_events e "
            "ON e.event_id=o.event_id AND e.event_version=o.event_version "
            "AND e.owner_id=o.owner_id JOIN web_push_subscriptions s "
            "ON s.device_id=o.installation_id "
            "WHERE o.state IN ('fallback_wait','pending','retry_wait') "
            "AND o.available_at<=? AND o.expires_at>? AND e.acknowledged_at IS NULL "
            "AND e.canceled_at IS NULL AND s.status='active' "
            "ORDER BY o.available_at,o.id LIMIT 1",
            (now, now),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        if not _reserve_budget(connection, row, now):
            connection.commit()
            return None
        changed = connection.execute(
            "UPDATE web_push_outbox SET state='leased',lease_owner=?,lease_until=?,"
            "attempt_count=attempt_count+1 WHERE id=? AND state=?",
            (lease_owner, now + _LEASE_SECONDS, row["id"], row["state"]),
        ).rowcount
        if not changed:
            connection.rollback()
            return None
        claimed = connection.execute(
            "SELECT o.*,s.vapid_subject AS origin,s.endpoint,s.p256dh,s.auth_secret,"
            "s.vapid_key_id,"
            "e.application,e.attention_class,e.route,e.created_at "
            "FROM web_push_outbox o JOIN web_push_subscriptions s "
            "ON s.device_id=o.installation_id "
            "JOIN web_push_attention_events e ON e.event_id=o.event_id "
            "AND e.event_version=o.event_version AND e.owner_id=o.owner_id "
            "WHERE o.id=?", (row["id"],),
        ).fetchone()
        connection.commit()
        result = dict(claimed)
        result["lease_owner"] = lease_owner
        return result
    finally:
        connection.close()


def _host_has_only_public_addresses(host: str) -> bool:
    try:
        answers = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    addresses = {answer[4][0].split("%", 1)[0] for answer in answers}
    if not addresses:
        return False
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_global:
                return False
        except ValueError:
            return False
    return True


def _lease_still_sendable(
    row: dict, *, db_path: Path | str | None = None,
) -> bool:
    """Final source/ack/subscription guard immediately before network I/O."""

    connection = _conn(db_path)
    try:
        current = connection.execute(
            "SELECT o.state,o.lease_owner,o.expires_at,e.acknowledged_at,"
            "e.canceled_at,s.status FROM web_push_outbox o "
            "JOIN web_push_attention_events e ON e.event_id=o.event_id "
            "AND e.event_version=o.event_version AND e.owner_id=o.owner_id "
            "JOIN web_push_subscriptions s ON s.device_id=o.installation_id "
            "WHERE o.id=?", (row["id"],),
        ).fetchone()
        return bool(
            current
            and current["state"] == "leased"
            and current["lease_owner"] == row["lease_owner"]
            and current["expires_at"] > time.time()
            and current["acknowledged_at"] is None
            and current["canceled_at"] is None
            and current["status"] == "active"
        )
    finally:
        connection.close()


def _cancel_stale_lease(
    row: dict, *, db_path: Path | str | None = None,
) -> None:
    connection = _conn(db_path)
    try:
        connection.execute(
            "UPDATE web_push_outbox SET state='canceled',lease_owner=NULL,"
            "lease_until=NULL,last_reason='final_guard' WHERE id=? "
            "AND state='leased' AND lease_owner=?",
            (row["id"], row["lease_owner"]),
        )
        connection.commit()
    finally:
        connection.close()


def _payload_for(row: dict) -> str:
    digest = _b64url(hashlib.sha256(
        f"{row['event_id']}:{row['event_version']}".encode()
    ).digest())[:32]
    if row["attention_class"] == "device_alert_test":
        title = "Autonomy device alerts are ready"
        body = "This installed app can receive background notifications."
    else:
        title = "Autonomy needs your attention"
        body = "Open the dashboard to review."
    payload = {
        "v": 1,
        "event_id": digest,
        "class": row["attention_class"],
        "title": title,
        "body": body,
        "route": row["route"],
        "tag": f"attention:{digest}",
        "issued_at": int(row["created_at"]),
        "expires_at": int(row["expires_at"]),
    }
    wire = json.dumps(payload, separators=(",", ":"))
    if len(wire.encode()) > 2048:
        raise ValueError("Web Push payload exceeds the 2048-byte policy limit")
    return wire


def _send_push(row: dict) -> int:
    try:
        import requests
        from pywebpush import webpush
    except ImportError as exc:
        raise RuntimeError(
            "Web Push runtime is unavailable; install deploy/requirements.txt"
        ) from exc
    parsed = urlsplit(row["endpoint"])
    host = (parsed.hostname or "").rstrip(".").lower()
    if not _valid_push_host(host) or not _host_has_only_public_addresses(host):
        raise PermissionError("push endpoint failed the public-service egress policy")

    class NoRedirectSession(requests.Session):
        def request(self, *args, **kwargs):
            kwargs["allow_redirects"] = False
            return super().request(*args, **kwargs)

    session = NoRedirectSession()
    session.trust_env = False
    topic = _b64url(hashlib.sha256(
        f"{row['event_id']}:{row['event_version']}".encode()
    ).digest())[:32]
    response = webpush(
        subscription_info={
            "endpoint": row["endpoint"],
            "keys": {"p256dh": row["p256dh"], "auth": row["auth_secret"]},
        },
        data=_payload_for(row),
        vapid_private_key=_load_vapid(row["vapid_key_id"]),
        vapid_claims={"sub": row["origin"]},
        content_encoding="aes128gcm",
        ttl=max(0, min(int(row["expires_at"] - time.time()), 86400)),
        timeout=10,
        headers={"Urgency": "normal", "Topic": topic},
        requests_session=session,
    )
    return int(response.status_code)


def _failure_status(exc: Exception) -> tuple[int | None, str | None, float | None]:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if not isinstance(status, int) or not 100 <= status <= 599:
        status = None
    reason = None
    try:
        payload = response.json() if response is not None else None
        candidate = payload.get("reason") if isinstance(payload, dict) else None
        if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", candidate):
            reason = candidate
    except Exception:
        pass
    retry_after = None
    if response is not None:
        try:
            retry_after = float(response.headers.get("Retry-After"))
        except (TypeError, ValueError):
            retry_after = None
    return status, reason, retry_after


def _finish_attempt(
    row: dict, *, status: int | None, reason: str | None,
    retry_after: float | None = None, error: Exception | None = None,
    db_path: Path | str | None = None,
) -> None:
    now = time.time()
    accepted = status is not None and 200 <= status < 300
    retired = status in {404, 410}
    retryable = status in {408, 425, 429} or (status is not None and status >= 500)
    if status is None and error is not None and not isinstance(error, (PermissionError, ValueError)):
        retryable = True
    connection = _conn(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT state,lease_owner,attempt_count,expires_at FROM web_push_outbox "
            "WHERE id=?", (row["id"],),
        ).fetchone()
        if current is None or current["state"] != "leased" \
                or current["lease_owner"] != row["lease_owner"]:
            connection.rollback()
            return
        if accepted:
            outcome, state, available_at = "accepted", "accepted", now
        elif retired:
            outcome, state, available_at = "retired", "canceled", now
        elif retryable and current["attempt_count"] < _MAX_ATTEMPTS \
                and current["expires_at"] > now:
            delay = min(3600.0, 5.0 * (2 ** max(0, current["attempt_count"] - 1)))
            delay = random.uniform(0, delay)
            if retry_after is not None:
                delay = max(delay, min(max(0.0, retry_after), 3600.0))
            outcome, state, available_at = "retry", "retry_wait", now + delay
        else:
            outcome, state, available_at = "failed", "failed", now
        safe_reason = reason or (type(error).__name__ if error is not None else None)
        connection.execute(
            "UPDATE web_push_outbox SET state=?,available_at=?,lease_owner=NULL,"
            "lease_until=NULL,accepted_at=?,last_status=?,last_reason=? WHERE id=?",
            (
                state, available_at, now if accepted else None, status,
                safe_reason, row["id"],
            ),
        )
        connection.execute(
            "INSERT INTO web_push_attempts(outbox_id,attempted_at,outcome,status,reason) "
            "VALUES(?,?,?,?,?)", (row["id"], now, outcome, status, safe_reason),
        )
        if retired:
            connection.execute(
                "UPDATE web_push_subscriptions SET status='retired',retired_at=?,"
                "retire_reason='push_service_gone',updated_at=? WHERE device_id=?",
                (now, now, row["installation_id"]),
            )
            connection.execute(
                "UPDATE web_push_outbox SET state='canceled',"
                "last_reason='subscription_retired' WHERE installation_id=? "
                "AND state IN ('fallback_wait','pending','retry_wait','leased')",
                (row["installation_id"],),
            )
        connection.commit()
    finally:
        connection.close()


def _cleanup(*, db_path: Path | str | None = None) -> None:
    cutoff = time.time() - 7 * 86400
    connection = _conn(db_path)
    try:
        connection.execute(
            "DELETE FROM web_push_attempts WHERE attempted_at<?", (cutoff,)
        )
        connection.execute(
            "DELETE FROM web_push_outbox WHERE (accepted_at IS NOT NULL AND accepted_at<?) "
            "OR (expires_at<? AND state IN ('canceled','failed','accepted'))",
            (cutoff, cutoff),
        )
        connection.execute(
            "DELETE FROM web_push_attention_events WHERE expires_at<? AND NOT EXISTS "
            "(SELECT 1 FROM web_push_outbox o WHERE o.event_id=web_push_attention_events.event_id "
            "AND o.event_version=web_push_attention_events.event_version "
            "AND o.owner_id=web_push_attention_events.owner_id)", (cutoff,),
        )
        connection.commit()
    finally:
        connection.close()


async def _worker_loop() -> None:
    assert _worker_stop is not None and _worker_wake is not None
    last_cleanup = 0.0
    while not _worker_stop.is_set():
        row = await asyncio.to_thread(_claim_due)
        if row is not None:
            if not await asyncio.to_thread(_lease_still_sendable, row):
                await asyncio.to_thread(_cancel_stale_lease, row)
                continue
            try:
                status = await asyncio.to_thread(_send_push, row)
            except Exception as exc:
                status, reason, retry_after = _failure_status(exc)
                logger.warning(
                    "web_push_send_failed endpoint=%s status=%s reason=%s error_type=%s",
                    hashlib.sha256(row["endpoint"].encode()).hexdigest()[:12],
                    status, reason, type(exc).__name__,
                )
                await asyncio.to_thread(
                    _finish_attempt, row, status=status, reason=reason,
                    retry_after=retry_after, error=exc,
                )
            else:
                await asyncio.to_thread(
                    _finish_attempt, row, status=status, reason=None,
                )
            continue
        if time.time() - last_cleanup > 3600:
            await asyncio.to_thread(_cleanup)
            last_cleanup = time.time()
        try:
            await asyncio.wait_for(_worker_wake.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        _worker_wake.clear()


async def start_worker() -> None:
    global _worker_task, _worker_wake, _worker_stop, _worker_loop_ref
    if _worker_task is not None and not _worker_task.done():
        return
    try:
        await asyncio.to_thread(init_db)
        await asyncio.to_thread(_application_server_key)
    except Exception as exc:
        logger.error(
            "web_push_worker_disabled error_type=%s", type(exc).__name__,
        )
        return
    _worker_wake = asyncio.Event()
    _worker_stop = asyncio.Event()
    _worker_loop_ref = asyncio.get_running_loop()
    _worker_task = asyncio.create_task(_worker_loop(), name="web-push-worker")


async def stop_worker() -> None:
    global _worker_task, _worker_wake, _worker_stop, _worker_loop_ref
    if _worker_task is None:
        return
    if _worker_stop is not None:
        _worker_stop.set()
    if _worker_wake is not None:
        _worker_wake.set()
    await _worker_task
    _worker_task = None
    _worker_wake = None
    _worker_stop = None
    _worker_loop_ref = None


def wake_worker() -> None:
    if _worker_wake is not None:
        try:
            same_loop = asyncio.get_running_loop() is _worker_loop_ref
        except RuntimeError:
            same_loop = False
        if same_loop or _worker_loop_ref is None:
            _worker_wake.set()
        else:
            _worker_loop_ref.call_soon_threadsafe(_worker_wake.set)


async def api_config(request: Request) -> JSONResponse:
    refused = _operator_only(request)
    if refused is not None:
        return refused
    try:
        _validated_request_origin(request)
        key = await asyncio.to_thread(_application_server_key)
    except (OSError, RuntimeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    return JSONResponse({"ok": True, "application_server_key": key})


async def api_state(request: Request) -> JSONResponse:
    refused = _operator_only(request)
    if refused is not None:
        return refused
    try:
        owner_id = await asyncio.to_thread(_stable_owner_id)
        installation_id = request.query_params.get("installation_id")
        state = await asyncio.to_thread(
            _subscription_state, owner_id, installation_id,
        )
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    return JSONResponse({"ok": True, **state})


async def api_enroll(request: Request) -> JSONResponse:
    refused = _operator_only(request)
    if refused is not None:
        return refused
    try:
        origin = _validated_request_origin(request)
        body = _json_request(request, await request.body())
        if set(body) != {"installation_id", "subscription"}:
            raise ValueError("body must contain only installation_id and subscription")
        if not isinstance(body["installation_id"], str):
            raise ValueError("installation_id is invalid")
        subscription = _validate_subscription(body["subscription"])
        owner_id = await asyncio.to_thread(_stable_owner_id)
        await asyncio.to_thread(
            _upsert_subscription, owner_id=owner_id, origin=origin,
            installation_id=body["installation_id"], subscription=subscription,
        )
    except TypeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=415)
    except OverflowError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=413)
    except PermissionError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    except (RuntimeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    return JSONResponse({"ok": True, "state": "subscribed"})


async def api_retire(request: Request) -> JSONResponse:
    refused = _operator_only(request)
    if refused is not None:
        return refused
    installation_id = request.path_params["installation_id"]
    if not _INSTALLATION_ID.fullmatch(installation_id):
        return JSONResponse({"ok": False, "error": "installation_id is invalid"}, status_code=422)
    try:
        _validated_request_origin(request)
        owner_id = await asyncio.to_thread(_stable_owner_id)
        retired = await asyncio.to_thread(
            _retire_subscription, installation_id, owner_id, "operator_retired",
        )
    except (RuntimeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    wake_worker()
    return JSONResponse({"ok": True, "retired": retired})


async def api_ack(request: Request) -> JSONResponse:
    refused = _operator_only(request)
    if refused is not None:
        return refused
    approval_id = request.path_params["event_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", approval_id):
        return JSONResponse({"ok": False, "error": "event_id is invalid"}, status_code=422)
    try:
        _validated_request_origin(request)
        body = _json_request(request, await request.body())
        if body != {"event_version": 1, "applied": True}:
            raise ValueError("ack must contain event_version=1 and applied=true")
        owner_id = await asyncio.to_thread(_stable_owner_id)
        acknowledged = await asyncio.to_thread(
            _ack_attention, f"approval:{approval_id}", 1, owner_id,
        )
    except TypeError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=415)
    except (RuntimeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    wake_worker()
    return JSONResponse({"ok": True, "acknowledged": acknowledged})


async def api_test(request: Request) -> JSONResponse:
    refused = _operator_only(request)
    if refused is not None:
        return refused
    try:
        _validated_request_origin(request)
        if await request.body() not in (b"", b"{}"):
            raise ValueError("test request body must be empty")
        event_id = f"diagnostic:{uuid.uuid4().hex}"
        count = await asyncio.to_thread(
            _register_attention,
            event_id=event_id, event_version=1, application="dashboard",
            attention_class="device_alert_test", route="/activity",
            coalesce_key="device-alert-test", delivery_class="normal",
            budget_class="operator_diagnostic", grace_seconds=0,
        )
    except (RuntimeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
    if not count:
        return JSONResponse(
            {"ok": False, "error": "no active device-alert subscription"},
            status_code=409,
        )
    wake_worker()
    return JSONResponse({
        "ok": True, "queued_installations": count,
        "meaning": "durably queued; push-service acceptance is not device delivery",
    })


ROUTES = [
    Route("/api/web-push/state", api_state, methods=["GET"]),
    Route("/api/web-push/subscriptions", api_enroll, methods=["POST"]),
    Route(
        "/api/web-push/subscriptions/{installation_id}", api_retire,
        methods=["DELETE"],
    ),
    Route(
        "/api/web-push/attention/{event_id}/ack", api_ack, methods=["POST"],
    ),
    Route("/api/web-push/test", api_test, methods=["POST"]),
]
