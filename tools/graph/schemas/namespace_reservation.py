"""Organization-homed sovereign Service namespace reservations.

The Setting member key is the sole stable reservation identity.  Values do
not repeat that key, the owning organization, or the origin recoverable from
``app_label`` and ``persona_label``.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any

from .registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
)


NAMESPACE_RESERVATION_SET_ID = "autonomy.network.namespace-reservation"
NAMESPACE_RESERVATION_REVISION = 1
RESERVATION_KEY_STRATEGY = "reservation_id"
RESERVED_APP_LABELS = frozenset(
    {"_autonomy", "www", "api", "relay", "registry", "auto", "serve"}
)
RESERVATION_STATES = ("active", "paused", "released")

_APP_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_PERSONA_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_PERSONA_PUB_RE = re.compile(r"^[0-9a-f]{64}$")
_RFC3339_MILLIS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)


def validate_app_label_value(value: Any) -> str:
    if not isinstance(value, str) or not _APP_LABEL_RE.fullmatch(value):
        raise SchemaValidationError(
            "NamespaceReservationV1: app_label must be a lowercase DNS label"
        )
    if value in RESERVED_APP_LABELS:
        raise SchemaValidationError(
            f"NamespaceReservationV1: app_label {value!r} is reserved"
        )
    return value


def validate_reservation_key(value: Any) -> str:
    if not isinstance(value, str):
        raise SchemaValidationError(
            "NamespaceReservationV1: reservation key must be a canonical UUID"
        )
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise SchemaValidationError(
            "NamespaceReservationV1: reservation key must be a canonical UUID"
        ) from None
    if str(parsed) != value:
        raise SchemaValidationError(
            "NamespaceReservationV1: reservation key must be a canonical UUID"
        )
    return value


SYNOPSIS = {
    "summary": (
        "One stable app origin reservation per persona/app pair. The member key "
        "is the reservation UUID and the payload owns its lifecycle."
    ),
    "nouns": [
        "service origin",
        "namespace reservation",
        "app subdomain",
        "sovereign publication",
    ],
    "related_set_ids": [
        "autonomy.org.member-profile#1",
        "autonomy.network.service-target#1",
    ],
}


@publication_band(max="raw")
@home("organization")
@keyed_per_entity(key_strategy=RESERVATION_KEY_STRATEGY)
class NamespaceReservationV1(SettingSchema):
    """A persona-owned ``<app>.<persona>.serve.auto.network`` origin."""

    set_id = NAMESPACE_RESERVATION_SET_ID
    schema_revision = NAMESPACE_RESERVATION_REVISION

    persona_pub: str = field(
        required=True,
        description="Organization-scoped persona public key as lowercase hex.",
    )
    persona_label: str = field(
        required=True,
        description="Immutable DNS label derived from display name and persona key.",
    )
    app_label: str = field(
        required=True,
        description="Operator-selected app DNS label.",
    )
    state: str = field(
        required=True,
        enum=list(RESERVATION_STATES),
        description="Authoritative active, paused, or terminally released state.",
    )
    created_at: str = field(required=True, description="Creation time in RFC 3339 milliseconds.")
    updated_at: str = field(required=True, description="Last lifecycle write time.")
    released_at: str = field(required=False, description="Terminal release time.")
    product_ref: str = field(
        required=False,
        description="Optional opaque product-plane reservation reference.",
    )

    @classmethod
    def validate_member_key(cls, key: str) -> None:
        validate_reservation_key(key)

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        persona_pub = payload.get("persona_pub")
        if not isinstance(persona_pub, str) or not _PERSONA_PUB_RE.fullmatch(persona_pub):
            raise SchemaValidationError(
                f"{cls.__name__}: persona_pub must be exactly 64 lowercase hex characters"
            )
        validate_app_label_value(payload.get("app_label"))
        persona_label = payload.get("persona_label")
        digest = hashlib.sha256(bytes.fromhex(persona_pub)).hexdigest()[:20]
        suffix = "-" + digest
        if not isinstance(persona_label, str) or not persona_label.endswith(suffix):
            raise SchemaValidationError(
                f"{cls.__name__}: persona_label must end in its persona digest"
            )
        slug = persona_label[: -len(suffix)]
        if not 1 <= len(slug) <= 42 or not _PERSONA_SLUG_RE.fullmatch(slug):
            raise SchemaValidationError(
                f"{cls.__name__}: persona_label has an invalid slug"
            )
        for name in ("created_at", "updated_at"):
            value = payload.get(name)
            if not isinstance(value, str) or not _RFC3339_MILLIS_RE.fullmatch(value):
                raise SchemaValidationError(
                    f"{cls.__name__}: {name} must be UTC RFC 3339 with milliseconds"
                )
        state = payload.get("state")
        released_at = payload.get("released_at")
        if state == "released":
            if not isinstance(released_at, str) or not _RFC3339_MILLIS_RE.fullmatch(released_at):
                raise SchemaValidationError(
                    f"{cls.__name__}: released rows require released_at"
                )
        elif "released_at" in payload:
            raise SchemaValidationError(
                f"{cls.__name__}: only released rows may carry released_at"
            )
        product_ref = payload.get("product_ref")
        if "product_ref" in payload and (
            not isinstance(product_ref, str)
            or not product_ref
            or len(product_ref) > 128
            or any(ord(char) < 0x20 or ord(char) > 0x7E for char in product_ref)
        ):
            raise SchemaValidationError(
                f"{cls.__name__}: product_ref must be 1-128 printable ASCII characters"
            )
