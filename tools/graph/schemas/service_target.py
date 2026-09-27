"""Trusted local targets for sovereign Service namespace reservations.

The Setting key is the NamespaceReservation UUID.  The payload deliberately
contains no address or caller-selected upstream: it freezes the local machine,
Dashboard session, container incarnation, and TCP port only.
"""

from __future__ import annotations

import re
from typing import Any

from .namespace_reservation import _is_rfc3339_millis, validate_reservation_key
from .registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
)


SERVICE_TARGET_SET_ID = "autonomy.network.service-target"
SERVICE_TARGET_REVISION = 2
SERVICE_TARGET_KEY_STRATEGY = "reservation_id"

SESSION_TARGET_KIND = "session"
DASHBOARD_TARGET_KIND = "dashboard"
TARGET_KINDS = (SESSION_TARGET_KIND, DASHBOARD_TARGET_KIND)
#: The only access mode a dashboard target may carry.
DASHBOARD_ACCESS_MODE = "personal"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_access_mode(payload: dict) -> None:
    if payload.get("access_mode", "public") not in {"public", "personal", "oidc"}:
        raise SchemaValidationError("access_mode must be public, personal or oidc")


SYNOPSIS = {
    "summary": (
        "The current trusted machine/session/container/port target for one "
        "sovereign Service namespace reservation."
    ),
    "nouns": ["service target", "target binding", "session port"],
    "related_set_ids": ["autonomy.network.namespace-reservation#1"],
}


@publication_band(max="raw")
@home("organization")
@keyed_per_entity(key_strategy=SERVICE_TARGET_KEY_STRATEGY)
class ServiceTargetV1(SettingSchema):
    """One currently configured local target, keyed by reservation UUID."""

    set_id = SERVICE_TARGET_SET_ID
    schema_revision = 1

    machine_id: str = field(required=True, description="Local enrolled machine ID.")
    session_id: str = field(required=True, description="Trusted Dashboard session name.")
    container_id: str = field(required=True, description="Frozen full Docker container ID.")
    port: int = field(required=True, description="TCP port in the session container.")
    created_at: str = field(required=True, description="Binding creation time.")
    updated_at: str = field(required=True, description="Last reassignment time.")
    access_mode: str = field(required=False, description="Public, personal passkey, or organization OIDC access.")

    @classmethod
    def validate_member_key(cls, key: str) -> None:
        validate_reservation_key(key)

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        _validate_target_fields(cls.__name__, payload)
        _validate_session_id(cls.__name__, payload)


def _validate_target_fields(name: str, payload: dict) -> None:
    validate_access_mode(payload)
    if not isinstance(payload.get("machine_id"), str) or not _HEX64_RE.fullmatch(
        payload["machine_id"]
    ):
        raise SchemaValidationError(
            f"{name}: machine_id must be exactly 64 lowercase hex characters"
        )
    if not isinstance(payload.get("container_id"), str) or not _HEX64_RE.fullmatch(
        payload["container_id"]
    ):
        raise SchemaValidationError(
            f"{name}: container_id must be exactly 64 lowercase hex characters"
        )
    port = payload.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise SchemaValidationError(f"{name}: port must be 1 through 65535")
    for field_name in ("created_at", "updated_at"):
        if not _is_rfc3339_millis(payload.get(field_name)):
            raise SchemaValidationError(
                f"{name}: {field_name} must be UTC RFC 3339 with milliseconds"
            )


def _validate_session_id(name: str, payload: dict) -> None:
    if not isinstance(payload.get("session_id"), str) or not _SESSION_ID_RE.fullmatch(
        payload["session_id"]
    ):
        raise SchemaValidationError(f"{name}: session_id is invalid")


@publication_band(max="raw")
@home("organization")
@keyed_per_entity(key_strategy=SERVICE_TARGET_KEY_STRATEGY)
class ServiceTargetV2(ServiceTargetV1):
    """Revision 2 adds ``kind``.

    ``session`` (the default; every revision-1 row upconverts to it) names a
    trusted Dashboard session container exactly as before. ``dashboard`` is
    the node's own dashboard container, published on its plain-HTTP listener:
    no session backs it, so ``session_id`` is absent, and the frozen
    ``container_id`` is the dashboard container's own incarnation.
    """

    set_id = SERVICE_TARGET_SET_ID
    schema_revision = SERVICE_TARGET_REVISION

    kind: str = field(required=False, description="session (default) or dashboard.")
    session_id: str = field(
        required=False,
        description="Trusted Dashboard session name; required for kind session, absent for kind dashboard.",
    )

    @classmethod
    def upconvert_from_prev(cls, payload: dict) -> dict:
        return {**payload, "kind": SESSION_TARGET_KIND}

    @classmethod
    def validate(cls, payload: Any) -> None:
        super(ServiceTargetV1, cls).validate(payload)
        if not isinstance(payload, dict):
            return
        kind = payload.get("kind", SESSION_TARGET_KIND)
        if kind not in TARGET_KINDS:
            raise SchemaValidationError(f"{cls.__name__}: kind must be session or dashboard")
        _validate_target_fields(cls.__name__, payload)
        if kind == SESSION_TARGET_KIND:
            _validate_session_id(cls.__name__, payload)
            if payload["session_id"] == DASHBOARD_TARGET_KIND:
                # The dashboard route's display name; a session may not wear it.
                raise SchemaValidationError(
                    f"{cls.__name__}: session_id 'dashboard' is reserved for kind dashboard"
                )
            return
        if "session_id" in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: session_id is not allowed for kind dashboard"
            )
        if payload.get("access_mode", "public") != DASHBOARD_ACCESS_MODE:
            # The dashboard's own gate is the personal passkey, always: a public
            # dashboard route would expose the unlock screen and every open
            # path to the internet (operator decision 2026-09-27).
            raise SchemaValidationError(
                f"{cls.__name__}: kind dashboard requires access_mode {DASHBOARD_ACCESS_MODE}"
            )
