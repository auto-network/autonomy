"""Trusted mediator for operator-local Central Attention presentation state.

This module records only seen, opened, and snooze timestamps in the existing
personal ``dashboard.attention.presentation#1`` Setting.  It has no approval,
attention-lifecycle, foreground-acknowledgment, or delivery authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import threading
import time
import unicodedata
from typing import Any, Callable, Mapping, Protocol

from tools.graph import settings_ops
from tools.graph.schemas.central_attention import (
    ATTENTION_ITEM_SET_ID,
    ATTENTION_PRESENTATION_SET_ID,
    CENTRAL_ATTENTION_REVISION,
)
from tools.graph.schemas.registry import validate_payload


MIN_SNOOZE_SECONDS = 60
MAX_SNOOZE_SECONDS = 30 * 24 * 60 * 60
MAX_ATTENTION_ID_BYTES = 256


class AttentionPresentationError(RuntimeError):
    """Bounded presentation failure suitable for later HTTP translation."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(f"{code}: {message or code.replace('_', ' ')}")


@dataclass(frozen=True, slots=True)
class AttentionItemRecord:
    attention_id: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AttentionPresentationRecord:
    attention_id: str
    payload: Mapping[str, Any]


AttentionReferenceResolver = Callable[[str], AttentionItemRecord | None]


class AttentionPresentationStore(Protocol):
    def get(self, attention_id: str) -> AttentionPresentationRecord | None: ...
    def upsert(
        self, attention_id: str, payload: Mapping[str, Any],
    ) -> AttentionPresentationRecord: ...


def _copy_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(payload), sort_keys=True, separators=(",", ":")))


