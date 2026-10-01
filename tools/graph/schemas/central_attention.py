"""Phase-one Settings vocabulary for Central Attention and Approvals.

These schemas describe durable records only. The trusted dashboard services
own cross-row rules such as immutable identity fields, monotonic source/state
versions, first-decision-wins, and audience derivation.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from .registry import (
    SchemaValidationError,
    SettingSchema,
    append_only_log,
    field,
    home,
    publication_band,
)


APPROVAL_REQUEST_SET_ID = "dashboard.approval.request"
APPROVAL_RESOLUTION_SET_ID = "dashboard.approval.resolution"
CENTRAL_ATTENTION_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Personal Settings records for central approval requests and "
        "resolutions."
    ),
    "nouns": [
        "central attention",
        "approval request",
        "approval resolution",
    ],
    "related_set_ids": [],
}


_SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_ASCII_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SOURCE_GUARD_FIELDS = {"kind", "ref", "version"}
_REQUESTER_FIELDS = {"kind", "id", "label"}
_DECIDER_FIELDS = {"kind", "id"}
_SAFE_REVIEW_FORBIDDEN = (
    "html",
    "route",
    "credential",
    "password",
    "private_key",
    "root_key",
    "secret",
    "token",
)
_STAGED_FORBIDDEN = (
    "password",
    "passphrase",
    "private_key",
    "root_key",
    "secret",
    "token",
    "credential",
    "authorization",
    "cookie",
)


def _fail(cls: type, message: str) -> None:
    raise SchemaValidationError(f"{cls.__name__}: {message}")


def _slug(cls: type, payload: dict, name: str, *, maximum: int = 64) -> str:
    value = payload.get(name)
    if (
        not isinstance(value, str)
        or not (1 <= len(value) <= maximum)
        or not _SLUG_RE.fullmatch(value)
    ):
        _fail(cls, f"{name!r} must be a lowercase identifier up to {maximum} characters")
    return value


def _text(
    cls: type,
    payload: dict,
    name: str,
    *,
    maximum: int,
    minimum: int = 1,
) -> str:
    value = payload.get(name)
    if (
        not isinstance(value, str)
        or not (minimum <= len(value) <= maximum)
        or _ASCII_CONTROL_RE.search(value)
    ):
        _fail(
            cls,
            f"{name!r} must be {minimum}..{maximum} characters without ASCII controls",
        )
    return value


def _optional_text(cls: type, payload: dict, name: str, *, maximum: int) -> None:
    if name in payload and payload[name] is not None:
        _text(cls, payload, name, maximum=maximum)


def _integer(
    cls: type,
    payload: dict,
    name: str,
    *,
    minimum: int = 0,
) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        _fail(cls, f"{name!r} must be an integer >= {minimum}")
    return value


def _timestamp(cls: type, payload: dict, name: str) -> float:
    value = payload.get(name)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        _fail(cls, f"{name!r} must be a finite non-negative timestamp")
    return float(value)


def _optional_timestamp(cls: type, payload: dict, name: str) -> float | None:
    if name not in payload or payload[name] is None:
        return None
    return _timestamp(cls, payload, name)


def _closed_object(
    cls: type,
    value: Any,
    name: str,
    *,
    allowed: set[str],
    required: set[str],
) -> dict:
    if not isinstance(value, dict):
        _fail(cls, f"{name!r} must be an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        _fail(cls, f"{name!r} has unknown field(s): {unknown}")
    missing = sorted(required - set(value))
    if missing:
        _fail(cls, f"{name!r} is missing field(s): {missing}")
    return value


def _bounded_json(
    cls: type,
    value: Any,
    name: str,
    *,
    maximum_bytes: int,
    forbidden_keys: tuple[str, ...] = (),
) -> None:
    if not isinstance(value, dict):
        _fail(cls, f"{name!r} must be an object")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        _fail(cls, f"{name!r} must contain JSON data")
    if len(encoded.encode("utf-8")) > maximum_bytes:
        _fail(cls, f"{name!r} exceeds {maximum_bytes} encoded bytes")

    def walk(current: Any, depth: int) -> None:
        if depth > 6:
            _fail(cls, f"{name!r} exceeds maximum nesting depth")
        if isinstance(current, dict):
            if len(current) > 64:
                _fail(cls, f"{name!r} contains too many object fields")
            for key, child in current.items():
                if (
                    not isinstance(key, str)
                    or not (1 <= len(key) <= 80)
                    or _ASCII_CONTROL_RE.search(key)
                ):
                    _fail(cls, f"{name!r} contains an invalid field name")
                lowered = key.lower()
                if any(term in lowered for term in forbidden_keys):
                    _fail(cls, f"{name!r} contains forbidden field {key!r}")
                walk(child, depth + 1)
        elif isinstance(current, list):
            if len(current) > 128:
                _fail(cls, f"{name!r} contains too many list entries")
            for child in current:
                walk(child, depth + 1)
        elif isinstance(current, str):
            if len(current) > 4096 or _ASCII_CONTROL_RE.search(current):
                _fail(cls, f"{name!r} contains an invalid string")
        elif isinstance(current, bool) or current is None or isinstance(current, int):
            return
        elif isinstance(current, float):
            if not math.isfinite(current):
                _fail(cls, f"{name!r} contains a non-finite number")
        else:
            _fail(cls, f"{name!r} contains non-JSON data")

    walk(value, 0)


@home("personal")
@publication_band(min="raw", max="raw")
@append_only_log(key="approval_id")
class ApprovalRequestV1(SettingSchema):
    """One server-derived request addressed to a person or persona."""

    set_id = APPROVAL_REQUEST_SET_ID
    schema_revision = CENTRAL_ATTENTION_REVISION

    application_scope: str = field(
        required=True,
        description="Registered application that owns this approval kind.",
    )
    kind: str = field(required=True, description="Closed registered approval kind.")
    requester_ref: dict = field(
        required=True,
        description="Server-derived authenticated requester reference.",
    )
    decider: dict = field(
        required=True,
        description="Server-derived person or persona allowed to decide.",
    )
    subject_ref: str = field(
        required=True,
        description="Opaque application-owned subject of the requested action.",
    )
    safe_review: dict = field(
        required=True,
        description="Bounded renderer-safe facts shown during review.",
    )
    request: dict = field(
        required=True,
        description="Immutable bounded kind-specific request payload.",
    )
    staged: dict = field(
        required=False,
        default=None,
        description="Optional server-frozen bounded execution context or opaque reference.",
    )
    created_at: float = field(required=True, description="Request creation timestamp.")
    expires_at: float = field(
        required=False,
        default=None,
        description="Optional policy expiry timestamp.",
    )
    source_version: int = field(
        required=True,
        description="Request record version, fixed to one in schema revision one.",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        _slug(cls, payload, "application_scope")
        _slug(cls, payload, "kind")
        requester = _closed_object(
            cls,
            payload.get("requester_ref"),
            "requester_ref",
            allowed=_REQUESTER_FIELDS,
            required={"kind", "id"},
        )
        if requester.get("kind") not in (
            "session",
            "registered_service",
            "internal_service",
            "external_service",
        ):
            _fail(cls, "requester_ref kind is not a registered requester type")
        _text(cls, requester, "id", maximum=256)
        _optional_text(cls, requester, "label", maximum=120)
        decider = _closed_object(
            cls,
            payload.get("decider"),
            "decider",
            allowed=_DECIDER_FIELDS,
            required=_DECIDER_FIELDS,
        )
        if decider.get("kind") not in ("person", "persona"):
            _fail(cls, "decider kind must be person or persona in revision 1")
        _text(cls, decider, "id", maximum=256)
        _text(cls, payload, "subject_ref", maximum=256)
        _bounded_json(
            cls,
            payload.get("safe_review"),
            "safe_review",
            maximum_bytes=8192,
            forbidden_keys=_SAFE_REVIEW_FORBIDDEN,
        )
        _bounded_json(cls, payload.get("request"), "request", maximum_bytes=16384)
        if payload.get("staged") is not None:
            _bounded_json(
                cls,
                payload["staged"],
                "staged",
                maximum_bytes=16384,
                forbidden_keys=_STAGED_FORBIDDEN,
            )
        created = _timestamp(cls, payload, "created_at")
        expires = _optional_timestamp(cls, payload, "expires_at")
        if expires is not None and expires <= created:
            _fail(cls, "expires_at must be after created_at")
        if _integer(cls, payload, "source_version", minimum=1) != 1:
            _fail(cls, "source_version is fixed to 1 in revision 1")


@home("personal")
@publication_band(min="raw", max="raw")
@append_only_log(key="approval_id")
class ApprovalResolutionV1(SettingSchema):
    """One canonical result selected by the later serializing service."""

    set_id = APPROVAL_RESOLUTION_SET_ID
    schema_revision = CENTRAL_ATTENTION_REVISION

    outcome: str = field(
        required=True,
        enum=["granted", "declined", "canceled", "expired"],
        description="Canonical terminal outcome selected by the trusted service.",
    )
    decider_ref: str | None = field(
        required=False,
        default=None,
        description="Server-derived deciding identity for human outcomes.",
    )
    resolved_at: float = field(required=True, description="Resolution timestamp.")
    decision: dict = field(
        required=False,
        default=None,
        description="Bounded kind-specific human decision fields.",
    )
    result_ref: str | None = field(
        required=False,
        default=None,
        description="Optional opaque application-owned result reference.",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        _timestamp(cls, payload, "resolved_at")
        outcome = payload.get("outcome")
        if outcome in ("granted", "declined") and not payload.get("decider_ref"):
            _fail(cls, f"{outcome} requires server-derived decider_ref")
        _optional_text(cls, payload, "decider_ref", maximum=256)
        if payload.get("decision") is not None:
            if outcome not in ("granted", "declined"):
                _fail(cls, "decision is permitted only for a human outcome")
            _bounded_json(cls, payload["decision"], "decision", maximum_bytes=8192)
        if payload.get("result_ref") is not None:
            if outcome != "granted":
                _fail(cls, "result_ref is permitted only for a granted outcome")
            _text(cls, payload, "result_ref", maximum=256)
