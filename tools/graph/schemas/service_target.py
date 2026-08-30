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
SERVICE_TARGET_REVISION = 1
SERVICE_TARGET_KEY_STRATEGY = "reservation_id"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


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
    schema_revision = SERVICE_TARGET_REVISION

    machine_id: str = field(required=True, description="Local enrolled machine ID.")
    session_id: str = field(required=True, description="Trusted Dashboard session name.")
    container_id: str = field(required=True, description="Frozen full Docker container ID.")
    port: int = field(required=True, description="TCP port in the session container.")
    created_at: str = field(required=True, description="Binding creation time.")
    updated_at: str = field(required=True, description="Last reassignment time.")

    @classmethod
    def validate_member_key(cls, key: str) -> None:
        validate_reservation_key(key)

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        if not isinstance(payload.get("machine_id"), str) or not _HEX64_RE.fullmatch(
            payload["machine_id"]
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: machine_id must be exactly 64 lowercase hex characters"
            )
        if not isinstance(payload.get("container_id"), str) or not _HEX64_RE.fullmatch(
            payload["container_id"]
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: container_id must be exactly 64 lowercase hex characters"
            )
        if not isinstance(payload.get("session_id"), str) or not _SESSION_ID_RE.fullmatch(
            payload["session_id"]
        ):
            raise SchemaValidationError(f"{cls.__name__}: session_id is invalid")
        port = payload.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise SchemaValidationError(f"{cls.__name__}: port must be 1 through 65535")
        for name in ("created_at", "updated_at"):
            if not _is_rfc3339_millis(payload.get(name)):
                raise SchemaValidationError(
                    f"{cls.__name__}: {name} must be UTC RFC 3339 with milliseconds"
                )