def _validate_attention_id(attention_id: Any) -> str:
    if not isinstance(attention_id, str) or not attention_id:
        raise AttentionPresentationError("invalid_request", "invalid attention ID")
    if attention_id != attention_id.strip():
        raise AttentionPresentationError("invalid_request", "invalid attention ID")
    try:
        byte_length = len(attention_id.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise AttentionPresentationError(
            "invalid_request", "invalid attention ID",
        ) from exc
    if byte_length < 1 or byte_length > MAX_ATTENTION_ID_BYTES:
        raise AttentionPresentationError("invalid_request", "invalid attention ID")
    if any(unicodedata.category(character).startswith("C") for character in attention_id):
        raise AttentionPresentationError("invalid_request", "invalid attention ID")
    return attention_id


class SettingsAttentionItemResolver:
    """Resolve one exact Central Attention item from the local personal store."""

    def __call__(self, attention_id: str) -> AttentionItemRecord | None:
        row = settings_ops.read_set_key(
            ATTENTION_ITEM_SET_ID, attention_id, org=None, peers=[],
        )
        if row is None:
            return None
        if not isinstance(row, dict) or row.get("key") != attention_id:
            raise RuntimeError("stored attention reference is malformed")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("stored attention reference is malformed")
        try:
            validate_payload(ATTENTION_ITEM_SET_ID, CENTRAL_ATTENTION_REVISION, payload)
        except Exception as exc:
            raise RuntimeError("stored attention reference is invalid") from exc
        return AttentionItemRecord(attention_id, _copy_payload(payload))


class SettingsAttentionPresentationStore:
    """Read and keyed-upsert the one personal presentation row for an item."""

    def get(self, attention_id: str) -> AttentionPresentationRecord | None:
        row = settings_ops.read_set_key(
            ATTENTION_PRESENTATION_SET_ID, attention_id, org=None, peers=[],
        )
        if row is None:
            return None
        if not isinstance(row, dict) or row.get("key") != attention_id:
            raise RuntimeError("stored attention presentation is malformed")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("stored attention presentation is malformed")
        try:
            validate_payload(
                ATTENTION_PRESENTATION_SET_ID, CENTRAL_ATTENTION_REVISION, payload,
            )
        except Exception as exc:
            raise RuntimeError("stored attention presentation is invalid") from exc
        return AttentionPresentationRecord(attention_id, _copy_payload(payload))

    def upsert(
        self, attention_id: str, payload: Mapping[str, Any],
    ) -> AttentionPresentationRecord:
        clean = _copy_payload(payload)
        settings_ops.upsert_by_key(
            ATTENTION_PRESENTATION_SET_ID,
            CENTRAL_ATTENTION_REVISION,
            attention_id,
            clean,
            org=None,
            state="raw",
        )
        return AttentionPresentationRecord(attention_id, _copy_payload(clean))


class _PresentationLocks:
    """Bounded process-wide locks shared by every mediator instance."""

    _locks = tuple(threading.RLock() for _ in range(257))

    @classmethod
    def for_id(cls, attention_id: str) -> threading.RLock:
        digest = hashlib.sha256(attention_id.encode("utf-8")).digest()
        return cls._locks[int.from_bytes(digest[:4], "big") % len(cls._locks)]


class AttentionPresentationService:
    """Merge-safe single-process writer for non-semantic display state."""

    def __init__(
        self,
        *,
        reference_resolver: AttentionReferenceResolver | None = None,
        store: AttentionPresentationStore | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._reference_resolver = reference_resolver or SettingsAttentionItemResolver()
        self.store = store or SettingsAttentionPresentationStore()
        self._clock = clock

    def _now(self) -> float:
        try:
            value = self._clock()
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError("invalid server clock")
            timestamp = float(value)
        except Exception as exc:
            raise AttentionPresentationError("invalid_request", "invalid server clock") from exc
        if not math.isfinite(timestamp) or timestamp < 0:
            raise AttentionPresentationError("invalid_request", "invalid server clock")
        return timestamp

    def _require_reference(self, attention_id: str) -> AttentionItemRecord:
        try:
            reference = self._reference_resolver(attention_id)
        except Exception as exc:
            raise AttentionPresentationError("unavailable") from exc
        if reference is None:
            raise AttentionPresentationError("not_found")
        if (
            not isinstance(reference, AttentionItemRecord)
            or reference.attention_id != attention_id
            or not isinstance(reference.payload, Mapping)
        ):
            raise AttentionPresentationError("unavailable")
        try:
            validate_payload(
                ATTENTION_ITEM_SET_ID,
                CENTRAL_ATTENTION_REVISION,
                dict(reference.payload),
            )
        except Exception as exc:
            raise AttentionPresentationError("unavailable") from exc
        return reference

    def _mutate(
        self,
        attention_id: Any,
        mutation: Callable[[dict[str, Any], float], None],
    ) -> AttentionPresentationRecord:
        key = _validate_attention_id(attention_id)
        with _PresentationLocks.for_id(key):
            self._require_reference(key)
            now = self._now()
            try:
                existing = self.store.get(key)
            except Exception as exc:
                raise AttentionPresentationError("unavailable") from exc
            if existing is not None and (
                not isinstance(existing, AttentionPresentationRecord)
                or existing.attention_id != key
                or not isinstance(existing.payload, Mapping)
            ):
                raise AttentionPresentationError("unavailable")
            try:
                payload = {} if existing is None else _copy_payload(existing.payload)
            except Exception as exc:
                raise AttentionPresentationError("unavailable") from exc
            if existing is not None:
                try:
                    validate_payload(
                        ATTENTION_PRESENTATION_SET_ID,
                        CENTRAL_ATTENTION_REVISION,
                        payload,
                    )
                except Exception as exc:
                    raise AttentionPresentationError("invalid_request") from exc
            mutation(payload, now)
            try:
                validate_payload(
                    ATTENTION_PRESENTATION_SET_ID,
                    CENTRAL_ATTENTION_REVISION,
                    payload,
                )
            except Exception as exc:
                raise AttentionPresentationError("invalid_request") from exc
            try:
                written = self.store.upsert(key, payload)
            except Exception as exc:
                raise AttentionPresentationError("unavailable") from exc
            if (
                not isinstance(written, AttentionPresentationRecord)
                or written.attention_id != key
                or not isinstance(written.payload, Mapping)
            ):
                raise AttentionPresentationError("unavailable")
            try:
                validate_payload(
                    ATTENTION_PRESENTATION_SET_ID,
                    CENTRAL_ATTENTION_REVISION,
                    dict(written.payload),
                )
            except Exception as exc:
                raise AttentionPresentationError("unavailable") from exc
            return written

    def mark_seen(self, attention_id: Any) -> AttentionPresentationRecord:
        def apply(payload: dict[str, Any], now: float) -> None:
            current = payload.get("seen_at")
            payload["seen_at"] = (
                max(float(current), now)
                if isinstance(current, (int, float)) and not isinstance(current, bool)
                else now
            )

        return self._mutate(attention_id, apply)

    def mark_opened(self, attention_id: Any) -> AttentionPresentationRecord:
        def apply(payload: dict[str, Any], now: float) -> None:
            current = payload.get("last_opened_at")
            payload["last_opened_at"] = (
                max(float(current), now)
                if isinstance(current, (int, float)) and not isinstance(current, bool)
                else now
            )

        return self._mutate(attention_id, apply)

    def snooze(
        self, attention_id: Any, duration_seconds: Any,
    ) -> AttentionPresentationRecord:
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, int)
            or not MIN_SNOOZE_SECONDS <= duration_seconds <= MAX_SNOOZE_SECONDS
        ):
            raise AttentionPresentationError("invalid_request", "invalid snooze duration")

        def apply(payload: dict[str, Any], now: float) -> None:
            payload["snoozed_until"] = now + duration_seconds

        return self._mutate(attention_id, apply)

    def clear_snooze(self, attention_id: Any) -> AttentionPresentationRecord:
        def apply(payload: dict[str, Any], now: float) -> None:
            payload["snoozed_until"] = now

        return self._mutate(attention_id, apply)
