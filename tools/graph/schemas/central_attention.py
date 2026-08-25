"""Phase-one Settings vocabulary for Central Attention and Approvals.

These schemas describe durable records only. The trusted dashboard services
own cross-row rules such as immutable identity fields, monotonic source/state
versions, first-decision-wins, audience derivation, and delivery races.
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
    keyed_per_entity,
    publication_band,
)


ATTENTION_APPLICATION_SET_ID = "dashboard.attention.application"
ATTENTION_ITEM_SET_ID = "dashboard.attention.item"
ATTENTION_PRESENTATION_SET_ID = "dashboard.attention.presentation"
ATTENTION_DELIVERY_SET_ID = "dashboard.attention.delivery"
APPROVAL_REQUEST_SET_ID = "dashboard.approval.request"
APPROVAL_RESOLUTION_SET_ID = "dashboard.approval.resolution"
CENTRAL_ATTENTION_REVISION = 1


SYNOPSIS = {
    "summary": (
        "Personal Settings records for registered attention applications, "
        "recipient projections, presentation state, foreground/Web Push "
        "delivery latches, and central approval requests and resolutions."
    ),
    "nouns": [
        "central attention",
        "approval request",
        "approval resolution",
        "attention inbox",
        "notification delivery latch",
        "presentation seen",
        "snooze",
    ],
    "related_set_ids": [],
}


_SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_ASCII_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_CLASS_POLICY_FIELDS = {
    "notification_class",
    "class_policy_revision",
    "eligible_transition",
    "push_policy",
    "delivery_class",
    "budget_class",
    "coalesce_scope",
    "ttl_seconds",
    "urgency",
    "privacy_renderer_id",
    "route_builder_id",
    "destination_id",
}
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
@keyed_per_entity(key_strategy="application_scope")
class AttentionApplicationV1(SettingSchema):
    """One code-registered application and its closed notification classes."""

    set_id = ATTENTION_APPLICATION_SET_ID
    schema_revision = CENTRAL_ATTENTION_REVISION

    label: str = field(required=True, description="Safe operator-facing application name.")
    icon_ref: str = field(required=True, description="Code-owned icon identifier.")
    open_mode: str = field(
        required=True,
        enum=["application", "registered_renderer"],
        description="Whether opening uses the application or a registered renderer.",
    )
    notification_classes: list = field(
        required=True,
        element=dict,
        description="Closed notification-class policies registered by application code.",
    )
    enabled: bool = field(
        required=True,
        description="Whether this registered application may currently publish attention.",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        _text(cls, payload, "label", maximum=80)
        _slug(cls, payload, "icon_ref")
        classes = payload.get("notification_classes")
        if not isinstance(classes, list) or not (1 <= len(classes) <= 64):
            _fail(cls, "'notification_classes' must contain 1..64 policies")
        seen: set[str] = set()
        for index, policy in enumerate(classes):
            policy = _closed_object(
                cls,
                policy,
                f"notification_classes[{index}]",
                allowed=_CLASS_POLICY_FIELDS,
                required=_CLASS_POLICY_FIELDS,
            )
            for name in (
                "notification_class",
                "delivery_class",
                "budget_class",
                "privacy_renderer_id",
                "route_builder_id",
                "destination_id",
            ):
                _slug(cls, policy, name)
            notification_class = policy["notification_class"]
            if notification_class in seen:
                _fail(cls, f"duplicate notification class {notification_class!r}")
            seen.add(notification_class)
            _integer(cls, policy, "class_policy_revision", minimum=1)
            if policy.get("eligible_transition") != "needs_attention":
                _fail(cls, "eligible_transition must be 'needs_attention' in revision 1")
            if policy.get("push_policy") not in ("in_app_only", "fallback"):
                _fail(cls, "push_policy must be 'in_app_only' or 'fallback'")
            if policy.get("coalesce_scope") not in ("event", "object", "application"):
                _fail(cls, "coalesce_scope must be event, object, or application")
            ttl = _integer(cls, policy, "ttl_seconds", minimum=1)
            if ttl > 2_592_000:
                _fail(cls, "ttl_seconds must not exceed 30 days")
            if policy.get("urgency") not in ("very-low", "low", "normal", "high"):
                _fail(cls, "urgency must be very-low, low, normal, or high")


@home("personal")
@publication_band(min="raw", max="raw")
@keyed_per_entity(key_strategy="attention_id")
class AttentionItemV1(SettingSchema):
    """One participant-local projection of an application-owned object."""

    set_id = ATTENTION_ITEM_SET_ID
    schema_revision = CENTRAL_ATTENTION_REVISION

    application_scope: str = field(
        required=True,
        description="Registered application that owns the source object.",
    )
    notification_class: str = field(
        required=True,
        description="Registered class whose policy produced this projection.",
    )
    object_ref: str = field(
        required=True,
        description="Opaque application-owned source object reference.",
    )
    participant_role: str = field(
        required=True,
        enum=["recipient", "sender"],
        description="This personal projection's role in the source exchange.",
    )
    attention_state: str = field(
        required=True,
        enum=["needs_attention", "waiting", "resolved"],
        description="Small cross-application attention state shown by the inbox.",
    )
    safe_title: str = field(
        required=True,
        description="Bounded privacy-safe title for the central surface.",
    )
    safe_summary: str | None = field(
        required=False,
        default=None,
        description="Optional bounded privacy-safe supporting text.",
    )
    counterparty_ref: str | None = field(
        required=False,
        default=None,
        description="Optional opaque reference to the other participant.",
    )
    occurred_at: float = field(
        required=True,
        description="Timestamp of the source transition represented here.",
    )
    source_version: int = field(
        required=True,
        description="Monotonic version supplied by the registered source.",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        _slug(cls, payload, "application_scope")
        _slug(cls, payload, "notification_class")
        _text(cls, payload, "object_ref", maximum=256)
        _text(cls, payload, "safe_title", maximum=160)
        _optional_text(cls, payload, "safe_summary", maximum=600)
        _optional_text(cls, payload, "counterparty_ref", maximum=256)
        _timestamp(cls, payload, "occurred_at")
        _integer(cls, payload, "source_version")


@home("personal")
@publication_band(min="raw", max="raw")
@keyed_per_entity(key_strategy="attention_id")
class AttentionPresentationV1(SettingSchema):
    """Operator-local display state with no lifecycle or delivery authority."""

    set_id = ATTENTION_PRESENTATION_SET_ID
    schema_revision = CENTRAL_ATTENTION_REVISION

    seen_at: float = field(
        required=False,
        default=None,
        description="When the operator last marked this presentation seen.",
    )
    snoozed_until: float = field(
        required=False,
        default=None,
        description="Optional time before which this presentation stays quiet.",
    )
    last_opened_at: float = field(
        required=False,
        default=None,
        description="When the operator last opened this presentation.",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        present = [
            name
            for name in ("seen_at", "snoozed_until", "last_opened_at")
            if payload.get(name) is not None
        ]
        if not present:
            _fail(cls, "at least one presentation timestamp is required")
        for name in present:
            _timestamp(cls, payload, name)


@home("personal")
@publication_band(min="raw", max="raw")
@keyed_per_entity(key_strategy="delivery_id")
class AttentionDeliveryV1(SettingSchema):
    """Transport-neutral latch for foreground-first attention delivery."""

    set_id = ATTENTION_DELIVERY_SET_ID
    schema_revision = CENTRAL_ATTENTION_REVISION

    event_id: str = field(required=True, description="Stable registered-source event ID.")
    attention_id: str = field(
        required=True,
        description="Attention projection addressed by this delivery latch.",
    )
    source_version: int = field(
        required=True,
        description="Exact registered-source version this latch represents.",
    )
    application_scope: str = field(
        required=True,
        description="Registered application whose policy created the latch.",
    )
    notification_class: str = field(
        required=True,
        description="Registered notification class frozen for this latch.",
    )
    class_policy_revision: int = field(
        required=True,
        description="Exact application class-policy revision frozen for delivery.",
    )
    delivery_class: str = field(
        required=True,
        description="Registered delivery behavior selected by class policy.",
    )
    budget_class: str = field(
        required=True,
        description="Registered interruption-budget class selected by policy.",
    )
    coalesce_key: str = field(
        required=True,
        description="Bounded code-derived key for transport-neutral coalescing.",
    )
    urgency: str = field(
        required=True,
        enum=["very-low", "low", "normal", "high"],
        description="Frozen delivery urgency selected by class policy.",
    )
    privacy_renderer_id: str = field(
        required=True,
        description="Registered privacy renderer retained through latch expiry.",
    )
    route_builder_id: str = field(
        required=True,
        description="Registered route builder retained through latch expiry.",
    )
    destination_id: str = field(
        required=True,
        description="Registered destination selected by application policy.",
    )
    source_guard: dict = field(
        required=True,
        description="Closed reference used to recheck source eligibility.",
    )
    created_at: float = field(required=True, description="Latch creation timestamp.")
    expires_at: float = field(required=True, description="Final latch expiry timestamp.")
    state: str = field(
        required=True,
        enum=[
            "foreground_wait",
            "background_due",
            "background_released",
            "foreground_applied",
            "ineligible",
            "expired",
        ],
        description="Current transport-neutral delivery-latch state.",
    )
    state_version: int = field(
        required=True,
        description="Monotonic version of the trusted latch transition.",
    )
    updated_at: float = field(
        required=True,
        description="Timestamp of the latest trusted latch transition.",
    )
    foreground_selected_at: float = field(
        required=False,
        default=None,
        description="When a qualifying visible client was selected.",
    )
    fallback_due_at: float = field(
        required=False,
        default=None,
        description="Deadline after which background fallback becomes due.",
    )
    acknowledged_at: float = field(
        required=False,
        default=None,
        description="When exact applied-and-rendered foreground acknowledgment arrived.",
    )
    visibility_proof_epoch: int = field(
        required=False,
        default=None,
        description="Server-validated visible-client epoch bound to acknowledgment.",
    )
    ack_epoch: int = field(
        required=False,
        default=None,
        description="Monotonic client acknowledgment epoch.",
    )
    released_at: float = field(
        required=False,
        default=None,
        description="First time a background target crossed its final guard.",
    )
    cancel_reason: str | None = field(
        required=False,
        default=None,
        description="Bounded code-owned reason delivery became terminal.",
    )

    @classmethod
    def validate(cls, payload: Any) -> None:  # noqa: C901 — state envelopes are intentionally explicit
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        _text(cls, payload, "event_id", maximum=128)
        _text(cls, payload, "attention_id", maximum=128)
        _integer(cls, payload, "source_version")
        _slug(cls, payload, "application_scope")
        _slug(cls, payload, "notification_class")
        _integer(cls, payload, "class_policy_revision", minimum=1)
        for name in (
            "delivery_class",
            "budget_class",
            "privacy_renderer_id",
            "route_builder_id",
            "destination_id",
        ):
            _slug(cls, payload, name)
        _text(cls, payload, "coalesce_key", maximum=256)
        guard = _closed_object(
            cls,
            payload.get("source_guard"),
            "source_guard",
            allowed=_SOURCE_GUARD_FIELDS,
            required=_SOURCE_GUARD_FIELDS,
        )
        _slug(cls, guard, "kind")
        _text(cls, guard, "ref", maximum=256)
        _integer(cls, guard, "version")

        created = _timestamp(cls, payload, "created_at")
        expires = _timestamp(cls, payload, "expires_at")
        updated = _timestamp(cls, payload, "updated_at")
        if expires <= created:
            _fail(cls, "expires_at must be after created_at")
        if not created <= updated <= expires:
            _fail(cls, "updated_at must fall between created_at and expires_at")
        _integer(cls, payload, "state_version", minimum=1)

        selected = _optional_timestamp(cls, payload, "foreground_selected_at")
        fallback = _optional_timestamp(cls, payload, "fallback_due_at")
        acknowledged = _optional_timestamp(cls, payload, "acknowledged_at")
        released = _optional_timestamp(cls, payload, "released_at")
        if (selected is None) != (fallback is None):
            _fail(
                cls,
                "foreground_selected_at and fallback_due_at form one envelope",
            )
        if selected is not None and not created <= selected <= expires:
            _fail(cls, "foreground_selected_at is outside the latch lifetime")
        if fallback is not None and not created < fallback <= expires:
            _fail(cls, "fallback_due_at must be after creation and no later than expiry")
        if selected is not None and fallback is not None and fallback < selected:
            _fail(cls, "fallback_due_at cannot precede foreground_selected_at")
        for name, value in (("acknowledged_at", acknowledged), ("released_at", released)):
            if value is not None and not created <= value <= expires:
                _fail(cls, f"{name} is outside the latch lifetime")
        for name, value in (
            ("foreground_selected_at", selected),
            ("acknowledged_at", acknowledged),
            ("released_at", released),
        ):
            if value is not None and updated < value:
                _fail(cls, f"updated_at cannot precede {name}")
        if fallback is not None and released is not None and released < fallback:
            _fail(cls, "released_at cannot precede fallback_due_at")
        if acknowledged is not None and released is not None and released > acknowledged:
            _fail(cls, "released_at cannot follow acknowledged_at")
        for name in ("visibility_proof_epoch", "ack_epoch"):
            if payload.get(name) is not None:
                _integer(cls, payload, name)
        _optional_text(cls, payload, "cancel_reason", maximum=160)

        state = payload.get("state")
        has_ack_proof = all(
            payload.get(name) is not None
            for name in ("acknowledged_at", "visibility_proof_epoch", "ack_epoch")
        )
        any_ack_proof = any(
            payload.get(name) is not None
            for name in ("acknowledged_at", "visibility_proof_epoch", "ack_epoch")
        )
        if any_ack_proof and not has_ack_proof:
            _fail(cls, "acknowledgment timestamp and both epochs form one envelope")
        if state == "foreground_wait":
            if selected is None or fallback is None:
                _fail(cls, "foreground_wait requires selection and fallback deadline")
            if any_ack_proof or released is not None or payload.get("cancel_reason") is not None:
                _fail(cls, "foreground_wait cannot carry terminal/release fields")
        elif state == "background_due":
            if fallback is not None and updated < fallback:
                _fail(
                    cls,
                    "background_due updated_at cannot precede fallback_due_at",
                )
            if any_ack_proof or released is not None or payload.get("cancel_reason") is not None:
                _fail(cls, "background_due cannot carry ack, release, or cancel fields")
        elif state == "background_released":
            if released is None:
                _fail(cls, "background_released requires released_at")
            if any_ack_proof or payload.get("cancel_reason") is not None:
                _fail(cls, "background_released cannot carry ack or cancel fields")
        elif state == "foreground_applied":
            if not has_ack_proof:
                _fail(cls, "foreground_applied requires acknowledged_at and both epochs")
            if payload.get("cancel_reason") is not None:
                _fail(cls, "foreground_applied cannot carry cancel_reason")
        elif state in ("ineligible", "expired"):
            if not payload.get("cancel_reason"):
                _fail(cls, f"{state} requires cancel_reason")
            if any_ack_proof:
                _fail(cls, f"{state} cannot carry acknowledgment fields")


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
