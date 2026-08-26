"""Durable Web Push projection beneath Central's delivery latch.

Central Settings remains the only authority for wait/due/released/applied
truth.  This module owns only per-installation transport work, leases,
interruption reservations, and the durable half of the final guard handshake.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
import hashlib
import math
import random
import re
import secrets
import sqlite3
import time
from typing import Any, Callable, Mapping

from tools.dashboard.dao.web_push import WebPushStore
from tools.graph.schemas.central_attention import (
    ATTENTION_DELIVERY_SET_ID,
    CENTRAL_ATTENTION_REVISION,
)
from tools.graph.schemas.registry import validate_payload


_OPAQUE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_OWNER = re.compile(r"^[a-f0-9]{64}$")
_NONTERMINAL = (
    "fallback_wait", "budget_wait", "pending", "leased",
    "guard_crossed", "retry_wait",
)
_DUE_LATCH_STATES = ("background_due", "background_released")
_TERMINAL_LATCH_STATES = ("foreground_applied", "ineligible", "expired")
_LEGAL_LATCH_TRANSITIONS = {
    "foreground_wait": {
        "foreground_wait", "background_due", "background_released",
        "foreground_applied", "ineligible", "expired",
    },
    "background_due": {
        "background_due", "background_released", "foreground_applied",
        "ineligible", "expired",
    },
    "background_released": {
        "background_released", "foreground_applied", "ineligible", "expired",
    },
    "foreground_applied": {"foreground_applied"},
    "ineligible": {"ineligible"},
    "expired": {"expired"},
}
_BUDGET_CAPACITY = {"operator_approval": 12}
_BUDGET_WINDOW_SECONDS = 3600.0
_LEASE_SECONDS = 60.0
_MAX_ATTEMPTS = 10
_TERMINAL_RETENTION_SECONDS = 7 * 86400.0
_TARGET_DOMAIN = b"autonomy:web-push-target:v1\0"


class WebPushDeliveryError(RuntimeError):
    """Bounded refusal from the transport projection."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code.replace("_", " "))


@dataclass(frozen=True, slots=True)
class GuardResult:
    status: str
    release_token: str | None = None
    available_at: float | None = None

    @classmethod
    def crossed(cls, token: str) -> "GuardResult":
        return cls("crossed", release_token=token)


@dataclass(frozen=True, slots=True)
class ReleaseAuthorization:
    delivery_id: str
    event_id: str
    target_id: str
    lease_token: str
    release_token: str


@dataclass(frozen=True, slots=True)
class ClaimedTarget:
    target_id: str
    owner_subject: str
    delivery_id: str
    event_id: str
    lease_token: str
    release_token: str | None
    attempt_count: int
    expires_at: float
    endpoint: str
    p256dh: str
    auth_secret: str
    vapid_key_id: str
    vapid_subject: str
    application_scope: str
    notification_class: str
    urgency: str
    privacy_renderer_id: str
    route_builder_id: str
    destination_id: str
    source_guard_ref: str
    latch_created_at: float


def _now(value: float | None = None) -> float:
    candidate = time.time() if value is None else value
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        raise WebPushDeliveryError("invalid_clock")
    try:
        result = float(candidate)
    except (OverflowError, ValueError, TypeError) as exc:
        raise WebPushDeliveryError("invalid_clock") from exc
    if not math.isfinite(result) or result < 0:
        raise WebPushDeliveryError("invalid_clock")
    return result


