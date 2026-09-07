"""Organization-owned delegated serving zones (custom domains).

A zone row records that the registry verified this organization's claim on a
delegated DNS zone (``autonomy.example.com``) so Services can publish
directly under it: ``<app>.<zone>`` with one wildcard certificate per zone.
The member key is the zone itself.  The registry's ``serve_zones`` table is
the authority; this row is the dashboard's projection of a successful claim
and drives the "Publish under" picker, certificate issuance, and the gateway.
"""

from __future__ import annotations

import re
from typing import Any

from .registry import (
    SchemaValidationError,
    SettingSchema,
    field,
    home,
    keyed_per_entity,
    publication_band,
)


SERVE_ZONE_SET_ID = "autonomy.network.serve-zone"
SERVE_ZONE_REVISION = 1
SERVE_BASE_DOMAIN = "serve.auto.network"
ZONE_BINDING_KINDS = ("parent-txt", "ns-token")
ZONE_STATES = ("active", "revoked")

_ZONE_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def validate_zone_value(value: Any) -> str:
    """Normalize and bound an organization zone exactly as the registry does:
    lowercase FQDN without a trailing dot, at least three labels, never the
    base zone and never anything under ``auto.network``."""
    if not isinstance(value, str):
        raise SchemaValidationError("zone must be a string")
    zone = value.strip().rstrip(".").lower()
    if not zone or len(zone) > 253:
        raise SchemaValidationError("zone is not a valid FQDN")
    labels = zone.split(".")
    if len(labels) < 3:
        raise SchemaValidationError("a delegated zone needs at least three labels")
    if any(_ZONE_LABEL_RE.fullmatch(label) is None for label in labels):
        raise SchemaValidationError("zone carries a malformed label")
    if zone == "auto.network" or zone.endswith(".auto.network"):
        raise SchemaValidationError("zone must be outside auto.network")
    return zone


SYNOPSIS = {
    "summary": (
        "One verified organization-owned delegated zone per row; Services "
        "publish directly under it as <app>.<zone>."
    ),
    "nouns": ["custom domain", "delegated zone", "serving zone"],
    "related_set_ids": ["autonomy.network.namespace-reservation#1"],
}


@publication_band(max="raw")
@home("organization")
@keyed_per_entity(key_strategy="zone")
class ServeZoneV1(SettingSchema):
    """A registry-verified claim on a delegated zone."""

    set_id = SERVE_ZONE_SET_ID
    schema_revision = SERVE_ZONE_REVISION

    binding_kind: str = field(
        required=True,
        enum=list(ZONE_BINDING_KINDS),
        description=(
            "How the parent zone binds the delegation to this organization: "
            "a TXT at _autonomy.<parent>, or an NS at <org-uuid>.ns.auto.network."
        ),
    )
    state: str = field(
        required=True,
        enum=list(ZONE_STATES),
        description="active while claimed; revoked after release.",
    )
    verified_at: int = field(
        required=True,
        description="Epoch seconds when the registry last verified the binding.",
    )
    claimed_at: str = field(required=True, description="First claim time, RFC 3339.")
    updated_at: str = field(required=True, description="Last lifecycle write, RFC 3339.")

    @classmethod
    def validate_member_key(cls, key: str) -> None:
        if validate_zone_value(key) != key:
            raise SchemaValidationError(
                f"{cls.__name__}: key must be the normalized zone"
            )

    @classmethod
    def validate(cls, payload: Any) -> None:
        super().validate(payload)
        if not isinstance(payload, dict):
            return
        if payload.get("binding_kind") not in ZONE_BINDING_KINDS:
            raise SchemaValidationError(f"{cls.__name__}: unknown binding_kind")
        if payload.get("state") not in ZONE_STATES:
            raise SchemaValidationError(f"{cls.__name__}: unknown state")
        verified = payload.get("verified_at")
        if isinstance(verified, bool) or not isinstance(verified, int) or verified <= 0:
            raise SchemaValidationError(f"{cls.__name__}: verified_at must be positive")
        for name in ("claimed_at", "updated_at"):
            if not isinstance(payload.get(name), str) or not payload[name]:
                raise SchemaValidationError(f"{cls.__name__}: {name} is required")
