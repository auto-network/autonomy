"""Trusted Settings mediator for the Central Attention index."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import threading
import unicodedata
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

from tools.dashboard.attention_registry import (
    MAX_SOURCE_VERSION,
    AttentionClassPolicy,
    AttentionIndexError,
    AttentionProjectionPlan,
    AttentionRegistry,
    AttentionSourceEvidence,
    RegisteredAttentionProducer,
    _bounded_version,
)
from tools.graph import settings_ops
from tools.graph.schemas.central_attention import (
    ATTENTION_APPLICATION_SET_ID,
    ATTENTION_ITEM_SET_ID,
    ATTENTION_PRESENTATION_SET_ID,
    CENTRAL_ATTENTION_REVISION,
)
from tools.graph.schemas.registry import validate_payload


logger = logging.getLogger(__name__)
MAX_ATTENTION_ID_BYTES = 128
MAX_CURSOR_BYTES = 4096
_STATES = ("needs_attention", "waiting", "resolved")
_CATEGORIES = ("apps", "comms", "approvals")
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_PROCESS_CURSOR_SECRET = secrets.token_bytes(32)


@dataclass(frozen=True, slots=True)
class AttentionApplicationRecord:
    application_scope: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AttentionItemRecord:
    attention_id: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AttentionPresentationRecord:
    attention_id: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AttentionChange:
    event_id: str
    attention_id: str
    source_version: int
    previous_state: str | None
    attention_state: str
    application_scope: str
    notification_class: str
    policy: AttentionClassPolicy
    review_renderer_id: str
    source_evidence: AttentionSourceEvidence


@dataclass(frozen=True, slots=True)
class AttentionQueryItem:
    attention_id: str
    payload: Mapping[str, Any]
    presentation: Mapping[str, Any] | None
    application_label: str
    icon_ref: str
    surface_category: str
    review_renderer_id: str


@dataclass(frozen=True, slots=True)
class AttentionQueryCounts:
    total_needs_attention: int
    categories: Mapping[str, int]
    states: Mapping[str, int]
    applications: Mapping[str, Mapping[str, int]]


@dataclass(frozen=True, slots=True)
class AttentionQueryResult:
    items: list[AttentionQueryItem]
    counts: AttentionQueryCounts
    next_cursor: str | None
    snapshot_version: str


class AttentionIndexStore(Protocol):
    def get_application(self, application_scope: str) -> AttentionApplicationRecord | None: ...
    def upsert_application(
        self, application_scope: str, payload: Mapping[str, Any],
    ) -> AttentionApplicationRecord: ...
    def get_item(self, attention_id: str) -> AttentionItemRecord | None: ...
    def upsert_item(
        self, attention_id: str, payload: Mapping[str, Any],
    ) -> AttentionItemRecord: ...
    def list_items(self) -> list[AttentionItemRecord]: ...
    def get_presentation(
        self, attention_id: str,
    ) -> AttentionPresentationRecord | None: ...
    def list_presentations(self) -> list[AttentionPresentationRecord]: ...


def _copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _validate_opaque_id(value: Any, *, maximum_bytes: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AttentionIndexError("invalid_request", "invalid attention ID")
    try:
        length = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise AttentionIndexError("invalid_request", "invalid attention ID") from exc
    if not 1 <= length <= maximum_bytes:
        raise AttentionIndexError("invalid_request", "invalid attention ID")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise AttentionIndexError("invalid_request", "invalid attention ID")
    return value


def _validate_attention_id(value: Any) -> str:
    return _validate_opaque_id(value, maximum_bytes=MAX_ATTENTION_ID_BYTES)


def _validate_presentation_id(value: Any) -> str:
    return _validate_opaque_id(value, maximum_bytes=256)


def _validate_version(value: Any) -> int:
    try:
        return _bounded_version(value)
    except Exception as exc:
        raise AttentionIndexError("invalid_request", "invalid source version") from exc


def _timestamp(value: Any) -> float:
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("invalid timestamp")
        answer = float(value)
    except Exception as exc:
        raise AttentionIndexError("invalid_request", "invalid timestamp") from exc
    if not math.isfinite(answer) or answer < 0:
        raise AttentionIndexError("invalid_request", "invalid timestamp")
    return answer


def _coherent_role_state(role: Any, state: Any) -> bool:
    return (
        role == "recipient" and state in {"needs_attention", "resolved"}
    ) or (
        role == "sender" and state in {"waiting", "resolved"}
    )


def canonical_attention_event_id(attention_id: Any, source_version: Any) -> str:
    key = _validate_attention_id(attention_id)
    version = _validate_version(source_version)
    encoded = json.dumps(
        ["dashboard.attention.event", 1, key, version],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _b64(hashlib.sha256(encoded).digest())


class SettingsAttentionIndexStore:
    """Personal raw Settings adapter for application, item, and presentation rows."""

    @staticmethod
    def _read_one(set_id: str, key: str, record_type: type):
        row = settings_ops.read_set_key(set_id, key, org=None, peers=[])
        if row is None:
            return None
        if not isinstance(row, dict) or row.get("key") != key:
            raise RuntimeError("stored attention row is malformed")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("stored attention payload is malformed")
        return record_type(key, _copy_json(payload))

    @staticmethod
    def _list(set_id: str, record_type: type) -> list:
        result = settings_ops.read_set(set_id, org=None, peers=[])
        if (
            not isinstance(result, settings_ops.SetMembers)
            or any(result.dropped.values())
        ):
            raise RuntimeError("stored attention set is a partial read")
        resolved = result.to_dict()
        records = []
        for key, row in resolved.items():
            payload = getattr(row, "payload", None)
            if not isinstance(key, str) or not isinstance(payload, dict):
                raise RuntimeError("stored attention set is malformed")
            records.append(record_type(key, _copy_json(payload)))
        return records

    def get_application(self, application_scope: str) -> AttentionApplicationRecord | None:
        return self._read_one(
            ATTENTION_APPLICATION_SET_ID, application_scope, AttentionApplicationRecord,
        )

    def upsert_application(
        self, application_scope: str, payload: Mapping[str, Any],
    ) -> AttentionApplicationRecord:
        clean = _copy_json(dict(payload))
        settings_ops.upsert_by_key(
            ATTENTION_APPLICATION_SET_ID,
            CENTRAL_ATTENTION_REVISION,
            application_scope,
            clean,
            org=None,
            state="raw",
        )
        return AttentionApplicationRecord(application_scope, _copy_json(clean))

    def get_item(self, attention_id: str) -> AttentionItemRecord | None:
        return self._read_one(ATTENTION_ITEM_SET_ID, attention_id, AttentionItemRecord)

    def upsert_item(
        self, attention_id: str, payload: Mapping[str, Any],
    ) -> AttentionItemRecord:
        clean = _copy_json(dict(payload))
        settings_ops.upsert_by_key(
            ATTENTION_ITEM_SET_ID,
            CENTRAL_ATTENTION_REVISION,
            attention_id,
            clean,
            org=None,
            state="raw",
        )
        return AttentionItemRecord(attention_id, _copy_json(clean))

    def list_items(self) -> list[AttentionItemRecord]:
        return self._list(ATTENTION_ITEM_SET_ID, AttentionItemRecord)

    def get_presentation(
        self, attention_id: str,
    ) -> AttentionPresentationRecord | None:
        return self._read_one(
            ATTENTION_PRESENTATION_SET_ID,
            attention_id,
            AttentionPresentationRecord,
        )

    def list_presentations(self) -> list[AttentionPresentationRecord]:
        return self._list(ATTENTION_PRESENTATION_SET_ID, AttentionPresentationRecord)


class InMemoryAttentionIndexStore:
    """Hermetic store used by focused mediator tests."""

    def __init__(self):
        self.applications: dict[str, dict[str, Any]] = {}
        self.items: dict[str, dict[str, Any]] = {}
        self.presentations: dict[str, dict[str, Any]] = {}
        self.application_writes: list[tuple[str, dict[str, Any]]] = []
        self.item_writes: list[tuple[str, dict[str, Any]]] = []
        self.fail_applications = False
        self.fail_items = False
        self.fail_presentations = False

    def get_application(self, application_scope: str) -> AttentionApplicationRecord | None:
        if self.fail_applications:
            raise RuntimeError("application read failed")
        payload = self.applications.get(application_scope)
        return None if payload is None else AttentionApplicationRecord(
            application_scope, _copy_json(payload),
        )

    def upsert_application(
        self, application_scope: str, payload: Mapping[str, Any],
    ) -> AttentionApplicationRecord:
        if self.fail_applications:
            raise RuntimeError("application write failed")
        clean = _copy_json(dict(payload))
        self.applications[application_scope] = clean
        self.application_writes.append((application_scope, _copy_json(clean)))
        return AttentionApplicationRecord(application_scope, _copy_json(clean))

    def get_item(self, attention_id: str) -> AttentionItemRecord | None:
        if self.fail_items:
            raise RuntimeError("item read failed")
        payload = self.items.get(attention_id)
        return None if payload is None else AttentionItemRecord(
            attention_id, _copy_json(payload),
        )

    def upsert_item(
        self, attention_id: str, payload: Mapping[str, Any],
    ) -> AttentionItemRecord:
        if self.fail_items:
            raise RuntimeError("item write failed")
        clean = _copy_json(dict(payload))
        self.items[attention_id] = clean
        self.item_writes.append((attention_id, _copy_json(clean)))
        return AttentionItemRecord(attention_id, _copy_json(clean))

    def list_items(self) -> list[AttentionItemRecord]:
        if self.fail_items:
            raise RuntimeError("item list failed")
        return [AttentionItemRecord(key, _copy_json(value)) for key, value in self.items.items()]

    def get_presentation(
        self, attention_id: str,
    ) -> AttentionPresentationRecord | None:
        if self.fail_presentations:
            raise RuntimeError("presentation read failed")
        payload = self.presentations.get(attention_id)
        return None if payload is None else AttentionPresentationRecord(
            attention_id, _copy_json(payload),
        )

    def list_presentations(self) -> list[AttentionPresentationRecord]:
        if self.fail_presentations:
            raise RuntimeError("presentation list failed")
        return [
            AttentionPresentationRecord(key, _copy_json(value))
            for key, value in self.presentations.items()
        ]


class _AttentionLocks:
    """Bounded process-wide locks shared by every mediator instance."""

    _locks = tuple(threading.RLock() for _ in range(257))

    @classmethod
    def for_id(cls, attention_id: str) -> threading.RLock:
        digest = hashlib.sha256(attention_id.encode("utf-8")).digest()
        return cls._locks[int.from_bytes(digest[:4], "big") % len(cls._locks)]


class AttentionIndexService:
    def __init__(
        self,
        *,
        registry: AttentionRegistry,
        store: AttentionIndexStore | None = None,
        after_commit: Callable[[AttentionChange], None] | None = None,
        cursor_secret: bytes | None = None,
    ):
        if not isinstance(registry, AttentionRegistry):
            raise ValueError("attention registry is required")
        secret = _PROCESS_CURSOR_SECRET if cursor_secret is None else cursor_secret
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("cursor secret must contain at least 32 bytes")
        if after_commit is not None and not callable(after_commit):
            raise ValueError("after-commit callback must be callable")
        self.registry = registry
        self.store = store or SettingsAttentionIndexStore()
        self._after_commit = after_commit
        self._cursor_secret = bytes(secret)

    def sync_registrations(self) -> int:
        writes = 0
        for projected in self.registry.application_records():
            payload = _copy_json(dict(projected.payload))
            try:
                validate_payload(
                    ATTENTION_APPLICATION_SET_ID, CENTRAL_ATTENTION_REVISION, payload,
                )
                current = self.store.get_application(projected.application_scope)
            except Exception as exc:
                raise AttentionIndexError("unavailable") from exc
            if current is not None:
                if (
                    not isinstance(current, AttentionApplicationRecord)
                    or current.application_scope != projected.application_scope
                    or not isinstance(current.payload, Mapping)
                ):
                    raise AttentionIndexError("unavailable")
                try:
                    current_payload = _copy_json(dict(current.payload))
                except Exception as exc:
                    raise AttentionIndexError("unavailable") from exc
                if current_payload == payload:
                    continue
            try:
                written = self.store.upsert_application(
                    projected.application_scope, payload,
                )
            except Exception as exc:
                raise AttentionIndexError("unavailable") from exc
            if (
                not isinstance(written, AttentionApplicationRecord)
                or written.application_scope != projected.application_scope
                or not isinstance(written.payload, Mapping)
            ):
                raise AttentionIndexError("unavailable")
            try:
                if _copy_json(dict(written.payload)) != payload:
                    raise AttentionIndexError("unavailable")
            except AttentionIndexError:
                raise
            except Exception as exc:
                raise AttentionIndexError("unavailable") from exc
            writes += 1
        return writes

    @staticmethod
    def _normalize_evidence(
        raw: Any, *, object_ref: str, source_version: int,
    ) -> AttentionSourceEvidence:
        if not isinstance(raw, AttentionSourceEvidence):
            raise AttentionIndexError("invalid_request", "invalid source evidence")
        guard = raw.source_guard
        if not isinstance(guard, Mapping) or set(guard) != {"kind", "ref", "version"}:
            raise AttentionIndexError("invalid_request", "invalid source guard")
        kind = guard.get("kind")
        ref = guard.get("ref")
        version = _validate_version(guard.get("version"))
        if (
            not isinstance(kind, str)
            or len(kind) > 64
            or not _IDENTIFIER_RE.fullmatch(kind)
            or not isinstance(ref, str)
            or not 1 <= len(ref) <= 256
            or any(ord(char) < 32 or ord(char) == 127 for char in ref)
            or ref != object_ref
            or version != source_version
        ):
            raise AttentionIndexError("invalid_request", "invalid source guard")
        expires = None
        if raw.source_expires_at is not None:
            expires = _timestamp(raw.source_expires_at)
        return AttentionSourceEvidence(
            source_guard=MappingProxyType({"kind": kind, "ref": ref, "version": version}),
            source_expires_at=expires,
        )

    @staticmethod
    def _plan_payload(producer: RegisteredAttentionProducer, source: Any):
        runtime = producer.registration.runtime
        if runtime is None or not runtime.publication_enabled:
            raise AttentionIndexError("class_disabled")
        try:
            plan = runtime.projection_planner(source)
        except AttentionIndexError:
            raise
        except Exception as exc:
            raise AttentionIndexError("invalid_request", "projection planning failed") from exc
        if not isinstance(plan, AttentionProjectionPlan):
            raise AttentionIndexError("invalid_request", "invalid projection plan")
        attention_id = _validate_attention_id(plan.attention_id)
        source_version = _validate_version(plan.source_version)
        occurred_at = _timestamp(plan.occurred_at)
        payload = {
            "application_scope": producer.registration.application_scope,
            "notification_class": producer.registration.notification_class,
            "object_ref": plan.object_ref,
            "participant_role": plan.participant_role,
            "attention_state": plan.attention_state,
            "safe_title": plan.safe_title,
            "occurred_at": occurred_at,
            "source_version": source_version,
        }
        if plan.safe_summary is not None:
            payload["safe_summary"] = plan.safe_summary
        if plan.counterparty_ref is not None:
            payload["counterparty_ref"] = plan.counterparty_ref
        if not _coherent_role_state(plan.participant_role, plan.attention_state):
            raise AttentionIndexError("invalid_request", "role/state mismatch")
        try:
            validate_payload(ATTENTION_ITEM_SET_ID, CENTRAL_ATTENTION_REVISION, payload)
            # The source plan must have one deterministic UTF-8 canonical form;
            # schema-valid Python strings are not necessarily UTF-8 encodable
            # (for example, an isolated surrogate).
            _canonical_bytes(payload)
        except Exception as exc:
            raise AttentionIndexError("invalid_request", "invalid attention projection") from exc
        try:
            raw_evidence = runtime.source_evidence_builder(
                payload["object_ref"], source_version,
            )
        except AttentionIndexError:
            raise
        except Exception as exc:
            raise AttentionIndexError("invalid_request", "source evidence failed") from exc
        evidence = AttentionIndexService._normalize_evidence(
            raw_evidence,
            object_ref=payload["object_ref"],
            source_version=source_version,
        )
        return attention_id, payload, evidence

    def publish(
        self, producer: RegisteredAttentionProducer, source: Any,
    ) -> AttentionItemRecord:
        if not self.registry.owns(producer):
            raise AttentionIndexError("invalid_request", "unregistered producer")
        attention_id, payload, evidence = self._plan_payload(producer, source)
        with _AttentionLocks.for_id(attention_id):
            try:
                current = self.store.get_item(attention_id)
            except Exception as exc:
                raise AttentionIndexError("unavailable") from exc
            previous_state = None
            if current is not None:
                if (
                    not isinstance(current, AttentionItemRecord)
                    or current.attention_id != attention_id
                    or not isinstance(current.payload, Mapping)
                ):
                    raise AttentionIndexError("unavailable")
                try:
                    current_payload = _copy_json(dict(current.payload))
                    validate_payload(
                        ATTENTION_ITEM_SET_ID,
                        CENTRAL_ATTENTION_REVISION,
                        current_payload,
                    )
                    old_version = _validate_version(current_payload.get("source_version"))
                    if not _coherent_role_state(
                        current_payload.get("participant_role"),
                        current_payload.get("attention_state"),
                    ):
                        raise ValueError("stored role/state mismatch")
                    current_canonical = _canonical_bytes(current_payload)
                except AttentionIndexError as exc:
                    raise AttentionIndexError("unavailable") from exc
                except Exception as exc:
                    raise AttentionIndexError("unavailable") from exc
                frozen = (
                    "application_scope", "notification_class", "object_ref",
                    "participant_role", "counterparty_ref",
                )
                if any(current_payload.get(name) != payload.get(name) for name in frozen):
                    raise AttentionIndexError("identity_conflict")
                new_version = payload["source_version"]
                if new_version < old_version:
                    raise AttentionIndexError("stale_source")
                if new_version == old_version:
                    if current_canonical == _canonical_bytes(payload):
                        return AttentionItemRecord(attention_id, _copy_json(current_payload))
                    raise AttentionIndexError("source_conflict")
                previous_state = current_payload["attention_state"]
            try:
                written = self.store.upsert_item(attention_id, payload)
            except Exception as exc:
                raise AttentionIndexError("unavailable") from exc
            if (
                not isinstance(written, AttentionItemRecord)
                or written.attention_id != attention_id
                or not isinstance(written.payload, Mapping)
            ):
                raise AttentionIndexError("unavailable")
            try:
                if _canonical_bytes(dict(written.payload)) != _canonical_bytes(payload):
                    raise AttentionIndexError("unavailable")
            except AttentionIndexError:
                raise
            except Exception as exc:
                raise AttentionIndexError("unavailable") from exc

        change = AttentionChange(
            event_id=canonical_attention_event_id(attention_id, payload["source_version"]),
            attention_id=attention_id,
            source_version=payload["source_version"],
            previous_state=previous_state,
            attention_state=payload["attention_state"],
            application_scope=producer.registration.application_scope,
            notification_class=producer.registration.notification_class,
            policy=producer.registration.policy,
            review_renderer_id=producer.registration.review_renderer_id,
            source_evidence=evidence,
        )
        if self._after_commit is not None:
            try:
                self._after_commit(change)
            except Exception:
                logger.exception("central attention after-commit callback failed")
        return AttentionItemRecord(attention_id, _copy_json(payload))

    def source_evidence_for(self, item: AttentionItemRecord) -> AttentionSourceEvidence:
        if (
            not isinstance(item, AttentionItemRecord)
            or not isinstance(item.payload, Mapping)
        ):
            raise AttentionIndexError("invalid_request")
        try:
            payload = _copy_json(dict(item.payload))
            _validate_attention_id(item.attention_id)
            validate_payload(ATTENTION_ITEM_SET_ID, CENTRAL_ATTENTION_REVISION, payload)
            _canonical_bytes(payload)
            version = _validate_version(payload.get("source_version"))
            if not _coherent_role_state(
                payload.get("participant_role"), payload.get("attention_state"),
            ):
                raise ValueError("stored role/state mismatch")
            registration = self.registry.require_class(
                payload["application_scope"], payload["notification_class"],
            )
        except Exception as exc:
            raise AttentionIndexError("unavailable") from exc
        runtime = registration.runtime
        if runtime is None:
            raise AttentionIndexError("unavailable")
        try:
            evidence = runtime.source_evidence_builder(payload["object_ref"], version)
        except Exception as exc:
            raise AttentionIndexError("unavailable") from exc
        try:
            return self._normalize_evidence(
                evidence, object_ref=payload["object_ref"], source_version=version,
            )
        except Exception as exc:
            raise AttentionIndexError("unavailable") from exc

    def _validated_item(
        self, record: AttentionItemRecord,
    ) -> tuple[AttentionItemRecord, Any, Any]:
        if (
            not isinstance(record, AttentionItemRecord)
            or not isinstance(record.payload, Mapping)
        ):
            raise AttentionIndexError("unavailable")
        try:
            payload = _copy_json(dict(record.payload))
            _validate_attention_id(record.attention_id)
            validate_payload(ATTENTION_ITEM_SET_ID, CENTRAL_ATTENTION_REVISION, payload)
            _validate_version(payload.get("source_version"))
            if not _coherent_role_state(
                payload.get("participant_role"), payload.get("attention_state"),
            ):
                raise ValueError("stored role/state mismatch")
            registration = self.registry.require_class(
                payload["application_scope"], payload["notification_class"],
            )
            application = self.registry.require_application(payload["application_scope"])
        except Exception as exc:
            raise AttentionIndexError("unavailable") from exc
        return AttentionItemRecord(record.attention_id, payload), registration, application

    @staticmethod
    def _validated_presentation(
        record: AttentionPresentationRecord,
    ) -> AttentionPresentationRecord:
        if (
            not isinstance(record, AttentionPresentationRecord)
            or not isinstance(record.payload, Mapping)
        ):
            raise AttentionIndexError("unavailable")
        try:
            payload = _copy_json(dict(record.payload))
            _validate_presentation_id(record.attention_id)
            validate_payload(
                ATTENTION_PRESENTATION_SET_ID,
                CENTRAL_ATTENTION_REVISION,
                payload,
            )
        except Exception as exc:
            raise AttentionIndexError("unavailable") from exc
        return AttentionPresentationRecord(record.attention_id, payload)

    @staticmethod
    def _query_item(
        record: AttentionItemRecord,
        registration: Any,
        application: Any,
        presentation: Mapping[str, Any] | None,
    ) -> AttentionQueryItem:
        return AttentionQueryItem(
            attention_id=record.attention_id,
            payload=record.payload,
            presentation=presentation,
            application_label=application.label,
            icon_ref=application.icon_ref,
            surface_category=registration.surface_category,
            review_renderer_id=registration.review_renderer_id,
        )

    def get_query_item(self, attention_id: Any) -> AttentionQueryItem | None:
        """Resolve one exact item through the same validation/join as query."""
        key = _validate_attention_id(attention_id)
        try:
            raw_item = self.store.get_item(key)
            raw_presentation = self.store.get_presentation(key)
        except Exception as exc:
            raise AttentionIndexError("unavailable") from exc
        if raw_item is None:
            return None
        item, registration, application = self._validated_item(raw_item)
        presentation = None
        if raw_presentation is not None:
            clean = self._validated_presentation(raw_presentation)
            if clean.attention_id != key:
                raise AttentionIndexError("unavailable")
            presentation = clean.payload
        return self._query_item(
            item, registration, application, presentation,
        )

    def _normalize_filters(
        self,
        *,
        application_scope: Any,
        surface_category: Any,
        attention_state: Any,
        participant_role: Any,
        limit: Any,
    ) -> dict[str, Any]:
        if application_scope is not None:
            try:
                self.registry.require_application(application_scope)
            except Exception as exc:
                raise AttentionIndexError("invalid_request", "unknown application") from exc
        if surface_category is not None and surface_category not in _CATEGORIES:
            raise AttentionIndexError("invalid_request", "unknown category")
        if attention_state is not None and attention_state not in _STATES:
            raise AttentionIndexError("invalid_request", "unknown attention state")
        if participant_role is not None and participant_role not in {"recipient", "sender"}:
            raise AttentionIndexError("invalid_request", "unknown participant role")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise AttentionIndexError("invalid_request", "invalid limit")
        return {
            "application_scope": application_scope,
            "surface_category": surface_category,
            "attention_state": attention_state,
            "participant_role": participant_role,
            "limit": limit,
        }

    def _encode_cursor(
        self, filters: Mapping[str, Any], snapshot: str, last: AttentionQueryItem,
    ) -> str:
        payload = {
            "v": 1,
            "f": dict(filters),
            "s": snapshot,
            "k": [last.payload["occurred_at"], last.attention_id],
        }
        raw = _canonical_bytes(payload)
        return f"{_b64(raw)}.{_b64(hmac.new(self._cursor_secret, raw, hashlib.sha256).digest())}"

    def _decode_cursor(
        self, cursor: Any, filters: Mapping[str, Any], snapshot: str,
    ) -> tuple[float, str]:
        if not isinstance(cursor, str) or not 1 <= len(cursor.encode("utf-8")) <= MAX_CURSOR_BYTES:
            raise AttentionIndexError("invalid_cursor")
        try:
            payload_part, signature_part = cursor.split(".")
            raw = _unb64(payload_part)
            signature = _unb64(signature_part)
        except Exception as exc:
            raise AttentionIndexError("invalid_cursor") from exc
        expected = hmac.new(self._cursor_secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise AttentionIndexError("invalid_cursor")
        try:
            payload = json.loads(raw)
        except Exception as exc:
            raise AttentionIndexError("invalid_cursor") from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {"v", "f", "s", "k"}
            or payload.get("v") != 1
            or payload.get("f") != dict(filters)
            or not isinstance(payload.get("k"), list)
            or len(payload["k"]) != 2
        ):
            raise AttentionIndexError("invalid_cursor")
        if payload.get("s") != snapshot:
            raise AttentionIndexError("refresh_required")
        try:
            occurred_at = _timestamp(payload["k"][0])
            attention_id = _validate_attention_id(payload["k"][1])
        except AttentionIndexError as exc:
            raise AttentionIndexError("invalid_cursor") from exc
        return occurred_at, attention_id

    def query(
        self,
        *,
        application_scope: Any = None,
        surface_category: Any = None,
        attention_state: Any = None,
        participant_role: Any = None,
        limit: Any = 50,
        cursor: Any = None,
    ) -> AttentionQueryResult:
        filters = self._normalize_filters(
            application_scope=application_scope,
            surface_category=surface_category,
            attention_state=attention_state,
            participant_role=participant_role,
            limit=limit,
        )
        try:
            raw_items = self.store.list_items()
            raw_presentations = self.store.list_presentations()
        except Exception as exc:
            raise AttentionIndexError("unavailable") from exc

        items: list[tuple[AttentionItemRecord, Any, Any]] = []
        snapshot_rows = []
        seen_item_ids: set[str] = set()
        for record in raw_items:
            try:
                clean, registration, application = self._validated_item(record)
                if record.attention_id in seen_item_ids:
                    raise ValueError("duplicate attention item")
                seen_item_ids.add(record.attention_id)
            except AttentionIndexError:
                raise
            except Exception as exc:
                raise AttentionIndexError("unavailable") from exc
            items.append((clean, registration, application))
            snapshot_rows.append({
                "attention_id": record.attention_id,
                "payload": clean.payload,
            })

        presentations: dict[str, Mapping[str, Any]] = {}
        for record in raw_presentations:
            try:
                clean = self._validated_presentation(record)
                if record.attention_id in presentations:
                    raise ValueError("duplicate attention presentation")
            except AttentionIndexError:
                raise
            except Exception as exc:
                raise AttentionIndexError("unavailable") from exc
            presentations[record.attention_id] = clean.payload

        try:
            snapshot = _b64(hashlib.sha256(_canonical_bytes({
                "items": sorted(snapshot_rows, key=lambda row: row["attention_id"]),
                "registry": self.registry.snapshot_payload(),
            })).digest())
        except Exception as exc:
            raise AttentionIndexError("unavailable") from exc

        joined = [self._query_item(
            record,
            registration,
            application,
            presentations.get(record.attention_id),
        ) for record, registration, application in items]
        joined.sort(key=lambda item: (-item.payload["occurred_at"], item.attention_id))

        role_cohort = [
            item for item in joined
            if participant_role is None or item.payload["participant_role"] == participant_role
        ]
        scoped = [
            item for item in role_cohort
            if application_scope is None or item.payload["application_scope"] == application_scope
        ]
        total = sum(item.payload["attention_state"] == "needs_attention" for item in scoped)
        category_counts = {
            category: sum(
                item.payload["attention_state"] == "needs_attention"
                and item.surface_category == category
                for item in scoped
            )
            for category in _CATEGORIES
        }
        selected_category = [
            item for item in scoped
            if surface_category is None or item.surface_category == surface_category
        ]
        state_counts = {
            state: sum(item.payload["attention_state"] == state for item in selected_category)
            for state in _STATES
        }
        application_counts = {}
        applications = (
            [self.registry.require_application(application_scope)]
            if application_scope is not None
            else list(self.registry.applications)
        )
        for application in applications:
            cohort = [
                item for item in role_cohort
                if item.payload["application_scope"] == application.application_scope
            ]
            application_counts[application.application_scope] = {
                state: sum(item.payload["attention_state"] == state for item in cohort)
                for state in _STATES
            }

        visible = [
            item for item in selected_category
            if attention_state is None or item.payload["attention_state"] == attention_state
        ]
        start = 0
        if cursor is not None:
            last_key = self._decode_cursor(cursor, filters, snapshot)
            for index, item in enumerate(visible):
                if (item.payload["occurred_at"], item.attention_id) == last_key:
                    start = index + 1
                    break
            else:
                raise AttentionIndexError("invalid_cursor")
        page = visible[start:start + limit]
        next_cursor = None
        if start + limit < len(visible) and page:
            next_cursor = self._encode_cursor(filters, snapshot, page[-1])
        return AttentionQueryResult(
            items=page,
            counts=AttentionQueryCounts(
                total_needs_attention=total,
                categories=category_counts,
                states=state_counts,
                applications=application_counts,
            ),
            next_cursor=next_cursor,
            snapshot_version=snapshot,
        )


__all__ = [
    "AttentionApplicationRecord",
    "AttentionChange",
    "AttentionIndexError",
    "AttentionIndexService",
    "AttentionItemRecord",
    "AttentionPresentationRecord",
    "AttentionQueryCounts",
    "AttentionQueryItem",
    "AttentionQueryResult",
    "InMemoryAttentionIndexStore",
    "SettingsAttentionIndexStore",
    "canonical_attention_event_id",
]