def _opaque(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _OPAQUE.fullmatch(value):
        raise WebPushDeliveryError("invalid_request", f"invalid {label}")
    return value


def _owner(value: Any) -> str:
    if not isinstance(value, str) or not _OWNER.fullmatch(value):
        raise WebPushDeliveryError("invalid_owner")
    return value


def _token() -> str:
    return secrets.token_urlsafe(32)


def _target_id(owner: str, delivery_id: str, subscription_id: str) -> str:
    digest = hashlib.sha256(
        _TARGET_DOMAIN
        + owner.encode("ascii") + b"\0"
        + delivery_id.encode("ascii") + b"\0"
        + subscription_id.encode("ascii")
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _validated_snapshot(delivery_id: Any, payload: Any) -> tuple[str, dict[str, Any]]:
    delivery = _opaque(delivery_id, "delivery ID")
    if not isinstance(payload, Mapping):
        raise WebPushDeliveryError("invalid_request")
    snapshot = dict(payload)
    try:
        validate_payload(
            ATTENTION_DELIVERY_SET_ID, CENTRAL_ATTENTION_REVISION, snapshot,
        )
    except Exception as exc:
        raise WebPushDeliveryError("invalid_request") from exc
    _opaque(snapshot.get("event_id"), "event ID")
    return delivery, snapshot


_IMMUTABLE_COLUMNS = {
    "event_id": "event_id",
    "attention_id": "attention_id",
    "source_version": "source_version",
    "application_scope": "application_scope",
    "notification_class": "notification_class",
    "class_policy_revision": "class_policy_revision",
    "delivery_class": "delivery_class",
    "budget_class": "budget_class",
    "coalesce_key": "coalesce_key",
    "urgency": "urgency",
    "privacy_renderer_id": "privacy_renderer_id",
    "route_builder_id": "route_builder_id",
    "destination_id": "destination_id",
    "created_at": "latch_created_at",
    "expires_at": "latch_expires_at",
}


class WebPushDeliveryStore:
    """Owner-bound SQLite adapter consumed by the Central coordinator."""

    def __init__(
        self,
        store: WebPushStore,
        *,
        owner_subject: str,
        clock: Callable[[], float] = time.time,
        token_factory: Callable[[], str] = _token,
        jitter: Callable[[float, float], float] = random.uniform,
    ):
        if not isinstance(store, WebPushStore):
            raise ValueError("store must be WebPushStore")
        if not callable(clock) or not callable(token_factory) or not callable(jitter):
            raise ValueError("delivery dependencies must be callable")
        self.store = store
        self.owner_subject = _owner(owner_subject)
        self._clock = clock
        self._token_factory = token_factory
        self._jitter = jitter

    def _instant(self) -> float:
        return _now(self._clock())

    @staticmethod
    def _preference(connection: sqlite3.Connection, owner: str, application: str) -> str:
        row = connection.execute(
            "SELECT mode FROM web_push_preferences "
            "WHERE operator_subject=? AND application=?",
            (owner, application),
        ).fetchone()
        return "off" if row is None else str(row["mode"])

    @staticmethod
    def _immutable_values(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        values = {column: snapshot[field] for field, column in _IMMUTABLE_COLUMNS.items()}
        guard = snapshot["source_guard"]
        values.update({
            "source_guard_kind": guard["kind"],
            "source_guard_ref": guard["ref"],
            "source_guard_version": guard["version"],
        })
        return values

    def project_latch(
        self, delivery_id: Any, payload: Any, *, now: float | None = None,
    ) -> int:
        """Idempotently project one current Central latch into transport rows."""

        delivery, snapshot = _validated_snapshot(delivery_id, payload)
        instant = self._instant() if now is None else _now(now)
        immutable = self._immutable_values(snapshot)
        connection = self.store.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM web_push_delivery_events "
                "WHERE owner_subject=? AND delivery_id=?",
                (self.owner_subject, delivery),
            ).fetchone()
            if existing is not None:
                if any(existing[column] != value for column, value in immutable.items()):
                    raise WebPushDeliveryError("identity_conflict")
                old_version = int(existing["latch_state_version"])
                new_version = int(snapshot["state_version"])
                if new_version < old_version:
                    connection.rollback()
                    return 0
                if new_version == old_version:
                    if (
                        existing["latch_state"] != snapshot["state"]
                        or existing["fallback_due_at"] != snapshot.get("fallback_due_at")
                        or float(existing["latch_updated_at"]) != float(snapshot["updated_at"])
                    ):
                        raise WebPushDeliveryError("state_conflict")
                    connection.execute(
                        "UPDATE web_push_delivery_events SET projected_at=? "
                        "WHERE owner_subject=? AND delivery_id=?",
                        (instant, self.owner_subject, delivery),
                    )
                else:
                    old_fallback = existing["fallback_due_at"]
                    new_fallback = snapshot.get("fallback_due_at")
                    if (
                        snapshot["state"] not in _LEGAL_LATCH_TRANSITIONS.get(
                            existing["latch_state"], frozenset(),
                        )
                        or float(snapshot["updated_at"]) < float(existing["latch_updated_at"])
                        or old_fallback != new_fallback
                    ):
                        raise WebPushDeliveryError("state_conflict")
                    connection.execute(
                        "UPDATE web_push_delivery_events SET latch_state=?,"
                        "latch_state_version=?,latch_updated_at=?,fallback_due_at=?,"
                        "projected_at=? WHERE owner_subject=? AND delivery_id=?",
                        (
                            snapshot["state"], new_version, snapshot["updated_at"],
                            snapshot.get("fallback_due_at"), instant,
                            self.owner_subject, delivery,
                        ),
                    )
            else:
                columns = [
                    "owner_subject", "delivery_id", *immutable.keys(), "latch_state",
                    "latch_state_version", "latch_updated_at", "fallback_due_at",
                    "projected_at",
                ]
                values = [
                    self.owner_subject, delivery, *immutable.values(), snapshot["state"],
                    snapshot["state_version"], snapshot["updated_at"],
                    snapshot.get("fallback_due_at"), instant,
                ]
                connection.execute(
                    f"INSERT INTO web_push_delivery_events({','.join(columns)}) "
                    f"VALUES({','.join('?' for _ in columns)})",
                    values,
                )

            if snapshot["state"] in _TERMINAL_LATCH_STATES:
                changed = self._cancel_rows(
                    connection, delivery, snapshot["event_id"], snapshot["state"], instant,
                )
                connection.commit()
                return changed

            subscriptions = connection.execute(
                "SELECT id,created_at FROM web_push_subscriptions "
                "WHERE operator_subject=? AND status='active' AND created_at<=?",
                (self.owner_subject, snapshot["created_at"]),
            ).fetchall()
            preference = self._preference(
                connection, self.owner_subject, snapshot["application_scope"],
            )
            target_state = (
                "fallback_wait" if snapshot["state"] == "foreground_wait" else "pending"
            )
            available_at = (
                snapshot["fallback_due_at"]
                if snapshot["state"] == "foreground_wait" else instant
            )
            inserted = 0
            for subscription in subscriptions:
                target_id = _target_id(self.owner_subject, delivery, subscription["id"])
                initial_state = target_state if preference != "off" else "canceled"
                reason = None if preference != "off" else "preference_off_at_projection"
                inserted += connection.execute(
                    "INSERT OR IGNORE INTO web_push_delivery_targets("
                    "target_id,owner_subject,delivery_id,event_id,subscription_id,state,"
                    "available_at,expires_at,last_reason,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        target_id, self.owner_subject, delivery, snapshot["event_id"],
                        subscription["id"], initial_state, available_at,
                        snapshot["expires_at"], reason, instant, instant,
                    ),
                ).rowcount
            if snapshot["state"] in _DUE_LATCH_STATES:
                connection.execute(
                    "UPDATE web_push_delivery_targets SET state='pending',available_at=?,"
                    "updated_at=? WHERE owner_subject=? AND delivery_id=? "
                    "AND state='fallback_wait'",
                    (instant, instant, self.owner_subject, delivery),
                )
            connection.commit()
            return inserted
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise WebPushDeliveryError("storage_conflict") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _cancel_rows(
        self,
        connection: sqlite3.Connection,
        delivery_id: str,
        event_id: str,
        reason: str,
        instant: float,
    ) -> int:
        placeholders = ",".join("?" for _ in _NONTERMINAL)
        return connection.execute(
            "UPDATE web_push_delivery_targets SET state='canceled',last_reason=?,"
            f"updated_at=? WHERE owner_subject=? AND delivery_id=? AND event_id=? "
            f"AND state IN ({placeholders})",
            (reason, instant, self.owner_subject, delivery_id, event_id, *_NONTERMINAL),
        ).rowcount

    def cancel_unsent(self, delivery_id: Any, event_id: Any, reason: Any) -> int:
        delivery = _opaque(delivery_id, "delivery ID")
        event = _opaque(event_id, "event ID")
        if (
            not isinstance(reason, str) or reason != reason.strip()
            or not 1 <= len(reason.encode("utf-8")) <= 160
        ):
            raise WebPushDeliveryError("invalid_request")
        instant = self._instant()
        connection = self.store.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = self._cancel_rows(connection, delivery, event, reason, instant)
            connection.commit()
            return changed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _capacity(budget_class: str) -> int:
        try:
            return _BUDGET_CAPACITY[budget_class]
        except (KeyError, TypeError) as exc:
            raise WebPushDeliveryError("unsupported_budget_class") from exc

    def _reserve_budget(
        self, connection: sqlite3.Connection, event: sqlite3.Row, instant: float,
    ) -> tuple[bool, float | None]:
        if event["budget_reserved_at"] is not None:
            try:
                reserved_at = float(event["budget_reserved_at"])
            except (TypeError, ValueError, OverflowError) as exc:
                raise WebPushDeliveryError("storage_unavailable") from exc
            if not math.isfinite(reserved_at) or not 0 <= reserved_at <= instant:
                raise WebPushDeliveryError("storage_unavailable")
            return True, None
        recent = connection.execute(
            "SELECT budget_reserved_at FROM web_push_delivery_events "
            "WHERE owner_subject=? AND budget_class=? AND budget_reserved_at>? "
            "ORDER BY budget_reserved_at",
            (
                self.owner_subject, event["budget_class"],
                instant - _BUDGET_WINDOW_SECONDS,
            ),
        ).fetchall()
        if len(recent) >= self._capacity(event["budget_class"]):
            return False, float(recent[0]["budget_reserved_at"]) + _BUDGET_WINDOW_SECONDS
        changed = connection.execute(
            "UPDATE web_push_delivery_events SET budget_reserved_at=? "
            "WHERE owner_subject=? AND delivery_id=? AND budget_reserved_at IS NULL",
            (instant, self.owner_subject, event["delivery_id"]),
        ).rowcount
        return changed == 1, None

    def _coalesce_budget_wait(
        self, connection: sqlite3.Connection, event: sqlite3.Row, available_at: float,
        instant: float,
    ) -> bool:
        newest = connection.execute(
            "SELECT delivery_id FROM web_push_delivery_events WHERE owner_subject=? "
            "AND application_scope=? AND coalesce_key=? AND latch_expires_at>? "
            "ORDER BY latch_created_at DESC,delivery_id DESC LIMIT 1",
            (
                self.owner_subject, event["application_scope"], event["coalesce_key"], instant,
            ),
        ).fetchone()
        if newest is None:
            return False
        if newest["delivery_id"] != event["delivery_id"]:
            self._cancel_rows(
                connection, event["delivery_id"], event["event_id"],
                "budget_superseded", instant,
            )
            return False
        older = connection.execute(
            "SELECT delivery_id,event_id FROM web_push_delivery_events "
            "WHERE owner_subject=? AND application_scope=? AND coalesce_key=? "
            "AND delivery_id<>? AND latch_created_at<=?",
            (
                self.owner_subject, event["application_scope"], event["coalesce_key"],
                event["delivery_id"], event["latch_created_at"],
            ),
        ).fetchall()
        for row in older:
            self._cancel_rows(
                connection, row["delivery_id"], row["event_id"],
                "budget_superseded", instant,
            )
        connection.execute(
            "UPDATE web_push_delivery_targets SET state='budget_wait',available_at=?,"
            "lease_token=NULL,lease_until=NULL,last_reason='budget_wait',updated_at=? "
            "WHERE owner_subject=? AND delivery_id=? "
            "AND state IN ('fallback_wait','pending','leased','retry_wait')",
            (available_at, instant, self.owner_subject, event["delivery_id"]),
        )
        return True

    def mark_target_guard_crossed(
        self,
        delivery_id: Any,
        event_id: Any,
        target_id: Any,
        lease_token: Any,
    ) -> GuardResult:
        delivery = _opaque(delivery_id, "delivery ID")
        event_id = _opaque(event_id, "event ID")
        target_id = _opaque(target_id, "target ID")
        lease_token = _opaque(lease_token, "lease token")
        instant = self._instant()
        connection = self.store.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT t.*,e.application_scope,e.budget_class,e.coalesce_key,"
                "e.latch_created_at,e.latch_expires_at,e.budget_reserved_at,"
                "s.status AS subscription_status,s.created_at AS subscription_created_at,"
                "s.expiration_time AS subscription_expiration_time "
                "FROM web_push_delivery_targets t "
                "JOIN web_push_delivery_events e ON e.owner_subject=t.owner_subject "
                "AND e.delivery_id=t.delivery_id "
                "JOIN web_push_subscriptions s ON s.id=t.subscription_id "
                "WHERE t.target_id=? AND t.owner_subject=? AND t.delivery_id=? "
                "AND t.event_id=?",
                (target_id, self.owner_subject, delivery, event_id),
            ).fetchone()
            if row is None or row["lease_token"] != lease_token:
                connection.rollback()
                return GuardResult("stale")
            if row["state"] not in {"leased", "guard_crossed"}:
                connection.rollback()
                return GuardResult("stale")
            try:
                subscription_created = float(row["subscription_created_at"])
                target_expires = float(row["expires_at"])
                latch_created = float(row["latch_created_at"])
                latch_expires = float(row["latch_expires_at"])
                subscription_expires = (
                    None if row["subscription_expiration_time"] is None
                    else float(row["subscription_expiration_time"])
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise WebPushDeliveryError("storage_unavailable") from exc
            timestamps = (
                subscription_created, target_expires, latch_created, latch_expires,
            ) + (() if subscription_expires is None else (subscription_expires,))
            if any(not math.isfinite(value) or value < 0 for value in timestamps):
                raise WebPushDeliveryError("storage_unavailable")
            if (
                row["subscription_status"] != "active"
                or subscription_created > latch_created
                or (subscription_expires is not None and subscription_expires <= instant)
                or target_expires <= instant
                or latch_expires <= instant
                or self._preference(
                    connection, self.owner_subject, row["application_scope"],
                ) == "off"
            ):
                connection.execute(
                    "UPDATE web_push_delivery_targets SET state='canceled',"
                    "last_reason='transport_ineligible',updated_at=? WHERE target_id=?",
                    (instant, target_id),
                )
                connection.commit()
                return GuardResult("ineligible")
            event = connection.execute(
                "SELECT * FROM web_push_delivery_events WHERE owner_subject=? "
                "AND delivery_id=?",
                (self.owner_subject, delivery),
            ).fetchone()
            reserved, next_allowed = self._reserve_budget(connection, event, instant)
            if not reserved:
                assert next_allowed is not None
                self._coalesce_budget_wait(connection, event, next_allowed, instant)
                connection.commit()
                return GuardResult("deferred", available_at=next_allowed)
            if row["state"] == "guard_crossed":
                release_token = row["release_token"]
                if not isinstance(release_token, str) or not _OPAQUE.fullmatch(release_token):
                    raise WebPushDeliveryError("storage_unavailable")
                connection.commit()
                return GuardResult.crossed(release_token)
            release_token = self._token_factory()
            _opaque(release_token, "release token")
            changed = connection.execute(
                "UPDATE web_push_delivery_targets SET state='guard_crossed',"
                "release_token=?,guard_crossed_at=?,updated_at=? WHERE target_id=? "
                "AND owner_subject=? AND delivery_id=? AND event_id=? "
                "AND state='leased' AND lease_token=?",
                (
                    release_token, instant, instant, target_id, self.owner_subject,
                    delivery, event_id, lease_token,
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return GuardResult("stale")
            connection.commit()
            return GuardResult.crossed(release_token)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _claimed(row: sqlite3.Row) -> ClaimedTarget:
        return ClaimedTarget(
            target_id=row["target_id"], owner_subject=row["owner_subject"],
            delivery_id=row["delivery_id"], event_id=row["event_id"],
            lease_token=row["lease_token"], release_token=row["release_token"],
            attempt_count=int(row["attempt_count"]), expires_at=float(row["expires_at"]),
            endpoint=row["endpoint"], p256dh=row["p256dh"],
            auth_secret=row["auth_secret"], vapid_key_id=row["vapid_key_id"],
            vapid_subject=row["vapid_subject"], application_scope=row["application_scope"],
            notification_class=row["notification_class"], urgency=row["urgency"],
            privacy_renderer_id=row["privacy_renderer_id"],
            route_builder_id=row["route_builder_id"], destination_id=row["destination_id"],
            source_guard_ref=row["source_guard_ref"],
            latch_created_at=float(row["latch_created_at"]),
        )

    @staticmethod
    def _claim_select(connection: sqlite3.Connection, where: str, args: tuple[Any, ...]):
        return connection.execute(
            "SELECT t.*,e.application_scope,e.notification_class,e.urgency,"
            "e.privacy_renderer_id,e.route_builder_id,e.destination_id,"
            "e.source_guard_ref,e.latch_created_at,s.endpoint,s.p256dh,s.auth_secret,"
            "s.vapid_key_id,s.vapid_subject FROM web_push_delivery_targets t "
            "JOIN web_push_delivery_events e ON e.owner_subject=t.owner_subject "
            "AND e.delivery_id=t.delivery_id JOIN web_push_subscriptions s "
            "ON s.id=t.subscription_id WHERE " + where
            + " ORDER BY t.available_at,t.created_at,t.target_id LIMIT 1",
            args,
        ).fetchone()

    def claim_next(self) -> ClaimedTarget | None:
        instant = self._instant()
        connection = self.store.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE web_push_delivery_targets SET state='expired',"
                "last_reason='transport_expired',lease_token=NULL,lease_until=NULL,"
                "release_token=NULL,guard_crossed_at=NULL,updated_at=? "
                "WHERE owner_subject=? AND expires_at<=? AND state IN "
                "('fallback_wait','budget_wait','pending','leased','guard_crossed',"
                "'retry_wait')",
                (instant, self.owner_subject, instant),
            )
            # A newly leased attempt consumes the bounded allowance even if
            # Central fails before writing a marker.  ``guard_crossed`` is
            # intentionally excluded: replaying that durable marker repairs
            # one existing lease/token and is not an eleventh send attempt.
            connection.execute(
                "UPDATE web_push_delivery_targets SET state='failed_permanent',"
                "last_reason='attempt_limit',lease_token=NULL,lease_until=NULL,"
                "release_token=NULL,guard_crossed_at=NULL,updated_at=? "
                "WHERE owner_subject=? AND attempt_count>=? AND ("
                "state IN ('pending','budget_wait','retry_wait') OR "
                "(state='leased' AND lease_until<=?))",
                (instant, self.owner_subject, _MAX_ATTEMPTS, instant),
            )
            connection.execute(
                "UPDATE web_push_delivery_targets SET state='pending',lease_token=NULL,"
                "lease_until=NULL,last_reason='lease_reclaimed',updated_at=? "
                "WHERE owner_subject=? AND state='leased' AND lease_until<=? "
                "AND attempt_count<?",
                (instant, self.owner_subject, instant, _MAX_ATTEMPTS),
            )
            marker = self._claim_select(
                connection,
                "t.owner_subject=? AND t.state='guard_crossed' AND t.available_at<=? "
                "AND t.expires_at>?",
                (self.owner_subject, instant, instant),
            )
            if marker is not None:
                connection.commit()
                return self._claimed(marker)
            row = self._claim_select(
                connection,
                "t.owner_subject=? AND t.state IN ('pending','budget_wait','retry_wait') "
                "AND t.available_at<=? AND t.expires_at>? "
                "AND t.attempt_count<? "
                "AND e.latch_state IN ('background_due','background_released') "
                "AND s.status='active'",
                (self.owner_subject, instant, instant, _MAX_ATTEMPTS),
            )
            if row is None:
                connection.commit()
                return None
            lease_token = self._token_factory()
            _opaque(lease_token, "lease token")
            changed = connection.execute(
                "UPDATE web_push_delivery_targets SET state='leased',lease_token=?,"
                "lease_until=?,release_token=NULL,guard_crossed_at=NULL,"
                "attempt_count=attempt_count+1,updated_at=? WHERE target_id=? "
                "AND state=? AND attempt_count<?",
                (
                    lease_token, instant + _LEASE_SECONDS, instant,
                    row["target_id"], row["state"], _MAX_ATTEMPTS,
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return None
            claimed = self._claim_select(
                connection, "t.target_id=?", (row["target_id"],),
            )
            connection.commit()
            return self._claimed(claimed)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def defer_marker(self, claim: ClaimedTarget, *, seconds: float = 1.0) -> None:
        delay = _now(seconds)
        instant = self._instant()
        connection = self.store.connect()
        try:
            arguments: list[Any] = [
                instant + delay, instant, claim.target_id, self.owner_subject,
                claim.delivery_id, claim.event_id, claim.lease_token,
            ]
            release_predicate = ""
            if claim.release_token is not None:
                release_predicate = " AND release_token=?"
                arguments.append(claim.release_token)
            connection.execute(
                "UPDATE web_push_delivery_targets SET available_at=?,updated_at=? "
                "WHERE target_id=? AND owner_subject=? AND delivery_id=? AND event_id=? "
                "AND state='guard_crossed' AND lease_token=?" + release_predicate,
                arguments,
            )
            connection.commit()
        finally:
            connection.close()

    def authorized_claim(
        self, claim: ClaimedTarget, authorization: ReleaseAuthorization,
    ) -> ClaimedTarget | None:
        """Prove the Central authorization names the exact durable marker."""

        if (
            not isinstance(claim, ClaimedTarget)
            or not isinstance(authorization, ReleaseAuthorization)
            or authorization.delivery_id != claim.delivery_id
            or authorization.event_id != claim.event_id
            or authorization.target_id != claim.target_id
            or authorization.lease_token != claim.lease_token
        ):
            return None
        try:
            _opaque(authorization.release_token, "release token")
        except WebPushDeliveryError:
            return None
        instant = self._instant()
        connection = self.store.connect()
        try:
            row = connection.execute(
                "SELECT release_token FROM web_push_delivery_targets "
                "WHERE target_id=? AND owner_subject=? AND delivery_id=? AND event_id=? "
                "AND state='guard_crossed' AND lease_token=? AND release_token=? "
                "AND expires_at>?",
                (
                    claim.target_id, self.owner_subject, claim.delivery_id,
                    claim.event_id, claim.lease_token, authorization.release_token,
                    instant,
                ),
            ).fetchone()
            if row is None:
                return None
            return replace(claim, release_token=authorization.release_token)
        finally:
            connection.close()

    def finish_attempt(
        self,
        claim: ClaimedTarget,
        authorization: ReleaseAuthorization,
        *,
        status: int | None,
        outcome: str,
        retryable: bool,
        retire_subscription: bool = False,
        retry_after: float | None = None,
        reason: str | None = None,
    ) -> bool:
        if (
            not isinstance(authorization, ReleaseAuthorization)
            or authorization.delivery_id != claim.delivery_id
            or authorization.event_id != claim.event_id
            or authorization.target_id != claim.target_id
            or authorization.lease_token != claim.lease_token
            or authorization.release_token != claim.release_token
        ):
            raise WebPushDeliveryError("invalid_authorization")
        instant = self._instant()
        connection = self.store.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT state,attempt_count,expires_at,subscription_id,lease_token,"
                "release_token FROM web_push_delivery_targets WHERE target_id=? "
                "AND owner_subject=?",
                (claim.target_id, self.owner_subject),
            ).fetchone()
            if (
                current is None or current["lease_token"] != claim.lease_token
                or current["release_token"] != claim.release_token
            ):
                connection.rollback()
                return False
            was_canceled = current["state"] == "canceled"
            if outcome == "accepted":
                next_state, available_at = "accepted", instant
            elif retire_subscription:
                next_state, available_at = "canceled", instant
            elif (
                retryable and not was_canceled
                and int(current["attempt_count"]) < _MAX_ATTEMPTS
                and float(current["expires_at"]) > instant
            ):
                ceiling = min(
                    3600.0,
                    5.0 * (2 ** max(0, int(current["attempt_count"]) - 1)),
                )
                delay = self._jitter(0.0, ceiling)
                if isinstance(delay, bool) or not isinstance(delay, (int, float)):
                    raise WebPushDeliveryError("invalid_backoff")
                try:
                    delay = float(delay)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise WebPushDeliveryError("invalid_backoff") from exc
                if not math.isfinite(delay) or not 0 <= delay <= ceiling:
                    raise WebPushDeliveryError("invalid_backoff")
                if retry_after is not None:
                    retry_value = _now(retry_after)
                    delay = max(delay, min(retry_value, 3600.0))
                next_state, available_at = "retry_wait", instant + delay
            elif was_canceled:
                next_state, available_at = "canceled", instant
            else:
                next_state, available_at = "failed_permanent", instant
            safe_reason = reason
            if safe_reason is not None and (
                not isinstance(safe_reason, str)
                or safe_reason != safe_reason.strip()
                or len(safe_reason.encode("utf-8")) > 160
            ):
                safe_reason = "bounded_failure"
            connection.execute(
                "UPDATE web_push_delivery_targets SET state=?,available_at=?,"
                "lease_token=NULL,lease_until=NULL,release_token=NULL,"
                "guard_crossed_at=NULL,accepted_at=?,last_status=?,last_reason=?,"
                "updated_at=? WHERE target_id=?",
                (
                    next_state, available_at, instant if outcome == "accepted" else None,
                    status, safe_reason or outcome, instant, claim.target_id,
                ),
            )
            connection.execute(
                "INSERT INTO web_push_delivery_attempts(target_id,release_token,"
                "attempted_at,outcome,status,reason) VALUES(?,?,?,?,?,?)",
                (
                    claim.target_id, authorization.release_token, instant,
                    outcome, status, safe_reason,
                ),
            )
            if retire_subscription:
                connection.execute(
                    "UPDATE web_push_subscriptions SET status='retired',retired_at=?,"
                    "retire_reason='push_service_gone',updated_at=? "
                    "WHERE id=? AND operator_subject=? AND status='active'",
                    (
                        instant, instant, current["subscription_id"], self.owner_subject,
                    ),
                )
                placeholders = ",".join("?" for _ in _NONTERMINAL)
                connection.execute(
                    "UPDATE web_push_delivery_targets SET state='canceled',"
                    "last_reason='subscription_retired',updated_at=? "
                    "WHERE owner_subject=? AND subscription_id=? "
                    f"AND state IN ({placeholders})",
                    (
                        instant, self.owner_subject, current["subscription_id"],
                        *_NONTERMINAL,
                    ),
                )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def cleanup(self) -> tuple[int, int]:
        instant = self._instant()
        cutoff = instant - _TERMINAL_RETENTION_SECONDS
        connection = self.store.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            attempts = connection.execute(
                "DELETE FROM web_push_delivery_attempts WHERE attempted_at<?", (cutoff,),
            ).rowcount
            targets = connection.execute(
                "DELETE FROM web_push_delivery_targets WHERE updated_at<? "
                "AND state IN ('accepted','failed_permanent','canceled','expired')",
                (cutoff,),
            ).rowcount
            connection.execute(
                "DELETE FROM web_push_delivery_events WHERE latch_expires_at<? "
                "AND NOT EXISTS (SELECT 1 FROM web_push_delivery_targets t "
                "WHERE t.owner_subject=web_push_delivery_events.owner_subject "
                "AND t.delivery_id=web_push_delivery_events.delivery_id)",
                (cutoff,),
            )
            connection.commit()
            return attempts, targets
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def diagnostics(self) -> dict[str, Any]:
        """Return bounded aggregate transport health without subscription secrets."""

        instant = self._instant()
        connection = self.store.connect()
        try:
            states = {
                row["state"]: int(row["count"])
                for row in connection.execute(
                    "SELECT state,count(*) AS count FROM web_push_delivery_targets "
                    "WHERE owner_subject=? GROUP BY state ORDER BY state",
                    (self.owner_subject,),
                ).fetchall()
            }
            oldest = connection.execute(
                "SELECT min(available_at) AS oldest FROM web_push_delivery_targets "
                "WHERE owner_subject=? AND state IN "
                "('fallback_wait','budget_wait','pending','retry_wait','leased','guard_crossed')",
                (self.owner_subject,),
            ).fetchone()["oldest"]
            attempts = {
                row["outcome"]: int(row["count"])
                for row in connection.execute(
                    "SELECT a.outcome,count(*) AS count FROM web_push_delivery_attempts a "
                    "JOIN web_push_delivery_targets t ON t.target_id=a.target_id "
                    "WHERE t.owner_subject=? AND a.attempted_at>? "
                    "GROUP BY a.outcome ORDER BY a.outcome",
                    (self.owner_subject, instant - 86400.0),
                ).fetchall()
            }
            reserved = connection.execute(
                "SELECT count(*) FROM web_push_delivery_events "
                "WHERE owner_subject=? AND budget_reserved_at>?",
                (self.owner_subject, instant - _BUDGET_WINDOW_SECONDS),
            ).fetchone()[0]
            return {
                "states": states,
                "oldest_due_age_seconds": (
                    None if oldest is None else max(0.0, instant - float(oldest))
                ),
                "attempts_24h": attempts,
                "budget_reservations_1h": int(reserved),
            }
        finally:
            connection.close()


__all__ = [
    "ClaimedTarget",
    "GuardResult",
    "ReleaseAuthorization",
    "WebPushDeliveryError",
    "WebPushDeliveryStore",
]
